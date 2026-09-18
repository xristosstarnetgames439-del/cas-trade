from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.compat import _candidate_provider
from app.config import get_settings
from app.models import StrongStockDataUnavailable
from app.providers.eltdx_auction import EltdxAuctionProvider
from app.services.strategy_manager import StrategyManager

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
    try:
        datetime.strptime(trade_date, "%Y-%m-%d")
        selected_exact = _parse_exact_conditions(exact_conditions)
        result = StrategyManager().run(
            strategy_id,
            _candidate_provider(),
            EltdxAuctionProvider(),
            trade_date=trade_date,
            data_dir=get_settings().data_dir,
            limit=max(1, min(limit, 100)),
            refresh=refresh,
            exact_conditions=selected_exact,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="策略不存在") from exc
    except StrongStockDataUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return result.model_dump(mode="json")


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
