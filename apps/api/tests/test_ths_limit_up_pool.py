from __future__ import annotations

from app.providers.ths_limit_up_pool import fetch_ths_limit_up_pool


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {
            "status_code": 0,
            "status_msg": "success",
            "data": {
                "info": [
                    {
                        "code": "603248",
                        "name": "锡华科技",
                        "latest": 24.57,
                        "high_days": "4天4板",
                        "reason_type": "次新股+风电齿轮箱+高端部件+全球客户",
                    }
                ]
            },
        }


class _Client:
    def __init__(self) -> None:
        self.params: dict[str, object] = {}

    def get(self, _url: str, **kwargs: object) -> _Response:
        self.params = dict(kwargs.get("params") or {})
        return _Response()


def test_fetch_ths_limit_up_pool_keeps_raw_fields() -> None:
    client = _Client()

    rows = fetch_ths_limit_up_pool("20260918", http_client=client)

    assert client.params["date"] == "20260918"
    assert client.params["limit"] == 200
    assert rows == [
        {
            "code": "603248",
            "name": "锡华科技",
            "latest": 24.57,
            "high_days": "4天4板",
            "reason_type": "次新股+风电齿轮箱+高端部件+全球客户",
        }
    ]
