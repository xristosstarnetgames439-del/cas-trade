from __future__ import annotations

import pytest

from app.models import StrongStockCandidate
from app.routers.strategies import _parse_exact_conditions
from app.services.auction_snatch import (
    AuctionSnatchObservation,
    AuctionSnatchScan,
)
from app.services.strategy_manager import StrategyManager


class _CandidateProvider:
    source_name = "测试涨停池"

    def get_candidates(self, trade_date: str):
        assert trade_date == "20260814"
        return [
            StrongStockCandidate(
                symbol="000802.SZ",
                name="北京文化",
                board_note="涨停日期: 20260813,20260811; 涨停统计: 3/2; 连板数: 2",
            )
        ]


class _AuctionProvider:
    source_name = "测试 eltdx"

    def __init__(self) -> None:
        self.calls = 0

    def scan(self, symbols: list[str], *, trade_date: str):
        self.calls += 1
        return AuctionSnatchScan(
            attempted=1,
            observations={
                "000802.SZ": AuctionSnatchObservation(
                    symbol="000802.SZ",
                    open_price=6.2,
                    open_change_pct=-0.96,
                    open_volume=100,
                    open_amount=620,
                    previous_price=5.85,
                    previous_time="09:24:57",
                    last_second_pct=5.9829,
                )
            },
        )


def test_manager_creates_discovers_and_runs_strategy(tmp_path) -> None:
    strategies_dir = tmp_path / "strategies"
    manager = StrategyManager(strategies_dir)
    created = manager.create(
        title="个人竞价策略",
        description="至少三天两板",
        rules={
            "require_last_second_price_up": True,
            "recent_limit_up_days": 3,
            "min_open_gap_pct": -2,
            "min_pattern_days": 3,
            "min_board_count": 2,
            "sort_by": "days_boards",
        },
    )

    assert list(created)[:5] == [
        "title",
        "path",
        "description",
        "conditions",
        "exact_conditions",
    ]
    assert created["path"] == "app/strategies/personal_strategy_1.py"
    assert created["conditions"]["data"]
    assert created["exact_conditions"] == {"data": []}
    assert manager.list()[0]["title"] == "个人竞价策略"

    auction_provider = _AuctionProvider()
    result = manager.run(
        "personal_strategy_1",
        _CandidateProvider(),
        auction_provider,
        trade_date="2026-08-14",
        data_dir=tmp_path / "data",
    )

    assert [item.symbol for item in result.items] == ["000802.SZ"]
    assert result.items[0].limit_up_pattern == "3天2板"
    assert auction_provider.calls == 1

    manager.run(
        "personal_strategy_1",
        _CandidateProvider(),
        auction_provider,
        trade_date="2026-08-14",
        data_dir=tmp_path / "data",
        refresh=True,
    )

    assert auction_provider.calls == 2

    with pytest.raises(ValueError, match="精确筛选条件不存在"):
        manager.run(
            "personal_strategy_1",
            _CandidateProvider(),
            auction_provider,
            trade_date="2026-08-14",
            data_dir=tmp_path / "data",
            exact_conditions=[0],
        )


def test_parse_exact_conditions_distinguishes_default_and_empty_selection() -> None:
    assert _parse_exact_conditions(None) is None
    assert _parse_exact_conditions("") == []
    assert _parse_exact_conditions("0,2,2") == [0, 2]
