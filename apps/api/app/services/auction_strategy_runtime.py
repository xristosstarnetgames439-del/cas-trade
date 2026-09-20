from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Callable, Protocol

from app.models import (
    AuctionSnapshotItem,
    AuctionSnapshotMetrics,
    AuctionSnapshotResponse,
    StrongStockCandidate,
    StrongStockSourceStatus,
)
from app.services.auction_snatch import (
    AuctionSnatchObservation,
    AuctionSnatchProvider,
    is_limit_up_close,
    limit_up_ratio,
)
from app.services.trading_calendar import is_open_session

_DATE_PATTERN = re.compile(r"(?<!\d)(20\d{6})(?!\d)")
_BOARD_LOOKBACK_DAYS = 10
_MAX_INTERNAL_BREAK_DAYS = 1

ExactMatcher = Callable[[AuctionSnapshotItem, set[int]], bool]


class CandidateProvider(Protocol):
    source_name: str

    def get_candidates(self, trade_date: str) -> list[StrongStockCandidate]: ...


def run_auction_rules(
    strategy: dict[str, object],
    candidate_provider: CandidateProvider,
    auction_provider: AuctionSnatchProvider,
    *,
    trade_date: str,
    limit: int = 100,
    exact_conditions: list[int] | None = None,
    exact_matcher: ExactMatcher | None = None,
    kline_provider: object | None = None,
) -> AuctionSnapshotResponse:
    """执行单个策略文件声明的竞价规则。"""
    rules = dict(strategy.get("rules") or {})
    recent_days = max(1, int(rules.get("recent_limit_up_days", 3)))
    provider_date = trade_date.replace("-", "")
    candidates = recent_limit_up_candidates(
        candidate_provider.get_candidates(provider_date),
        trade_date=trade_date,
        days=recent_days,
    )
    symbol_prefixes = tuple(str(value) for value in rules.get("symbol_prefixes", []))
    if symbol_prefixes:
        candidates = [
            candidate for candidate in candidates if candidate.symbol.startswith(symbol_prefixes)
        ]
    scan = auction_provider.scan(
        [candidate.symbol for candidate in candidates],
        trade_date=trade_date,
    )
    configured_exact = dict(strategy.get("exact_conditions") or {}).get("data") or []
    selected_exact = (
        {
            index
            for index, condition in enumerate(configured_exact)
            if isinstance(condition, dict) and condition.get("isselect") is True
        }
        if exact_conditions is None
        else set(exact_conditions)
    )
    items = []
    for candidate in candidates:
        observation = scan.observations.get(candidate.symbol)
        if observation is None:
            continue
        item = _strategy_item(
            candidate,
            observation,
            trade_date=trade_date,
            recent_days=recent_days,
            kline_provider=kline_provider,
        )
        if _matches(item, rules) and (exact_matcher is None or exact_matcher(item, selected_exact)):
            items.append(item)
    items.sort(
        key=lambda item: _sort_key(item, str(rules.get("sort_by", "days_boards"))), reverse=True
    )
    items = items[: max(1, min(limit, 100))]
    return AuctionSnapshotResponse(
        trade_date=trade_date,
        session="closed",
        snapshot_status="fresh",
        metrics=AuctionSnapshotMetrics(
            candidate_count=len(items),
            strong_high_open_count=sum(1 for item in items if (item.open_gap_pct or 0) >= 3),
            high_risk_count=0,
            total_turnover_cny=round(sum(item.turnover_cny or 0 for item in items), 2),
        ),
        items=items,
        source_status=[
            StrongStockSourceStatus(
                source=getattr(candidate_provider, "source_name", "涨停候选池"),
                status="success",
                detail=f"前 {recent_days} 个交易日涨停候选 {len(candidates)} 只",
            ),
            StrongStockSourceStatus(
                source=auction_provider.source_name,
                status="success" if scan.observations else "failed",
                detail=(
                    f"逐只核验 {scan.attempted} 只，策略命中 {len(items)} 只"
                    + (f"，{scan.failed} 只读取失败" if scan.failed else "")
                ),
            ),
        ],
    )


