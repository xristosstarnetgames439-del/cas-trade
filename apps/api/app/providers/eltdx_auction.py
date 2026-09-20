from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from datetime import date, datetime, timedelta
from functools import partial
import logging
from zoneinfo import ZoneInfo

from app.models import StrongStockDataUnavailable
from app.services.auction_snatch import (
    AuctionSnatchObservation,
    AuctionSnatchScan,
)
from app.services.trading_calendar import is_open_session

SHANGHAI = ZoneInfo("Asia/Shanghai")
_AUCTION_START_SECONDS = 9 * 3600 + 20 * 60
_AUCTION_END_SECONDS = 9 * 3600 + 25 * 60
_LAST_MOMENT_START_SECONDS = 9 * 3600 + 24 * 60 + 30
_MINUTE_LAST_MOMENT_START_SECONDS = 9 * 3600 + 24 * 60
_RAISE_WINDOW_SECONDS = 30

logger = logging.getLogger(__name__)



class EltdxAuctionProvider:
    """通过 eltdx 读取通达信秒级竞价过程和 09:25 正式撮合。"""

    source_name = "eltdx 通达信竞价"

    def __init__(
        self,
        *,
        timeout_seconds: float = 5,
        workers: int = 8,
        archive_store: object | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.workers = max(1, workers)
        self.archive_store = archive_store
        self.cancel_check = cancel_check or (lambda: False)

    def scan(self, symbols: list[str], *, trade_date: str) -> AuctionSnatchScan:
        unique_symbols = list(dict.fromkeys(symbols))
        if not unique_symbols:
            return AuctionSnatchScan(observations={}, attempted=0)
        try:
            from eltdx import TdxClient
        except ModuleNotFoundError as exc:
            raise StrongStockDataUnavailable("eltdx 未安装，无法读取通达信竞价过程") from exc

        observations: dict[str, AuctionSnatchObservation] = {}
        failed = 0
        live = _is_live_auction_session(trade_date)
        worker = partial(_load_observation, live=live, archive_store=self.archive_store)
        workers = min(self.workers if live else min(self.workers, 4), len(unique_symbols))
        per_stock_timeout = self.timeout_seconds + (10 if live else 40)
        overall_timeout = per_stock_timeout * ((len(unique_symbols) + workers - 1) // workers) + 30
        with TdxClient(
            timeout=self.timeout_seconds if live else max(self.timeout_seconds, 20),
            pool_size=workers,
            probe_hosts=True,
        ) as client:
            executor = ThreadPoolExecutor(max_workers=workers)
            try:
                futures = {
                    executor.submit(worker, client, symbol, trade_date): symbol
                    for symbol in unique_symbols
                }
                for future in as_completed(futures, timeout=overall_timeout):
                    if self.cancel_check():
                        break
                    symbol = futures[future]
                    try:
                        observation = future.result(timeout=per_stock_timeout)
                    except Exception:
                        failed += 1
                        continue
                    if observation is not None:
                        observations[symbol] = observation
            except TimeoutError:
                failed += sum(1 for future in futures if not future.done())
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        return AuctionSnatchScan(
            observations=observations,
            attempted=len(unique_symbols),
            failed=failed,
        )


def _load_observation(
    client: object,
    symbol: str,
    trade_date: str,
    *,
    live: bool = True,
    archive_store: object | None = None,
) -> AuctionSnatchObservation | None:
    code = _eltdx_code(symbol)
    if live:
        auction = client.helpers.auction_data(code)
        if not _same_trade_date(getattr(auction, "trading_date", None), trade_date):
            # 秒级竞价序列只保存主站当前交易日，不得拿其他日期的数据冒充。
            return None
        observation = _observation_from_auction(
            client, symbol, code, trade_date, auction
        )
        _archive_raw_auction(
            archive_store, client, symbol, code, trade_date, auction, observation
        )
        if observation is not None:
            return observation
    auction = client.helpers.auction_data(code, trade_date)
    observation = _observation_from_auction(client, symbol, code, trade_date, auction)
    _archive_raw_auction(
        archive_store, client, symbol, code, trade_date, auction, observation
    )
    return observation


def _archive_raw_auction(
    archive_store: object | None,
    client: object,
    symbol: str,
    code: str,
    trade_date: str,
    auction: object,
    observation: AuctionSnatchObservation | None,
) -> None:
    archive = getattr(archive_store, "archive_auction", None)
    if not callable(archive) or not _same_trade_date(
        getattr(auction, "trading_date", None), trade_date
    ):
        return
    previous_open_volume = (
        observation.previous_open_volume
        if observation is not None
        else _previous_open_volume(client, code, trade_date)
    )
    try:
        archive(
            trade_date,
            symbol,
            auction,
            observation,
            previous_open_volume=previous_open_volume,
        )
    except Exception as exc:
        logger.warning("竞价原始文件写入失败 %s %s: %s", trade_date, symbol, exc)


def _observation_from_auction(
    client: object, symbol: str, code: str, trade_date: str, auction: object
) -> AuctionSnatchObservation | None:
    if not _same_trade_date(getattr(auction, "trading_date", None), trade_date):
        return None
    current_match = getattr(auction, "snapshot_0925", None)
    if current_match is None:
        return None

    all_points, minute_resolution = _auction_points(auction)
    all_points = sorted(all_points, key=_point_time_seconds)
    tail_start = (
        _MINUTE_LAST_MOMENT_START_SECONDS if minute_resolution else _LAST_MOMENT_START_SECONDS
    )
    points = [
        point
        for point in all_points
        if tail_start <= _point_time_seconds(point) < _AUCTION_END_SECONDS
    ]
    if not points and minute_resolution:
        points = [
            point
            for point in all_points
            if _AUCTION_START_SECONDS <= _point_time_seconds(point) < _AUCTION_END_SECONDS
        ]
    if not points:
        return None
    previous = max(points, key=_point_time_seconds)
    open_price = float(current_match.price)
    previous_price = float(previous.price)
    if open_price <= 0 or previous_price <= 0:
        return None
    price_base = getattr(auction, "pre_close_price", None)
    pre_close = float(price_base) if price_base not in (None, 0) else None
    open_change_pct = (
        round((open_price - pre_close) / pre_close * 100, 4)
        if pre_close is not None and pre_close > 0
        else None
    )
    previous_open_volume = _previous_open_volume(client, code, trade_date)
    auction_volume_ratio = (
        float(current_match.volume) / previous_open_volume
        if current_match.volume is not None and previous_open_volume not in (None, 0)
        else None
    )
    previous_time = getattr(previous, "time_label", None) or ""
    return AuctionSnatchObservation(
        symbol=symbol,
        open_price=open_price,
        open_change_pct=open_change_pct,
        open_volume=float(current_match.volume) if current_match.volume is not None else None,
        open_amount=round(float(current_match.trade_amount_yuan), 2),
        previous_price=previous_price,
        previous_time=previous_time,
        last_second_pct=round((open_price - previous_price) / previous_price * 100, 4),
        valid_raise_count=_count_valid_raises(all_points, open_price=open_price),
        previous_open_volume=previous_open_volume,
        auction_volume_ratio=(
            round(auction_volume_ratio, 4) if auction_volume_ratio is not None else None
        ),
    )


def _count_valid_raises(points: list[object], *, open_price: float | None = None) -> int:
    """09:20-09:25 抬价事件：30 秒内跌破节点作废，再抬确认上次并重启；含 09:25 撮合。"""
    series = [
        (_point_time_seconds(point), float(point.price))
        for point in points
        if _AUCTION_START_SECONDS <= _point_time_seconds(point) < _AUCTION_END_SECONDS
        and float(point.price) > 0
    ]
    if open_price is not None and open_price > 0:
        if series and series[-1][0] == _AUCTION_END_SECONDS:
            series[-1] = (_AUCTION_END_SECONDS, float(open_price))
        else:
            series.append((_AUCTION_END_SECONDS, float(open_price)))
    if len(series) < 2:
        return 0

    count = 0
    last_price = series[0][1]
    pending_price: float | None = None
    pending_time: int | None = None

    for time_seconds, price in series[1:]:
        if (
            pending_price is not None
            and pending_time is not None
            and time_seconds - pending_time >= _RAISE_WINDOW_SECONDS
        ):
            count += 1
            pending_price = None
            pending_time = None
        if pending_price is not None:
            if price < pending_price:
                pending_price = None
                pending_time = None
            elif price > pending_price:
                count += 1
                pending_price = price
                pending_time = time_seconds
        elif price > last_price:
            pending_price = price
            pending_time = time_seconds
        last_price = price

    if pending_price is not None:
        count += 1
    return count


def _is_live_auction_session(trade_date: str, now: datetime | None = None) -> bool:
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    else:
        current = current.astimezone(SHANGHAI)
    # 通达信当日秒级竞价会保留到下一个交易日切换前，收盘后仍应走 live，不能改历史成交。
    return current.date().isoformat() == trade_date


def _same_trade_date(value: object, trade_date: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if len(text) >= 10 and text[4] == "-":
        return text[:10] == trade_date
    compact = trade_date.replace("-", "")
    return text.replace("-", "")[:8] == compact


def _auction_points(auction: object) -> tuple[list[object], bool]:
    series = getattr(auction, "series", None)
    points = list(getattr(series, "points", ()) or ())
    if points:
        return points, False
    return list(getattr(auction, "auction_records", ()) or ()), True


def _point_time_seconds(point: object) -> int:
    seconds = getattr(point, "time_seconds", None)
    if seconds is not None:
        return int(seconds)
    minutes = getattr(point, "time_minutes", None)
    if minutes is not None:
        return int(minutes) * 60
    return -1


def _previous_open_volume(client: object, code: str, trade_date: str) -> float | None:
    previous_date = _previous_open_date(trade_date)
    try:
        auction = client.helpers.auction_data(
            code,
            previous_date,
            include_series=False,
            include_quote=False,
        )
    except Exception:
        return None
    snapshot = getattr(auction, "snapshot_0925", None)
    volume = getattr(snapshot, "volume", None)
    return float(volume) if volume not in (None, 0) else None


def _previous_open_date(trade_date: str) -> str:
    cursor = date.fromisoformat(trade_date) - timedelta(days=1)
    while not is_open_session(cursor):
        cursor -= timedelta(days=1)
    return cursor.isoformat()


def _eltdx_code(symbol: str) -> str:
    code, _, exchange = symbol.strip().upper().partition(".")
    if exchange == "SH":
        return f"sh{code}"
    if exchange == "BJ":
        return f"bj{code}"
    return f"sz{code}"
