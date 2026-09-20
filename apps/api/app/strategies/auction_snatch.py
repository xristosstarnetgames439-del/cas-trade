"""竞价抢筹：首个可独立执行的个人选股策略。"""

from __future__ import annotations

import os

from app.models import AuctionSnapshotItem, AuctionSnapshotResponse
from app.services.auction_snatch import AuctionSnatchProvider
from app.services.auction_strategy_runtime import run_auction_rules

STRATEGY = {
    "title": "竞价抢筹",
    "path": "app/strategies/auction_snatch.py",
    "description": "寻找集合竞价最后一刻被资金抬价、近期有涨停基础且竞价位置不过低的股票。",
    "conditions": {
        "data": [
            "仅筛选沪深主板股票（代码以 00、60 开头）",
            "09:25 正式撮合价高于 09:25 前最后一个虚拟竞价价",
            "前 3 个已完成交易日内至少出现过一次涨停",
            "竞价开盘涨幅不低于 -2%",
            "几天几板最多回看前 10 个交易日",
            "按几天、几板、竞价开盘涨幅依次从高到低排列",
        ]
    },
    "exact_conditions": {
        "data": [
            {
                "label": "09:20-09:25 至少 3 次有效抬价（30秒内跌破节点作废，再抬确认上次；含 09:25 撮合）",
                "isselect": True,
                "requires": "seconds",
            },
            {"label": "竞价前断板日最多 1 日", "isselect": True},
            {"label": "今日竞价量大于上一交易日竞价量", "isselect": False},
            {"label": "今日竞价量不低于上一交易日的 60%", "isselect": True},
        ]
    },
    "id": "auction_snatch",
    "rules": {
        "require_last_second_price_up": True,
        "symbol_prefixes": ["00", "60"],
        "recent_limit_up_days": 3,
        "min_open_gap_pct": -2.0,
        "min_pattern_days": 0,
        "min_board_count": 0,
        "sort_by": "days_boards",
    },
    "status": "active",
    "version": 7,
}


def run(
    candidate_provider: object,
    auction_provider: AuctionSnatchProvider,
    *,
    trade_date: str,
    limit: int = 100,
    exact_conditions: list[int] | None = None,
) -> AuctionSnapshotResponse:
    return run_auction_rules(
        STRATEGY,
        candidate_provider,
        auction_provider,
        trade_date=trade_date,
        limit=limit,
        exact_conditions=exact_conditions,
        exact_matcher=_matches_exact,
        kline_provider=getattr(auction_provider, "kline_provider", None) or _kline_provider(),
    )


def _kline_provider() -> object | None:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    try:
        from app.providers.eastmoney_kline import EastmoneyKlineProvider
    except Exception:
        return None
    return EastmoneyKlineProvider()


def _matches_exact(item: AuctionSnapshotItem, selected: set[int]) -> bool:
    if 0 in selected and (item.valid_raise_count or 0) < 3:
        return False
    if 1 in selected and (item.limit_up_break_days is None or item.limit_up_break_days > 1):
        return False
    if 2 in selected and (item.auction_volume_ratio is None or item.auction_volume_ratio <= 1):
        return False
    return 3 not in selected or (
        item.auction_volume_ratio is not None and item.auction_volume_ratio >= 0.6
    )
