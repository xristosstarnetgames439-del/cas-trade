from __future__ import annotations

import importlib.util
import pprint
import re
from pathlib import Path
from types import ModuleType

from app.models import AuctionSnapshotMetrics, AuctionSnapshotResponse, StrongStockSourceStatus
from app.services.auction_snatch import AuctionSnatchProvider
from app.services.strategy_history_store import StrategyHistoryStore
from app.services.strategy_result_store import StrategyResultStore

_STRATEGY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


class StrategyManager:
    """发现、创建并执行 app/strategies 下的一文件一策略。"""

    def __init__(self, strategies_dir: Path | None = None) -> None:
        self.strategies_dir = strategies_dir or Path(__file__).resolve().parents[1] / "strategies"

    def list(self) -> list[dict[str, object]]:
        strategies = []
        for path in sorted(self.strategies_dir.glob("*.py")):
            if path.name == "__init__.py" or path.name.startswith("_"):
                continue
            try:
                _, metadata = self._load(path)
            except Exception:
                continue
            strategies.append(metadata)
        return strategies

    def definition(self, strategy_id: str) -> dict[str, object]:
        """读取单个策略元数据，供运行时选择数据能力。"""
        _, metadata = self._load(self._path(strategy_id))
        return metadata

    def create(
        self,
        *,
        title: str,
        description: str,
        rules: dict[str, object],
    ) -> dict[str, object]:
        if not title.strip() or not description.strip():
            raise ValueError("策略标题和简介不能为空")
        strategy_id = self._next_id()
        conditions = _rule_descriptions(rules)
        metadata = {
            "title": title.strip(),
            "path": f"app/strategies/{strategy_id}.py",
            "description": description.strip(),
            "conditions": {"data": conditions},
            "exact_conditions": {"data": []},
            "id": strategy_id,
            "rules": rules,
            "status": "active",
            "version": 1,
        }
        source = (
            '"""由策略管理页面生成，可直接修改本文件扩展个人选股逻辑。"""\n\n'
            "from app.services.auction_strategy_runtime import run_auction_rules\n\n"
            f"STRATEGY = {pprint.pformat(metadata, sort_dicts=False, width=100)}\n\n\n"
            "def run(\n"
            "    candidate_provider, auction_provider, *, trade_date: str, limit: int = 100, exact_conditions=None\n"
            "):\n"
            "    return run_auction_rules(\n"
            "        STRATEGY, candidate_provider, auction_provider, trade_date=trade_date, limit=limit,\n"
            "        exact_conditions=exact_conditions\n"
            "    )\n"
        )
        self.strategies_dir.mkdir(parents=True, exist_ok=True)
        path = self.strategies_dir / f"{strategy_id}.py"
        with path.open("x", encoding="utf-8") as stream:
            stream.write(source)
        return metadata

    def run(
        self,
        strategy_id: str,
        candidate_provider: object,
        auction_provider: AuctionSnatchProvider,
        *,
        trade_date: str,
        data_dir: Path,
        limit: int = 100,
        refresh: bool = False,
        exact_conditions: list[int] | None = None,
        collect_all: bool = False,
    ) -> AuctionSnapshotResponse:
        path = self._path(strategy_id)
        module, metadata = self._load(path)
        if collect_all:
            # 运行阶段只应用基础条件，完整采集全部精确条件需要的字段。
            exact_conditions = []
        configured_exact = dict(metadata.get("exact_conditions") or {}).get("data") or []
        if exact_conditions is not None and any(
            index >= len(configured_exact) for index in exact_conditions
        ):
            raise ValueError("精确筛选条件不存在")
        version = int(metadata.get("version", 1))
        store = StrategyResultStore(data_dir, strategy_id=strategy_id, version=version)
        history = StrategyHistoryStore(data_dir)
        if not refresh and exact_conditions is None:
            stored = store.load(trade_date)
            if stored is not None:
                return stored.model_copy(update={"items": stored.items[:limit]}, deep=True)
        runner = getattr(module, "run", None)
        if not callable(runner):
            raise ValueError(f"策略 {strategy_id} 缺少 run 函数")
        result = runner(
            candidate_provider,
            auction_provider,
            trade_date=trade_date,
            limit=None if collect_all else limit,
            exact_conditions=exact_conditions,
        )
        if exact_conditions is None:
            store.save(result)
        history.save(
            strategy_id=strategy_id,
            strategy_version=version,
            snapshot=result,
            exact_conditions=exact_conditions,
            is_full_pool=collect_all,
        )
        return result

    def view(
        self,
        strategy_id: str,
        *,
        trade_date: str,
        data_dir: Path,
        exact_conditions: list[int] | None = None,
    ) -> AuctionSnapshotResponse | None:
        """仅从本地数据库读取完整候选，复用策略文件的精确条件；不访问行情源。"""
        module, metadata = self._load(self._path(strategy_id))
        configured = metadata["exact_conditions"]["data"]
        selected = (
            {index for index, condition in enumerate(configured) if condition["isselect"]}
            if exact_conditions is None
            else set(exact_conditions)
        )
        if any(index < 0 or index >= len(configured) for index in selected):
            raise ValueError("精确筛选条件不存在")
        matcher = getattr(module, "_matches_exact", None)
        if selected and not callable(matcher):
            raise ValueError("策略文件尚未实现精确筛选函数 _matches_exact")
        snapshot = StrategyHistoryStore(data_dir).load_latest(
            strategy_id, trade_date, full_pool_only=True
        )
        if snapshot is None:
            return None
        items = [item for item in snapshot.items if not selected or matcher(item, selected)]
        status = StrongStockSourceStatus(
            source="本地策略数据库",
            status="success",
            detail=f"候选池 {len(snapshot.items)} 只，按 {len(selected)} 项精确条件筛选，命中 {len(items)} 只",
        )
        return snapshot.model_copy(update={
            "items": items,
            "metrics": AuctionSnapshotMetrics(
                candidate_count=len(items),
                strong_high_open_count=sum(1 for item in items if (item.open_gap_pct or 0) >= 3),
                high_risk_count=sum(1 for item in items if item.tier == "high_risk"),
                total_turnover_cny=round(sum(item.turnover_cny or 0 for item in items), 2),
            ),
            "source_status": [*snapshot.source_status, status],
        }, deep=True)

    def _path(self, strategy_id: str) -> Path:
        if not _STRATEGY_ID_PATTERN.fullmatch(strategy_id):
            raise ValueError("策略编号格式不正确")
        path = self.strategies_dir / f"{strategy_id}.py"
        if not path.exists():
            raise FileNotFoundError(strategy_id)
        return path

    def _load(self, path: Path) -> tuple[ModuleType, dict[str, object]]:
        module_name = f"app.strategies.{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ValueError(f"无法加载策略文件 {path.name}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        metadata = getattr(module, "STRATEGY", None)
        if not isinstance(metadata, dict) or metadata.get("id") != path.stem:
            raise ValueError(f"策略文件 {path.name} 的 STRATEGY.id 不正确")
        _validate_metadata(path, metadata)
        return module, dict(metadata)

    def _next_id(self) -> str:
        number = 1
        while (self.strategies_dir / f"personal_strategy_{number}.py").exists():
            number += 1
        return f"personal_strategy_{number}"