def recent_limit_up_candidates(
    candidates: list[StrongStockCandidate],
    *,
    trade_date: str,
    days: int = 3,
) -> list[StrongStockCandidate]:
    target_dates = set(_previous_open_dates(trade_date, days))
    return [
        candidate for candidate in candidates if _candidate_limit_up_dates(candidate) & target_dates
    ]


def _matches(item: AuctionSnapshotItem, rules: dict[str, object]) -> bool:
    if rules.get("require_last_second_price_up", True) and item.last_second_price_up is False:
        return False
    min_open_gap = float(rules.get("min_open_gap_pct", -100.0))
    if item.open_gap_pct is None or item.open_gap_pct < min_open_gap:
        return False
    if (item.limit_up_pattern_days or 0) < int(rules.get("min_pattern_days", 0)):
        return False
    return (item.limit_up_board_count or 0) >= int(rules.get("min_board_count", 0))


def _sort_key(item: AuctionSnapshotItem, sort_by: str) -> tuple[float, ...]:
    open_gap = item.open_gap_pct if item.open_gap_pct is not None else -100.0
    last_second = item.last_second_pct if item.last_second_pct is not None else -100.0
    if sort_by == "open_gap":
        return open_gap, item.limit_up_pattern_days or 0, item.limit_up_board_count or 0
    if sort_by == "last_second_pct":
        return last_second, open_gap, item.limit_up_board_count or 0
    return item.limit_up_pattern_days or 0, item.limit_up_board_count or 0, open_gap, last_second


def _strategy_item(
    candidate: StrongStockCandidate,
    observation: AuctionSnatchObservation,
    *,
    trade_date: str,
    recent_days: int,
    kline_provider: object | None = None,
) -> AuctionSnapshotItem:
    extra_dates, close_price = _kline_stats(
        kline_provider,
        candidate.symbol,
        candidate.name,
        trade_date=trade_date,
    )
    downloaded_stats = _downloaded_board_stats(candidate)
    pattern_days, board_count = downloaded_stats or _board_stats(
        candidate, trade_date=trade_date, extra_dates=extra_dates
    )
    pattern = f"{pattern_days}天{board_count}板" if pattern_days and board_count else None
    break_days = _break_days(
        candidate, trade_date=trade_date, days=recent_days, extra_dates=extra_dates
    )
    has_preopen = observation.last_second_pct is not None
    return AuctionSnapshotItem(
        symbol=candidate.symbol,
        name=candidate.name,
        industry=candidate.industry,
        last_price=observation.open_price,
        current_pct_change=observation.open_change_pct,
        open_gap_pct=observation.open_change_pct,
        turnover_cny=observation.open_amount,
        volume=observation.open_volume,
        auction_score=round(observation.last_second_pct or 0, 4),
        tier="strong_high_open" if (observation.open_change_pct or 0) >= 3 else "neutral",
        action_note=(
            f"最后一刻抬价，前 {recent_days} 个交易日有涨停，竞价位置符合策略。"
            if has_preopen
            else f"历史竞价仅有 09:25 撮合，按可用条件筛选；前 {recent_days} 个交易日有涨停。"
        ),
        signals=[
            *(["竞价最后一刻抬价"] if has_preopen else []),
            f"近{recent_days}日涨停",
            *([pattern] if pattern else []),
        ],
        quote_time="09:25:00",
        last_second_price_up=(observation.last_second_pct > 0 if has_preopen else None),
        last_second_pct=observation.last_second_pct,
        prev_node_price=observation.previous_price,
        prev_node_time=(f"{trade_date}T{observation.previous_time}" if has_preopen else None),
        limit_up_3d=recent_days == 3,
        limit_up_pattern_days=pattern_days,
        limit_up_board_count=board_count,
        limit_up_pattern=pattern,
        limit_up_break_days=break_days,
        valid_raise_count=observation.valid_raise_count,
        previous_auction_volume=observation.previous_open_volume,
        auction_volume_ratio=observation.auction_volume_ratio,
        close_price=close_price,
    )


