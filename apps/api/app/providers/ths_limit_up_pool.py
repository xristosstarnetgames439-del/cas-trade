from __future__ import annotations

from typing import Any

import httpx

from app.models import StrongStockDataUnavailable

THS_LIMIT_UP_POOL_SOURCE = "同花顺涨停揭秘"
THS_LIMIT_UP_POOL_URL = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
_THS_FIELDS = (
    "199112,10,9001,330323,330324,330325,9002,330329,"
    "133971,133970,1968584,3475914,9003,9004"
)
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def fetch_ths_limit_up_pool(
    trade_date: str,
    *,
    http_client: Any | None = None,
    timeout_seconds: float = 12,
) -> list[dict[str, object]]:
    """下载同花顺单日涨停揭秘原始行；trade_date=YYYYMMDD。"""
    if len(trade_date) != 8 or not trade_date.isdigit():
        raise ValueError("同花顺涨停池日期必须是 YYYYMMDD")
    client = http_client or httpx
    try:
        response = client.get(
            THS_LIMIT_UP_POOL_URL,
            params={
                "page": 1,
                "limit": 200,
                "field": _THS_FIELDS,
                "filter": "HS,GEM2STAR",
                "order_field": "330324",
                "order_type": "0",
                "date": trade_date,
            },
            headers={"User-Agent": _USER_AGENT},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise StrongStockDataUnavailable(
            f"同花顺涨停揭秘请求失败（{exc.__class__.__name__}）"
        ) from exc
    if not isinstance(payload, dict) or payload.get("status_code") not in (0, "0"):
        message = payload.get("status_msg") if isinstance(payload, dict) else None
        raise StrongStockDataUnavailable(f"同花顺涨停揭秘返回失败：{message or '响应异常'}")
    data = payload.get("data")
    rows = data.get("info") if isinstance(data, dict) else None
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise StrongStockDataUnavailable("同花顺涨停揭秘返回结构异常")
    return [dict(row) for row in rows if isinstance(row, dict)]