def _validate_metadata(path: Path, metadata: dict[str, object]) -> None:
    expected_path = f"app/strategies/{path.name}"
    for field in ("title", "description"):
        if not isinstance(metadata.get(field), str) or not str(metadata[field]).strip():
            raise ValueError(f"策略文件 {path.name} 的 {field} 不能为空")
    if metadata.get("path") != expected_path:
        raise ValueError(f"策略文件 {path.name} 的 path 必须为 {expected_path}")
    conditions = metadata.get("conditions")
    condition_data = conditions.get("data") if isinstance(conditions, dict) else None
    if not isinstance(condition_data, list) or not all(
        isinstance(item, str) for item in condition_data
    ):
        raise ValueError(f"策略文件 {path.name} 的 conditions.data 必须为字符串列表")
    exact_conditions = metadata.get("exact_conditions")
    exact_data = exact_conditions.get("data") if isinstance(exact_conditions, dict) else None
    if not isinstance(exact_data, list) or not all(
        _valid_exact_condition(item) for item in exact_data
    ):
        raise ValueError(
            f"策略文件 {path.name} 的 exact_conditions.data 必须包含 label 和 isselect"
        )


def _valid_exact_condition(item: object) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("label"), str)
        and bool(item["label"].strip())
        and isinstance(item.get("isselect"), bool)
    )


def _rule_descriptions(rules: dict[str, object]) -> list[str]:
    conditions = []
    if rules.get("require_last_second_price_up", True):
        conditions.append("09:25 正式撮合价高于最后一个虚拟竞价价")
    conditions.append(f"前 {int(rules.get('recent_limit_up_days', 3))} 个交易日内有涨停")
    conditions.append(f"竞价开盘涨幅不低于 {float(rules.get('min_open_gap_pct', -2)):g}%")
    if int(rules.get("min_pattern_days", 0)) > 0:
        conditions.append(f"涨停统计天数不少于 {int(rules['min_pattern_days'])} 天")
    if int(rules.get("min_board_count", 0)) > 0:
        conditions.append(f"涨停板数不少于 {int(rules['min_board_count'])} 板")
    sort_labels = {
        "days_boards": "几天、几板、竞价涨幅",
        "open_gap": "竞价涨幅、几天、几板",
        "last_second_pct": "最后一刻抬价幅度、竞价涨幅",
    }
    conditions.append(f"排序：{sort_labels.get(str(rules.get('sort_by')), '几天、几板')}从高到低")
    return conditions
