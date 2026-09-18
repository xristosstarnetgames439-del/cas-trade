from __future__ import annotations

from pathlib import Path

from app.models import AuctionSnapshotResponse


class StrategyResultStore:
    """按策略、版本和交易日保存选股结果。"""

    def __init__(self, data_dir: Path, *, strategy_id: str, version: int = 1) -> None:
        self.root = data_dir / "strategy_results" / strategy_id / f"v{version}"

    def load(self, trade_date: str) -> AuctionSnapshotResponse | None:
        path = self._path(trade_date)
        if not path.exists():
            return None
        try:
            snapshot = AuctionSnapshotResponse.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return snapshot.model_copy(update={"snapshot_status": "cached"}, deep=True)

    def save(self, snapshot: AuctionSnapshotResponse) -> None:
        if not snapshot.trade_date or not snapshot.items:
            return
        path = self._path(snapshot.trade_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")

    def _path(self, trade_date: str) -> Path:
        return self.root / f"{trade_date}.json"
