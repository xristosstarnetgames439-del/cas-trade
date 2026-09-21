from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from app.models import AuctionSnapshotItem, AuctionSnapshotResponse

_BUSY_TIMEOUT_MS = 30_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_runs (
  run_id TEXT PRIMARY KEY,
  strategy_id TEXT NOT NULL,
  strategy_version INTEGER NOT NULL,
  trade_date TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  exact_conditions_json TEXT,
  item_count INTEGER NOT NULL,
  is_latest INTEGER NOT NULL,
  is_full_pool INTEGER NOT NULL DEFAULT 0,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS strategy_runs_latest_idx
  ON strategy_runs(strategy_id, trade_date, is_latest);

CREATE TABLE IF NOT EXISTS strategy_run_items (
  run_id TEXT NOT NULL,
  rank INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  name TEXT,
  close_price REAL,
  open_gap_pct REAL,
  last_second_pct REAL,
  valid_raise_count INTEGER,
  limit_up_pattern TEXT,
  auction_volume_ratio REAL,
  item_json TEXT NOT NULL,
  PRIMARY KEY (run_id, symbol),
  FOREIGN KEY (run_id) REFERENCES strategy_runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS strategy_run_items_symbol_idx
  ON strategy_run_items(symbol, run_id);
"""


class StrategyHistoryStore:
    """策略筛选结果历史：完整 JSON + 按票拆行，含当日收盘价。"""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "history.sqlite3"
        self._ensure_schema()

    def save(
        self,
        *,
        strategy_id: str,
        strategy_version: int,
        snapshot: AuctionSnapshotResponse,
        exact_conditions: list[int] | None,
        is_full_pool: bool = False,
    ) -> None:
        if not snapshot.trade_date:
            return
        run_id = uuid4().hex
        payload = snapshot.model_dump_json()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE strategy_runs
                SET is_latest = 0
                WHERE strategy_id = ? AND trade_date = ? AND is_latest = 1
                """,
                (strategy_id, snapshot.trade_date),
            )
            connection.execute(
                """
                INSERT INTO strategy_runs (
                  run_id, strategy_id, strategy_version, trade_date, generated_at,
                  exact_conditions_json, item_count, is_latest, payload_json, is_full_pool
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    run_id,
                    strategy_id,
                    strategy_version,
                    snapshot.trade_date,
                    snapshot.generated_at,
                    json.dumps(exact_conditions, ensure_ascii=False),
                    len(snapshot.items),
                    payload,
                    int(is_full_pool),
                ),
            )
            connection.executemany(
                """
                INSERT INTO strategy_run_items (
                  run_id, rank, symbol, name, close_price, open_gap_pct, last_second_pct,
                  valid_raise_count, limit_up_pattern, auction_volume_ratio, item_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_item_row(run_id, rank, item) for rank, item in enumerate(snapshot.items, start=1)],
            )

    def load_latest(
        self, strategy_id: str, trade_date: str, *, full_pool_only: bool = False
    ) -> AuctionSnapshotResponse | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM strategy_runs
                WHERE strategy_id = ? AND trade_date = ? AND (? = 0 OR is_full_pool = 1)
                ORDER BY
                  is_full_pool DESC,
                  CASE exact_conditions_json WHEN '[]' THEN 0 ELSE 1 END,
                  is_latest DESC,
                  generated_at DESC, rowid DESC
                LIMIT 1
                """,
                (strategy_id, trade_date, int(full_pool_only)),
            ).fetchone()
        if row is None:
            return None
        try:
            snapshot = AuctionSnapshotResponse.model_validate_json(row["payload_json"])
        except Exception:
            return None
        return snapshot.model_copy(update={"snapshot_status": "cached"}, deep=True)

    def load_auction_metrics(
        self, strategy_id: str, trade_date: str
    ) -> list[AuctionSnapshotItem]:
        """按股票读取历史上字段最完整的一条竞价结果，用于补全原始 JSON。"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT items.item_json
                FROM strategy_run_items AS items
                JOIN strategy_runs AS runs ON runs.run_id = items.run_id
                WHERE runs.strategy_id = ? AND runs.trade_date = ?
                ORDER BY
                  items.valid_raise_count IS NOT NULL DESC,
                  items.last_second_pct IS NOT NULL DESC,
                  runs.generated_at DESC,
                  items.rank
                """,
                (strategy_id, trade_date),
            ).fetchall()
        items: list[AuctionSnapshotItem] = []
        seen: set[str] = set()
        for row in rows:
            try:
                item = AuctionSnapshotItem.model_validate_json(row["item_json"])
            except Exception:
                continue
            if item.symbol in seen:
                continue
            seen.add(item.symbol)
            items.append(item)
        return items

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(strategy_runs)")}
            if "is_full_pool" not in columns:
                # 旧记录可能已按精确条件或 100 条上限裁剪，不能当作完整候选池。
                connection.execute(
                    "ALTER TABLE strategy_runs ADD COLUMN is_full_pool INTEGER NOT NULL DEFAULT 0"
                )

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=_BUSY_TIMEOUT_MS / 1_000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _item_row(run_id: str, rank: int, item: AuctionSnapshotItem) -> tuple[object, ...]:
    return (
        run_id,
        rank,
        item.symbol,
        item.name,
        item.close_price,
        item.open_gap_pct,
        item.last_second_pct,
        item.valid_raise_count,
        item.limit_up_pattern,
        item.auction_volume_ratio,
        item.model_dump_json(),
    )
