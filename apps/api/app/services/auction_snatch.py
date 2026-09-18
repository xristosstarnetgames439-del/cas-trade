"""竞价抢筹策略的数据支撑：涨停判定与并发批量查询。

竞价抢筹 = 集合竞价最后一刻价格被抬升（9:25 价格高于上一节点 09:24:50 价格）
且最近 3 个交易日内出现过涨停收盘。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from app.models import KlineBar

DEFAULT_KLINE_WORKERS = 12
LIMIT_UP_DAYS = 3

ST_LIMIT_UP_RATIO = 0.05
MAIN_BOARD_LIMIT_UP_RATIO = 0.10
CHINEXT_STAR_LIMIT_UP_RATIO = 0.20
BSE_LIMIT_UP_RATIO = 0.30


class KlineProvider(Protocol):
    def get_klines(self, symbol: str, count: int = 220) -> list[KlineBar]: ...


@dataclass(frozen=True, slots=True)
class AuctionSnatchObservation:
    symbol: str
    open_price: float
    open_change_pct: float | None
    open_volume: float | None
    open_amount: float | None
    previous_price: float
    previous_time: str
    last_second_pct: float
    valid_raise_count: int | None = None
    previous_open_volume: float | None = None
    auction_volume_ratio: float | None = None


@dataclass(frozen=True, slots=True)
class AuctionSnatchScan:
    observations: dict[str, AuctionSnatchObservation]
    attempted: int
    failed: int = 0


class AuctionSnatchProvider(Protocol):
    source_name: str

    def scan(self, symbols: list[str], *, trade_date: str) -> AuctionSnatchScan: ...


def limit_up_ratio(symbol: str, name: str | None = None) -> float:
    """按板块返回涨停幅度：创业板/科创板 20%，北交所 30%，主板 ST 5%，其余 10%。"""
    code = str(symbol).split(".", 1)[0]
    if code.startswith(("300", "301", "302", "688", "689")):
        return CHINEXT_STAR_LIMIT_UP_RATIO
    if code.startswith(("43", "83", "87", "92")):
        return BSE_LIMIT_UP_RATIO
    if "ST" in (name or "").upper():
        return ST_LIMIT_UP_RATIO
    return MAIN_BOARD_LIMIT_UP_RATIO


def is_limit_up_close(close: float, prev_close: float, ratio: float) -> bool:
    """收盘价是否封在按四舍五入计算出的涨停价上（容差 1e-6 消除浮点误差）。"""
    if prev_close <= 0:
        return False
    limit_price = _round_price(prev_close * (1 + ratio))
    return close + 1e-6 >= limit_price


def has_limit_up_within_days(
    bars: list[KlineBar],
    *,
    trade_date: str,
    days: int = LIMIT_UP_DAYS,
    symbol: str = "",
    name: str | None = None,
) -> bool:
    """trade_date 之前最近 days 个已收盘交易日中是否出现过涨停收盘。"""
    if days <= 0 or not bars:
        return False
    trade_key = _date_key(trade_date)
    ratio = limit_up_ratio(symbol, name)
    ordered = sorted(
        (bar for bar in bars if bar.date and bar.close > 0),
        key=lambda bar: _date_key(bar.date),
    )
    window = [bar for bar in ordered if _date_key(bar.date) < trade_key][-(days + 1) :]
    for index, bar in enumerate(window):
        if index == 0:
            continue
        prev_close = window[index - 1].close
        if prev_close <= 0:
            continue
        if is_limit_up_close(bar.close, prev_close, ratio):
            return True
    return False


def find_limit_up_3d_symbols(
    provider: KlineProvider,
    items: list[object],
    *,
    trade_date: str,
    days: int = LIMIT_UP_DAYS,
    workers: int = DEFAULT_KLINE_WORKERS,
) -> set[str]:
    """并发读取日 K，返回最近 days 个交易日内有涨停收盘的标的集合；单标的数据异常时跳过。"""
    rows = [(item.symbol, item.name) for item in items if getattr(item, "symbol", None)]
    if not rows:
        return set()

    limit_up_symbols: set[str] = set()
    max_workers = max(1, min(workers, len(rows)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _symbol_has_limit_up,
                provider,
                symbol,
                name,
                trade_date,
                days,
            ): symbol
            for symbol, name in rows
        }
        for future, symbol in ((future, futures[future]) for future in futures):
            try:
                if future.result():
                    limit_up_symbols.add(symbol)
            except Exception:
                continue
    return limit_up_symbols


def _symbol_has_limit_up(
    provider: KlineProvider,
    symbol: str,
    name: str | None,
    trade_date: str,
    days: int,
) -> bool:
    """单个标的的涨停判定；数据源异常视为不满足，不让单个失败拖垮整个批次。"""
    try:
        bars = provider.get_klines(symbol, count=max(days + 5, 10))
    except Exception:
        return False
    return has_limit_up_within_days(
        bars,
        trade_date=trade_date,
        days=days,
        symbol=symbol,
        name=name,
    )


def _date_key(value: str) -> str:
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return text
    return date.fromisoformat(text).strftime("%Y%m%d")


def _round_price(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
