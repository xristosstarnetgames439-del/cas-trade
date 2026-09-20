from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from threading import RLock
from typing import Literal
from zoneinfo import ZoneInfo

from app.models import KlineBar, StrongStockCandidate, StrongStockDataUnavailable
from app.providers.recent_limit_up_candidates import (
    RecentLimitUpCandidateProvider,
    _fetch_pool_rows,
    parse_recent_limit_up_rows,
)
from app.services.auction_snatch import AuctionSnatchObservation, AuctionSnatchScan
from app.services.trading_calendar import is_open_session, previous_open_session

StrategyRawPeriod = Literal["month", "three_months", "year"]
ProgressCallback = Callable[[int, int, str], None]
CancelCheck = Callable[[], bool]

SHANGHAI = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = 1
_WRITE_LOCK = RLock()
_RESOLUTION_RANK = {"match_only": 0, "minute": 1, "seconds": 2}


class StrategyRawDownloadCanceled(RuntimeError):
    pass


class StrategyRawStore:
    """策略原始文件仓库；JSON 先写临时文件再原子替换。"""

    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / "strategy_raw"

    def pool_path(self, trade_date: str) -> Path:
        return self.root / "pools" / f"{trade_date}.json"

    def auction_path(self, trade_date: str, symbol: str) -> Path:
        return self.root / "auctions" / trade_date / f"{_safe_symbol(symbol)}.json"

    def kline_path(self, symbol: str) -> Path:
        return self.root / "klines" / f"{_safe_symbol(symbol)}.json"

    def load_pool(self, trade_date: str) -> dict[str, object] | None:
        return _read_json(self.pool_path(trade_date), trade_date=trade_date)

    def save_pool(self, trade_date: str, rows: list[dict[str, object]]) -> bool:
        path = self.pool_path(trade_date)
        if self.load_pool(trade_date) is not None:
            return False
        _atomic_json(
            path,
            {
                "schema_version": SCHEMA_VERSION,
                "trade_date": trade_date,
                "source": "AKShare 东方财富涨停池",
                "captured_at": _now(),
                "rows": rows,
            },
        )
        return True

    def load_auction(self, trade_date: str, symbol: str) -> dict[str, object] | None:
        payload = _read_json(self.auction_path(trade_date, symbol), trade_date=trade_date)
        if payload is None or payload.get("symbol") != symbol:
            return None
        return payload

    def archive_auction(
        self,
        trade_date: str,
        symbol: str,
        auction: object,
        observation: AuctionSnatchObservation | None,
        *,
        previous_open_volume: float | None,
    ) -> bool:
        points, resolution = _serializable_points(auction)
        snapshot = getattr(auction, "snapshot_0925", None)
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "trade_date": trade_date,
            "symbol": symbol,
            "source": "eltdx 通达信竞价",
            "captured_at": _now(),
            "resolution": resolution,
            "has_preopen_path": bool(points),
            "has_open_match": snapshot is not None,
            "has_previous_open_volume": previous_open_volume not in (None, 0),
            "pre_close_price": _number(getattr(auction, "pre_close_price", None)),
            "snapshot_0925": _snapshot_payload(snapshot),
            "previous_open_volume": previous_open_volume,
            "points": points,
            "observation": _observation_payload(observation),
        }
        path = self.auction_path(trade_date, symbol)
        with _WRITE_LOCK:
            existing = self.load_auction(trade_date, symbol)
            if existing is not None and not _is_better_auction(payload, existing):
                return False
            _atomic_json(path, payload)
        return True

    def load_klines(self, symbol: str) -> dict[str, object] | None:
        payload = _read_json(self.kline_path(symbol))
        if payload is None or payload.get("symbol") != symbol:
            return None
        bars = payload.get("bars")
        return payload if isinstance(bars, list) and bars else None

    def save_klines(self, symbol: str, bars: list[KlineBar]) -> bool:
        existing = self.load_klines(symbol)
        by_date: dict[str, dict[str, object]] = {}
        if existing is not None:
            for row in existing.get("bars", []):
                if isinstance(row, dict) and isinstance(row.get("date"), str):
                    by_date[str(row["date"])] = row
        for bar in bars:
            by_date[bar.date] = bar.model_dump(mode="json")
        if not by_date:
            return False
        ordered = [by_date[key] for key in sorted(by_date)]
        _atomic_json(
            self.kline_path(symbol),
            {
                "schema_version": SCHEMA_VERSION,
                "symbol": symbol,
                "source": "东方财富不复权日K",
                "captured_at": _now(),
                "start_date": ordered[0]["date"],
                "end_date": ordered[-1]["date"],
                "bars": ordered,
            },
        )
        return True

    def has_kline_coverage(self, symbol: str, start: date, end: date) -> bool:
        payload = self.load_klines(symbol)
        if payload is None:
            return False
        return str(payload.get("start_date", "")) <= start.isoformat() and str(
            payload.get("end_date", "")
        ) >= end.isoformat()


