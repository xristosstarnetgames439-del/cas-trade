from __future__ import annotations

from types import SimpleNamespace

from datetime import datetime
from zoneinfo import ZoneInfo

from app.providers.eltdx_auction import (
    _count_valid_raises,
    _eltdx_code,
    _is_live_auction_session,
    _load_observation,
)


class _Helpers:
    def __init__(self, *, same_session: bool = True) -> None:
        self.same_session = same_session
        self.current_match = SimpleNamespace(
            price=6.20,
            volume=188715,
            trade_amount_yuan=117003300,
        )

    def auction_data(self, code: str, date: str | None = None, **_kwargs):
        assert code == "sz000802"
        if date is not None:
            assert date == "2026-08-13"
            return SimpleNamespace(snapshot_0925=SimpleNamespace(volume=100000))
        return SimpleNamespace(
            trading_date="2026-08-14" if self.same_session else "2026-08-15",
            snapshot_0925=self.current_match,
            pre_close_price=6.26,
            series=SimpleNamespace(
                points=(
                    SimpleNamespace(
                        time_seconds=9 * 3600 + 24 * 60 + 30,
                        time_label="09:24:30",
                        price=5.63,
                    ),
                    SimpleNamespace(
                        time_seconds=9 * 3600 + 24 * 60 + 57,
                        time_label="09:24:57",
                        price=5.85,
                    ),
                    SimpleNamespace(
                        time_seconds=14 * 3600 + 57 * 60,
                        time_label="14:57:00",
                        price=5.94,
                    ),
                )
            ),
        )


class _Client:
    def __init__(self, *, same_session: bool = True) -> None:
        self.helpers = _Helpers(same_session=same_session)


def test_load_observation_uses_last_preopen_virtual_price() -> None:
    result = _load_observation(_Client(), "000802.SZ", "2026-08-14")

    assert result is not None
    assert result.previous_time == "09:24:57"
    assert result.previous_price == 5.85
    assert result.open_price == 6.20
    assert result.last_second_pct == 5.9829
    assert result.open_change_pct == -0.9585
    assert result.previous_open_volume == 100000
    assert result.auction_volume_ratio == 1.8872
    assert result.valid_raise_count == 2


def test_load_observation_rejects_series_from_another_session() -> None:
    assert _load_observation(_Client(same_session=False), "000802.SZ", "2026-08-14") is None


def test_load_observation_uses_historical_minute_records() -> None:
    class _HistoryHelpers:
        def auction_data(self, code: str, date: str | None = None, **_kwargs):
            assert code == "sz000993"
            if date == "2026-09-17":
                return SimpleNamespace(snapshot_0925=SimpleNamespace(volume=100000))
            assert date == "2026-09-18"
            return SimpleNamespace(
                trading_date="2026-09-18",
                snapshot_0925=SimpleNamespace(
                    price=18.30,
                    volume=200000,
                    trade_amount_yuan=36600000,
                ),
                pre_close_price=17.97,
                series=None,
                auction_records=(
                    SimpleNamespace(time_minutes=9 * 60 + 24, time_label="09:24", price=17.80),
                ),
            )

    class _HistoryClient:
        helpers = _HistoryHelpers()

    result = _load_observation(_HistoryClient(), "000993.SZ", "2026-09-18", live=False)

    assert result is not None
    assert result.open_price == 18.30
    assert result.previous_price == 17.80
    assert result.previous_time == "09:24"
    assert result.last_second_pct == 2.809
    assert result.auction_volume_ratio == 2.0


def test_live_auction_session_covers_whole_current_trade_date() -> None:
    evening = datetime(2026, 9, 18, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert _is_live_auction_session("2026-09-18", now=evening) is True
    morning = datetime(2026, 9, 18, 9, 26, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert _is_live_auction_session("2026-09-18", now=morning) is True
    assert _is_live_auction_session("2026-09-17", now=morning) is False


def test_eltdx_code_maps_exchange_suffix() -> None:
    assert _eltdx_code("000802.SZ") == "sz000802"
    assert _eltdx_code("600000.SH") == "sh600000"
    assert _eltdx_code("920001.BJ") == "bj920001"


def _point(hour: int, minute: int, second: int, price: float) -> SimpleNamespace:
    return SimpleNamespace(time_seconds=hour * 3600 + minute * 60 + second, price=price)


def test_count_valid_raises_confirms_reraise_and_includes_0925_match() -> None:
    points = [
        _point(9, 24, 39, 17.63),
        _point(9, 24, 48, 17.73),
        _point(9, 24, 57, 17.80),
    ]

    assert _count_valid_raises(points, open_price=18.30) == 3


def test_count_valid_raises_invalidates_when_price_breaks_node() -> None:
    points = [
        _point(9, 23, 36, 17.96),
        _point(9, 23, 45, 17.97),
        _point(9, 23, 54, 17.96),
        _point(9, 24, 39, 17.63),
        _point(9, 24, 48, 17.73),
        _point(9, 24, 57, 17.80),
    ]

    assert _count_valid_raises(points, open_price=18.30) == 3
    assert (
        _count_valid_raises(
            [_point(9, 20, 0, 10.0), _point(9, 20, 3, 10.1), _point(9, 20, 12, 10.0)]
        )
        == 0
    )


def test_count_valid_raises_timeout_hold_without_reraise_still_counts() -> None:
    base = 9 * 3600 + 20 * 60
    held = [
        SimpleNamespace(time_seconds=base, price=10.0),
        SimpleNamespace(time_seconds=base + 3, price=10.1),
        SimpleNamespace(time_seconds=base + 33, price=10.1),
    ]

    assert _count_valid_raises(held) == 1