def _board_stats(
    candidate: StrongStockCandidate,
    *,
    trade_date: str,
    extra_dates: set[str] | None = None,
) -> tuple[int, int]:
    """按竞价日前一交易日计算几天几板：连续涨停可夹 1 日断板，断板日计入天。"""
    limit_up_dates = _candidate_limit_up_dates(candidate) | (extra_dates or set())
    if not limit_up_dates:
        return 0, 0
    previous_days = _previous_open_dates(trade_date, _BOARD_LOOKBACK_DAYS)
    last_zt_index = next(
        (index for index, day in enumerate(previous_days) if day in limit_up_dates),
        None,
    )
    if last_zt_index is None:
        return 0, 0
    start_index = last_zt_index
    cursor = last_zt_index
    while True:
        next_zt = next(
            (
                index
                for index in range(cursor + 1, len(previous_days))
                if previous_days[index] in limit_up_dates
            ),
            None,
        )
        if next_zt is None or next_zt - cursor - 1 > _MAX_INTERNAL_BREAK_DAYS:
            break
        start_index = next_zt
        cursor = next_zt
    window = previous_days[: start_index + 1]
    boards = sum(1 for day in window if day in limit_up_dates)
    return len(window), boards


def _downloaded_board_stats(candidate: StrongStockCandidate) -> tuple[int, int] | None:
    """历史下载使用同花顺口径；当天实时候选没有此字段，继续走原算法。"""
    match = re.search(r"同花顺几天几板\s*[:：]\s*([^;；]+)", candidate.board_note or "")
    if match is None:
        return None
    pattern = match.group(1).strip()
    if pattern == "首板":
        return 1, 1
    parsed = re.fullmatch(r"(\d+)天(\d+)板", pattern)
    if parsed is None:
        return None
    return int(parsed.group(1)), int(parsed.group(2))


def _candidate_limit_up_dates(candidate: StrongStockCandidate) -> set[str]:
    values = [*candidate.limit_up_evidence, candidate.board_note or ""]
    return {match for value in values for match in _DATE_PATTERN.findall(value)}


def _kline_stats(
    provider: object | None,
    symbol: str,
    name: str | None,
    *,
    trade_date: str,
) -> tuple[set[str], float | None]:
    if provider is None or not symbol:
        return set(), None
    try:
        bars = provider.get_klines(symbol, count=50)
    except Exception:
        return set(), None
    trade_key = _as_date_key(trade_date)
    ratio = limit_up_ratio(symbol, name)
    ordered = sorted(
        (bar for bar in bars if getattr(bar, "date", None) and bar.close > 0),
        key=lambda bar: _as_date_key(bar.date),
    )
    dates: set[str] = set()
    close_price: float | None = None
    for index, bar in enumerate(ordered):
        day_key = _as_date_key(bar.date)
        if day_key == trade_key:
            close_price = float(bar.close)
        if index == 0 or day_key >= trade_key:
            continue
        prev_close = ordered[index - 1].close
        if prev_close > 0 and is_limit_up_close(bar.close, prev_close, ratio):
            dates.add(day_key)
    return dates, close_price


def _as_date_key(value: str) -> str:
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return text
    return date.fromisoformat(text[:10]).strftime("%Y%m%d")


def _break_days(
    candidate: StrongStockCandidate,
    *,
    trade_date: str,
    days: int,
    extra_dates: set[str] | None = None,
) -> int | None:
    """竞价前最近一次涨停之后，已经历的未涨停交易日数量。"""
    limit_up_dates = _candidate_limit_up_dates(candidate) | (extra_dates or set())
    for index, open_date in enumerate(_previous_open_dates(trade_date, days)):
        if open_date in limit_up_dates:
            return index
    return None


def _previous_open_dates(trade_date: str, days: int) -> list[str]:
    output: list[str] = []
    cursor = date.fromisoformat(trade_date) - timedelta(days=1)
    while len(output) < max(0, days):
        if is_open_session(cursor):
            output.append(cursor.strftime("%Y%m%d"))
        cursor -= timedelta(days=1)
    return output