class LocalStrategyCandidateProvider:
    source_name = "本地历史涨停池"

    def __init__(self, store: StrategyRawStore, *, lookback_days: int) -> None:
        self.store = store
        self.lookback_days = max(1, lookback_days)

    def get_candidates(self, trade_date: str) -> list[StrongStockCandidate]:
        target = _parse_date(trade_date)
        dates = _previous_open_dates(target, self.lookback_days)
        rows_by_date: list[tuple[str, list[dict[str, object]]]] = []
        missing: list[str] = []
        for day in dates:
            payload = self.store.load_pool(day.isoformat())
            if payload is None:
                missing.append(day.isoformat())
                continue
            rows = payload.get("rows")
            rows_by_date.append(
                (day.strftime("%Y%m%d"), rows if isinstance(rows, list) else [])
            )
        if missing:
            raise StrongStockDataUnavailable(
                f"历史涨停池缺失：{', '.join(missing[:5])}，请先下载对应区间"
            )
        return parse_recent_limit_up_rows(rows_by_date)


class LocalStrategyKlineProvider:
    def __init__(self, store: StrategyRawStore) -> None:
        self.store = store

    def get_klines(self, symbol: str, count: int = 220) -> list[KlineBar]:
        payload = self.store.load_klines(symbol)
        if payload is None:
            raise StrongStockDataUnavailable(f"{symbol} 本地日K缺失，请先下载对应区间")
        bars = [KlineBar.model_validate(row) for row in payload.get("bars", [])]
        return bars[-count:]


class LocalStrategyAuctionProvider:
    source_name = "本地历史竞价文件"

    def __init__(
        self,
        store: StrategyRawStore,
        *,
        require_preopen: bool,
        require_seconds: bool,
    ) -> None:
        self.store = store
        self.require_preopen = require_preopen
        self.require_seconds = require_seconds
        self.kline_provider = LocalStrategyKlineProvider(store)

    def scan(self, symbols: list[str], *, trade_date: str) -> AuctionSnatchScan:
        observations: dict[str, AuctionSnatchObservation] = {}
        problems: list[str] = []
        for symbol in symbols:
            payload = self.store.load_auction(trade_date, symbol)
            if payload is None:
                problems.append(f"{symbol}竞价文件缺失")
                continue
            resolution = str(payload.get("resolution", "match_only"))
            if self.require_preopen and payload.get("has_preopen_path") is not True:
                problems.append(f"{symbol}只有09:25撮合，缺少09:25前竞价路径")
                continue
            if self.require_seconds and resolution != "seconds":
                problems.append(f"{symbol}没有秒级竞价，无法计算有效抬价次数")
                continue
            if self.store.load_klines(symbol) is None:
                problems.append(f"{symbol}日K文件缺失")
                continue
            observation = _observation_from_payload(payload)
            if observation is None:
                problems.append(f"{symbol}竞价文件字段不完整")
                continue
            observations[symbol] = observation
        if problems:
            detail = "；".join(problems[:5])
            suffix = f"；另有 {len(problems) - 5} 项" if len(problems) > 5 else ""
            raise StrongStockDataUnavailable(f"历史数据不可判定：{detail}{suffix}")
        return AuctionSnatchScan(observations=observations, attempted=len(symbols))


