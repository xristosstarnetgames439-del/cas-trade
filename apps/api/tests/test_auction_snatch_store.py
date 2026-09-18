from __future__ import annotations

from app.models import AuctionSnapshotItem, AuctionSnapshotResponse
from app.services.strategy_result_store import StrategyResultStore


def test_auction_snatch_store_round_trip(tmp_path) -> None:
    store = StrategyResultStore(tmp_path, strategy_id="auction_snatch")
    snapshot = AuctionSnapshotResponse(
        trade_date="2026-08-14",
        items=[AuctionSnapshotItem(symbol="000802.SZ", name="北京文化")],
    )

    store.save(snapshot)
    loaded = store.load("2026-08-14")

    assert loaded is not None
    assert loaded.snapshot_status == "cached"
    assert loaded.items[0].name == "北京文化"


def test_auction_snatch_store_ignores_empty_or_corrupt_data(tmp_path) -> None:
    store = StrategyResultStore(tmp_path, strategy_id="auction_snatch")
    store.save(AuctionSnapshotResponse(trade_date="2026-08-14"))
    assert store.load("2026-08-14") is None

    store.root.mkdir(parents=True)
    (store.root / "2026-08-14.json").write_text("broken", encoding="utf-8")
    assert store.load("2026-08-14") is None
