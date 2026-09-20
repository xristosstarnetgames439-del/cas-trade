from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.compat import _background_job_store, _candidate_provider, app_state
from app.config import get_settings
from app.models import StrongStockDataUnavailable
from app.providers.eltdx_auction import EltdxAuctionProvider
from app.services.strategy_history_store import StrategyHistoryStore
from app.services.strategy_manager import StrategyManager
from app.services.strategy_raw_data import (
    LocalStrategyAuctionProvider,
    LocalStrategyCandidateProvider,
    StrategyRawPeriod,
    StrategyRawStore,
    run_strategy_raw_download,
    strategy_download_dates,
)
from app.services.trading_calendar import local_date

router = APIRouter()


class StrategyCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=300)
    require_last_second_price_up: bool = True
    recent_limit_up_days: int = Field(default=3, ge=1, le=20)
    min_open_gap_pct: float = Field(default=-2, ge=-20, le=20)
    min_pattern_days: int = Field(default=0, ge=0, le=20)
    min_board_count: int = Field(default=0, ge=0, le=20)
    sort_by: Literal["days_boards", "open_gap", "last_second_pct"] = "days_boards"


@router.get("/api/strategies")
def get_strategies() -> dict[str, object]:
    # 接口契约直接对应策略文件：title/path/description/conditions.data/exact_conditions.data。
    return {"items": StrategyManager().list()}


@router.post("/api/strategies")
def create_strategy(request: StrategyCreateRequest) -> dict[str, object]:
    rules = request.model_dump(exclude={"title", "description"})
    try:
        return StrategyManager().create(
            title=request.title,
            description=request.description,
            rules=rules,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/strategies/{strategy_id}/run")
def run_strategy(
    strategy_id: str,
    trade_date: str,
    limit: int = 100,
    refresh: bool = True,
    exact_conditions: str | None = None,
) -> dict[str, object]:
    historical = False
    try:
        datetime.strptime(trade_date, "%Y-%m-%d")
        if date.fromisoformat(trade_date) > local_date():
            raise ValueError("不能运行未来交易日的策略")
        selected_exact = _parse_exact_conditions(exact_conditions)
        manager = StrategyManager()
        metadata = manager.definition(strategy_id)
        data_dir = _strategy_data_dir()
        historical = trade_date != local_date().isoformat()
        if historical:
            rules = dict(metadata.get("rules") or {})
            lookback_days = max(1, int(rules.get("recent_limit_up_days", 3)))
            required = _selected_requirements(metadata, selected_exact)
            raw_store = StrategyRawStore(data_dir)
            candidate_provider = LocalStrategyCandidateProvider(
                raw_store, lookback_days=lookback_days
            )
            auction_provider = LocalStrategyAuctionProvider(
                raw_store,
                require_preopen=bool(rules.get("require_last_second_price_up", True)),
                require_seconds="seconds" in required,
            )
        else:
            candidate_provider = _candidate_provider()
            auction_provider = EltdxAuctionProvider(
                archive_store=StrategyRawStore(data_dir)
            )
        result = manager.run(
            strategy_id,
            candidate_provider,
            auction_provider,
            trade_date=trade_date,
            data_dir=data_dir,
            limit=max(1, min(limit, 100)),
            refresh=refresh,
            exact_conditions=selected_exact,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="策略不存在") from exc
    except StrongStockDataUnavailable as exc:
        raise HTTPException(status_code=409 if historical else 503, detail=str(exc)) from exc
    return result.model_dump(mode="json")


@router.get("/api/strategies/{strategy_id}/runs/{trade_date}")
def get_strategy_run(strategy_id: str, trade_date: str) -> dict[str, object]:
    try:
        datetime.strptime(trade_date, "%Y-%m-%d")
        StrategyManager()._path(strategy_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="策略不存在") from exc
    stored = StrategyHistoryStore(_strategy_data_dir()).load_latest(strategy_id, trade_date)
    if stored is None:
        raise HTTPException(status_code=404, detail="暂无该日筛选记录")
    return stored.model_dump(mode="json")


def _parse_exact_conditions(value: str | None) -> list[int] | None:
    if value is None:
        return None
    if not value:
        return []
    try:
        selected = [int(item) for item in value.split(",")]
    except ValueError as exc:
        raise ValueError("精确筛选条件格式不正确") from exc
    if any(item < 0 for item in selected):
        raise ValueError("精确筛选条件格式不正确")
    return list(dict.fromkeys(selected))


@router.post("/api/strategies/{strategy_id}/raw-downloads")
def create_strategy_raw_download(
    strategy_id: str,
    period: StrategyRawPeriod,
) -> dict[str, object]:
    try:
        metadata = StrategyManager().definition(strategy_id)
        rules = dict(metadata.get("rules") or {})
        lookback_days = max(1, int(rules.get("recent_limit_up_days", 3)))
        symbol_prefixes = tuple(
            str(value) for value in rules.get("symbol_prefixes", [])
        )
        dates = strategy_download_dates(period)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="策略不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    jobs = _background_job_store()
    active = jobs.get_active("strategy_raw_download")
    if active is not None:
        return active.model_dump(mode="json")
    job = jobs.create_transient_job(
        "strategy_raw_download",
        lambda progress, should_cancel: run_strategy_raw_download(
            _strategy_data_dir(),
            period,
            lookback_days=lookback_days,
            symbol_prefixes=symbol_prefixes,
            progress=progress,
            should_cancel=should_cancel,
        ),
        running_message=f"{metadata['title']}历史数据下载中",
        success_message=f"{metadata['title']}历史数据下载完成",
        progress_total=max(1, len(dates)),
    )
    return job.model_dump(mode="json")


@router.get("/api/strategies/{strategy_id}/raw-downloads/{job_id}")
def get_strategy_raw_download(strategy_id: str, job_id: str) -> dict[str, object]:
    try:
        StrategyManager().definition(strategy_id)
        job = _background_job_store().get(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="策略不存在") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="历史数据下载任务不存在") from exc
    if job.type != "strategy_raw_download":
        raise HTTPException(status_code=404, detail="历史数据下载任务不存在")
    return job.model_dump(mode="json")


@router.post("/api/strategies/{strategy_id}/raw-downloads/{job_id}/cancel")
def cancel_strategy_raw_download(strategy_id: str, job_id: str) -> dict[str, object]:
    try:
        StrategyManager().definition(strategy_id)
        job = _background_job_store().get(job_id)
        if job.type != "strategy_raw_download":
            raise KeyError(job_id)
        canceled = _background_job_store().cancel(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="策略不存在") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="历史数据下载任务不存在") from exc
    return canceled.model_dump(mode="json")


def _selected_requirements(
    metadata: dict[str, object], selected_exact: list[int] | None
) -> set[str]:
    configured = dict(metadata.get("exact_conditions") or {}).get("data") or []
    selected = (
        {
            index
            for index, condition in enumerate(configured)
            if isinstance(condition, dict) and condition.get("isselect") is True
        }
        if selected_exact is None
        else set(selected_exact)
    )
    return {
        str(condition["requires"])
        for index, condition in enumerate(configured)
        if index in selected
        and isinstance(condition, dict)
        and isinstance(condition.get("requires"), str)
    }


def _strategy_data_dir() -> Path:
    return Path(getattr(app_state().state, "runs_dir", get_settings().data_dir))