def strategy_download_dates(
    period: StrategyRawPeriod,
    *,
    now: datetime | None = None,
) -> list[date]:
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    today = current.date()
    end = (
        today
        if is_open_session(today) and current.time() >= time(15, 5)
        else previous_open_session(today)
    )
    if period == "month":
        start = end.replace(day=1)
    elif period == "three_months":
        start = _shift_month_start(end, -2)
    elif period == "year":
        start = end.replace(month=1, day=1)
    else:
        raise ValueError("下载周期仅支持本月、近三月或本年")
    return _open_dates(start, end)


def run_strategy_raw_download(
    data_dir: Path,
    period: StrategyRawPeriod,
    *,
    lookback_days: int,
    symbol_prefixes: tuple[str, ...] = (),
    progress: ProgressCallback,
    should_cancel: CancelCheck,
    pool_fetcher: Callable[[str], list[dict[str, object]]] | None = None,
    auction_provider_factory: Callable[[StrategyRawStore], object] | None = None,
    kline_provider: object | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    dates = strategy_download_dates(period, now=now)
    if not dates:
        raise StrongStockDataUnavailable("所选区间没有已完成交易日")
    store = StrategyRawStore(data_dir)
    if pool_fetcher is None:
        provider = RecentLimitUpCandidateProvider.from_akshare()
        if provider.pool_fetcher is None:
            raise StrongStockDataUnavailable("AKShare 未安装，无法下载历史涨停池")
        pool_fetcher = provider.pool_fetcher
    if auction_provider_factory is None:
        from app.providers.eltdx_auction import EltdxAuctionProvider

        def default_auction_provider(raw_store: StrategyRawStore) -> object:
            return EltdxAuctionProvider(
                workers=4,
                archive_store=raw_store,
                cancel_check=should_cancel,
            )

        auction_provider_factory = default_auction_provider
    owns_kline_provider = kline_provider is None
    if kline_provider is None:
        from app.providers.eastmoney_kline import EastmoneyKlineProvider

        kline_provider = EastmoneyKlineProvider(adjust="none")

    downloaded_pools = downloaded_auctions = downloaded_klines = 0
    failures: dict[str, str] = {}
    warmup_start = _previous_open_dates(dates[0], max(lookback_days, 10))[-1]
    kline_count = max(80, (dates[-1] - warmup_start).days * 5 // 7 + 60)
    try:
        for index, trade_day in enumerate(dates):
            _raise_if_canceled(should_cancel)
            trade_date = trade_day.isoformat()
            pool_dates = [trade_day, *_previous_open_dates(trade_day, lookback_days)]
            for pool_day in pool_dates:
                pool_date = pool_day.isoformat()
                if store.load_pool(pool_date) is not None:
                    continue
                rows = _fetch_pool_rows(pool_fetcher, pool_day.strftime("%Y%m%d"))
                downloaded_pools += int(store.save_pool(pool_date, rows))
            candidates = LocalStrategyCandidateProvider(
                store, lookback_days=lookback_days
            ).get_candidates(trade_date.replace("-", ""))
            if symbol_prefixes:
                candidates = [
                    candidate
                    for candidate in candidates
                    if candidate.symbol.startswith(symbol_prefixes)
                ]
            symbols = [candidate.symbol for candidate in candidates]
            missing_auctions = [
                symbol
                for symbol in symbols
                if store.load_auction(trade_date, symbol) is None
            ]
            if missing_auctions:
                try:
                    auction_provider_factory(store).scan(
                        missing_auctions, trade_date=trade_date
                    )
                except Exception as exc:
                    _raise_if_canceled(should_cancel)
                    reason = _exception_summary(exc)
                    for symbol in missing_auctions:
                        path = f"auctions/{trade_date}/{symbol}.json"
                        failures[path] = f"{path}：竞价请求失败（{reason}）"
                _raise_if_canceled(should_cancel)
            for symbol in symbols:
                auction_path = f"auctions/{trade_date}/{symbol}.json"
                if store.load_auction(trade_date, symbol) is None:
                    failures.setdefault(
                        auction_path,
                        f"{auction_path}：竞价请求完成后仍未生成文件",
                    )
                else:
                    failures.pop(auction_path, None)
                    downloaded_auctions += 1
                kline_path = f"klines/{symbol}.json"
                if store.has_kline_coverage(symbol, warmup_start, dates[-1]):
                    failures.pop(kline_path, None)
                    continue
                _raise_if_canceled(should_cancel)
                try:
                    bars = kline_provider.get_klines(symbol, count=kline_count)
                    downloaded_klines += int(store.save_klines(symbol, bars))
                    if store.has_kline_coverage(symbol, warmup_start, dates[-1]):
                        failures.pop(kline_path, None)
                    else:
                        payload = store.load_klines(symbol) or {}
                        actual_start = str(payload.get("start_date") or "无数据")
                        actual_end = str(payload.get("end_date") or "无数据")
                        failures[kline_path] = (
                            f"{kline_path}：日K覆盖不足，需要 {warmup_start.isoformat()}"
                            f"～{dates[-1].isoformat()}，实际 {actual_start}～{actual_end}"
                        )
                except Exception as exc:
                    failures[kline_path] = (
                        f"{kline_path}：日K请求失败（{_exception_summary(exc)}）"
                    )
            progress(
                index + 1,
                len(dates),
                f"已下载 {index + 1}/{len(dates)} 个交易日，{trade_date} 候选 {len(symbols)} 只",
            )
        if failures:
            details = list(failures.values())
            preview = "；".join(details[:5])
            remaining = f"；另有 {len(details) - 5} 项" if len(details) > 5 else ""
            raise StrongStockDataUnavailable(
                f"下载已完成，但有 {len(details)} 个文件不完整。"
                f"目标交易区间：{dates[0].isoformat()}～{dates[-1].isoformat()}；"
                f"日K预热区间：{warmup_start.isoformat()}～{dates[-1].isoformat()}；"
                f"失败明细：{preview}{remaining}。已成功写入的数据会保留，重新下载会自动续传"
            )
        return {
            "period": period,
            "start_date": dates[0].isoformat(),
            "end_date": dates[-1].isoformat(),
            "trading_days": len(dates),
            "downloaded_pools": downloaded_pools,
            "downloaded_auctions": downloaded_auctions,
            "downloaded_klines": downloaded_klines,
        }
    finally:
        if owns_kline_provider:
            close = getattr(kline_provider, "close", None)
            if callable(close):
                close()


def _serializable_points(auction: object) -> tuple[list[dict[str, object]], str]:
    series = getattr(auction, "series", None)
    source = list(getattr(series, "points", ()) or ())
    resolution = "seconds"
    if not source:
        source = list(getattr(auction, "auction_records", ()) or ())
        resolution = "minute" if source else "match_only"
    points = []
    for point in source:
        points.append(
            {
                "time_label": str(getattr(point, "time_label", "") or ""),
                "time_seconds": getattr(point, "time_seconds", None),
                "time_minutes": getattr(point, "time_minutes", None),
                "price": _number(getattr(point, "price", None)),
                "volume": _number(getattr(point, "volume", None)),
            }
        )
    return points, resolution


def _snapshot_payload(snapshot: object | None) -> dict[str, float | None] | None:
    if snapshot is None:
        return None
    return {
        "price": _number(getattr(snapshot, "price", None)),
        "volume": _number(getattr(snapshot, "volume", None)),
        "trade_amount_yuan": _number(getattr(snapshot, "trade_amount_yuan", None)),
    }


def _observation_payload(
    observation: AuctionSnatchObservation | None,
) -> dict[str, object] | None:
    if observation is None:
        return None
    return {
        "symbol": observation.symbol,
        "open_price": observation.open_price,
        "open_change_pct": observation.open_change_pct,
        "open_volume": observation.open_volume,
        "open_amount": observation.open_amount,
        "previous_price": observation.previous_price,
        "previous_time": observation.previous_time,
        "last_second_pct": observation.last_second_pct,
        "valid_raise_count": observation.valid_raise_count,
        "previous_open_volume": observation.previous_open_volume,
        "auction_volume_ratio": observation.auction_volume_ratio,
    }


def _observation_from_payload(payload: dict[str, object]) -> AuctionSnatchObservation | None:
    raw = payload.get("observation")
    if not isinstance(raw, dict):
        return None
    try:
        resolution = str(payload.get("resolution", "match_only"))
        return AuctionSnatchObservation(
            symbol=str(raw["symbol"]),
            open_price=float(raw["open_price"]),
            open_change_pct=_optional_float(raw.get("open_change_pct")),
            open_volume=_optional_float(raw.get("open_volume")),
            open_amount=_optional_float(raw.get("open_amount")),
            previous_price=float(raw["previous_price"]),
            previous_time=str(raw["previous_time"]),
            last_second_pct=float(raw["last_second_pct"]),
            valid_raise_count=(
                int(raw["valid_raise_count"])
                if resolution == "seconds" and raw.get("valid_raise_count") is not None
                else None
            ),
            previous_open_volume=_optional_float(raw.get("previous_open_volume")),
            auction_volume_ratio=_optional_float(raw.get("auction_volume_ratio")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _is_better_auction(new: dict[str, object], old: dict[str, object]) -> bool:
    def quality(payload: dict[str, object]) -> tuple[int, bool, bool, bool, int]:
        return (
            _RESOLUTION_RANK.get(str(payload.get("resolution")), -1),
            payload.get("has_open_match") is True,
            payload.get("has_previous_open_volume") is True,
            isinstance(payload.get("observation"), dict),
            len(payload.get("points", [])),
        )

    return quality(new) > quality(old)


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".part",
            delete=False,
        ) as stream:
            temp_path = stream.name
            json.dump(payload, stream, ensure_ascii=False, indent=2, default=_json_default)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def _read_json(path: Path, *, trade_date: str | None = None) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        return None
    if trade_date is not None and payload.get("trade_date") != trade_date:
        return None
    return payload


def _json_default(value: object) -> object:
    if hasattr(value, "item"):
        return value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _exception_summary(exc: Exception) -> str:
    detail = " ".join(str(exc).split())
    return f"{exc.__class__.__name__}: {detail[:160]}" if detail else exc.__class__.__name__


def _previous_open_dates(value: date, count: int) -> list[date]:
    output: list[date] = []
    cursor = value
    while len(output) < max(0, count):
        cursor = previous_open_session(cursor)
        output.append(cursor)
    return output


def _open_dates(start: date, end: date) -> list[date]:
    output = []
    cursor = start
    while cursor <= end:
        if is_open_session(cursor):
            output.append(cursor)
        cursor += timedelta(days=1)
    return output


def _shift_month_start(value: date, offset: int) -> date:
    month_index = value.year * 12 + value.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, 1)


def _parse_date(value: str) -> date:
    text = value.strip()
    return date.fromisoformat(text) if "-" in text else datetime.strptime(text, "%Y%m%d").date()


def _raise_if_canceled(should_cancel: CancelCheck) -> None:
    if should_cancel():
        raise StrategyRawDownloadCanceled("历史数据下载已取消")


def _safe_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if not normalized or any(character not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ._-" for character in normalized):
        raise ValueError("证券代码格式不正确")
    return normalized


def _optional_float(value: object) -> float | None:
    return None if value in (None, "") else float(value)


def _number(value: object) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")
