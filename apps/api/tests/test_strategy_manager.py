from __future__ import annotations

import pytest

from app.models import StrongStockCandidate, StrongStockDataUnavailable
from app.routers.strategies import _parse_exact_conditions, get_strategy_run
from app.services.auction_snatch import (
    AuctionSnatchObservation,
    AuctionSnatchScan,
)
from app.services.strategy_history_store import StrategyHistoryStore
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
    history = StrategyHistoryStore(tmp_path / "data")
    stored = history.load_latest("personal_strategy_1", "2026-08-14")
    assert stored is not None
    assert [item.symbol for item in stored.items] == ["000802.SZ"]

    with pytest.raises(ValueError, match="精确筛选条件不存在"):
        manager.run(
            "personal_strategy_1",
            _CandidateProvider(),
            auction_provider,
            trade_date="2026-08-14",
            data_dir=tmp_path / "data",
            exact_conditions=[0],
        )


def test_manager_saves_history_for_exact_filters_and_empty_pool(tmp_path) -> None:
    strategies_dir = tmp_path / "strategies"
    manager = StrategyManager(strategies_dir)
    manager.create(
        title="空池策略",
        description="用于确认空结果也会入库",
        rules={
            "require_last_second_price_up": True,
            "recent_limit_up_days": 3,
            "min_open_gap_pct": -2,
            "min_pattern_days": 0,
            "min_board_count": 0,
            "sort_by": "days_boards",
        },
    )

    class _FlatAuction(_AuctionProvider):
        def scan(self, symbols: list[str], *, trade_date: str):
            scan = super().scan(symbols, trade_date=trade_date)
            observation = scan.observations["000802.SZ"]
            scan.observations["000802.SZ"] = AuctionSnatchObservation(
                symbol=observation.symbol,
                open_price=observation.open_price,
                open_change_pct=observation.open_change_pct,
                open_volume=observation.open_volume,
                open_amount=observation.open_amount,
                previous_price=observation.open_price,
                previous_time=observation.previous_time,
                last_second_pct=0,
            )
            return scan

    result = manager.run(
        "personal_strategy_1",
        _CandidateProvider(),
        _FlatAuction(),
        trade_date="2026-08-14",
        data_dir=tmp_path / "data",
        exact_conditions=[],
    )
    assert result.items == []
    stored = StrategyHistoryStore(tmp_path / "data").load_latest(
        "personal_strategy_1", "2026-08-14"
    )
    assert stored is not None
    assert stored.items == []


def test_parse_exact_conditions_distinguishes_default_and_empty_selection() -> None:
    assert _parse_exact_conditions(None) is None
    assert _parse_exact_conditions("") == []
    assert _parse_exact_conditions("0,2,2") == [0, 2]


def test_full_pool_survives_more_than_100_items_and_views_only_read_database(tmp_path, monkeypatch):
    class Candidates:
        source_name = "测试涨停池"

        def get_candidates(self, trade_date):
            return [StrongStockCandidate(
                symbol=f"{600000 + index}.SH", name=f"测试股票{index}", board_note="涨停日期: 20260813"
            ) for index in range(105)]

    class Auctions:
        source_name = "测试竞价"
        calls = 0

        def scan(self, symbols, *, trade_date):
            self.calls += 1
            return AuctionSnatchScan(attempted=len(symbols), observations={
                symbol: AuctionSnatchObservation(
                    symbol=symbol, open_price=10, open_change_pct=2, open_volume=200,
                    open_amount=2000, previous_price=9.9, previous_time="09:24:57",
                    last_second_pct=1, valid_raise_count=3 if index < 2 else None,
                    auction_volume_ratio=0.7 if index == 0 else 1.2,
                ) for index, symbol in enumerate(symbols)
            })

    manager = StrategyManager()
    auctions = Auctions()
    snapshot = manager.run(
        "auction_snatch", Candidates(), auctions, trade_date="2026-08-14",
        data_dir=tmp_path, limit=1, exact_conditions=[0, 1, 3], collect_all=True,
    )
    assert len(snapshot.items) == 105
    store = StrategyHistoryStore(tmp_path)
    assert len(store.load_latest("auction_snatch", "2026-08-14", full_pool_only=True).items) == 105
    monkeypatch.setattr("app.routers.strategies._strategy_data_dir", lambda: tmp_path)
    strict = get_strategy_run("auction_snatch", "2026-08-14", "0,1,3")
    assert len(strict["items"]) == 2
    relaxed = get_strategy_run("auction_snatch", "2026-08-14", "1,3")
    assert len(relaxed["items"]) == 105
    higher_volume = get_strategy_run("auction_snatch", "2026-08-14", "0,2")
    assert [item["symbol"] for item in higher_volume["items"]] == ["600001.SH"]
    assert len(get_strategy_run("auction_snatch", "2026-08-14", "")["items"]) == 105
    assert auctions.calls == 1
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM strategy_runs").fetchone()[0] == 1

    class Unavailable:
        def scan(self, symbols, *, trade_date):
            return AuctionSnatchScan(attempted=len(symbols), failed=len(symbols), observations={})

    with pytest.raises(StrongStockDataUnavailable, match="未覆盖"):
        manager.run("auction_snatch", Candidates(), Unavailable(), trade_date="2026-08-14",
                    data_dir=tmp_path, collect_all=True)
    assert len(get_strategy_run("auction_snatch", "2026-08-14", "")["items"]) == 105
