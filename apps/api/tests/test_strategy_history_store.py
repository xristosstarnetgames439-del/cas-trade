from __future__ import annotations

from fastapi import HTTPException

from app.config import Settings
from app.models import AuctionSnapshotItem, AuctionSnapshotResponse
from app.routers.strategies import get_strategy_run
from app.services.strategy_history_store import StrategyHistoryStore


def test_history_store_keeps_empty_pool_and_latest_run(tmp_path) -> None:
    store = StrategyHistoryStore(tmp_path)
    empty = AuctionSnapshotResponse(trade_date="2026-09-18")
    store.save(
        strategy_id="auction_snatch",
        strategy_version=5,
        snapshot=empty,
        exact_conditions=[0, 1, 3],
    )

    loaded = store.load_latest("auction_snatch", "2026-09-18")
    assert loaded is not None
    assert loaded.items == []
    assert loaded.snapshot_status == "cached"

    filled = AuctionSnapshotResponse(
        trade_date="2026-09-18",
        items=[
            AuctionSnapshotItem(symbol="000993.SZ", name="闽东电力", close_price=18.30),
        ],
    )
    store.save(
        strategy_id="auction_snatch",
        strategy_version=5,
        snapshot=filled,
        exact_conditions=[0, 1, 3],
    )
    latest = store.load_latest("auction_snatch", "2026-09-18")
    assert latest is not None
    assert latest.items[0].close_price == 18.3
    assert latest.items[0].name == "闽东电力"


def test_get_strategy_run_reads_latest_or_404(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.routers.strategies.get_settings",
        lambda: Settings(data_dir=tmp_path),
    )
    try:
        get_strategy_run("auction_snatch", "2026-09-18")
        raise AssertionError("missing run should 404")
    except HTTPException as exc:
        assert exc.status_code == 404

    StrategyHistoryStore(tmp_path).save(
        strategy_id="auction_snatch",
        strategy_version=5,
        snapshot=AuctionSnapshotResponse(
            trade_date="2026-09-18",
            items=[AuctionSnapshotItem(symbol="000993.SZ", close_price=18.3)],
        ),
        exact_conditions=[0, 1, 3],
    )
    payload = get_strategy_run("auction_snatch", "2026-09-18")
    assert payload["snapshot_status"] == "cached"
    assert payload["items"][0]["close_price"] == 18.3


def test_history_store_loads_richest_auction_metrics_even_when_latest_is_sparse(
    tmp_path,
) -> None:
    store = StrategyHistoryStore(tmp_path)
    store.save(
        strategy_id="auction_snatch",
        strategy_version=5,
        snapshot=AuctionSnapshotResponse(
            trade_date="2026-09-18",
            items=[
                AuctionSnapshotItem(
                    symbol="000993.SZ",
                    last_price=18.3,
                    last_second_pct=2.81,
                    valid_raise_count=3,
                    auction_volume_ratio=0.7,
                )
            ],
        ),
        exact_conditions=[0, 1, 3],
    )
    store.save(
        strategy_id="auction_snatch",
        strategy_version=7,
        snapshot=AuctionSnapshotResponse(
            trade_date="2026-09-18",
            items=[
                AuctionSnapshotItem(
                    symbol="000993.SZ",
                    last_price=18.3,
                    auction_volume_ratio=0.7,
                )
            ],
        ),
        exact_conditions=[3],
    )

    items = store.load_auction_metrics("auction_snatch", "2026-09-18")

    assert len(items) == 1
    assert items[0].valid_raise_count == 3
    assert items[0].last_second_pct == 2.81
