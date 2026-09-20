from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.models import KlineBar, StrongStockDataUnavailable
from app.services.auction_snatch import AuctionSnatchObservation
from app.services.strategy_raw_data import (
    LocalStrategyAuctionProvider,
    LocalStrategyCandidateProvider,
    StrategyRawDownloadCanceled,
    StrategyRawStore,
    run_strategy_raw_download,
    strategy_download_dates,
)


def _point(second: int, price: float) -> SimpleNamespace:
    return SimpleNamespace(
        time_seconds=9 * 3600 + 24 * 60 + second,
        time_label=f"09:24:{second:02d}",
        price=price,
        volume=100,
    )


def _observation() -> AuctionSnatchObservation:
    return AuctionSnatchObservation(
        symbol="000802.SZ",
        open_price=6.2,
        open_change_pct=1.2,
        open_volume=200,
        open_amount=1240,
        previous_price=6.0,
        previous_time="09:24:57",
        last_second_pct=3.3333,
        valid_raise_count=3,
        previous_open_volume=100,
        auction_volume_ratio=2,
    )


def test_raw_store_keeps_seconds_and_local_provider_reads_it(tmp_path) -> None:
    store = StrategyRawStore(tmp_path)
    trade_date = "2026-08-14"
    auction = SimpleNamespace(
        snapshot_0925=SimpleNamespace(price=6.2, volume=200, trade_amount_yuan=1240),
        pre_close_price=6.1,
        series=SimpleNamespace(points=(_point(39, 5.9), _point(57, 6.0))),
        auction_records=(),
    )
    assert store.archive_auction(
        trade_date,
        "000802.SZ",
        auction,
        _observation(),
        previous_open_volume=100,
    )

    minute = SimpleNamespace(
        snapshot_0925=auction.snapshot_0925,
        pre_close_price=6.1,
        series=None,
        auction_records=(SimpleNamespace(time_minutes=9 * 60 + 24, price=6.0),),
    )
    assert not store.archive_auction(
        trade_date,
        "000802.SZ",
        minute,
        _observation(),
        previous_open_volume=100,
    )
    store.save_klines(
        "000802.SZ",
        [
            KlineBar(
                date="2026-08-13",
                open=5.8,
                close=6,
                high=6,
                low=5.8,
                volume=1000,
            )
        ],
    )
    provider = LocalStrategyAuctionProvider(
        store, require_preopen=True, require_seconds=True
    )

    scan = provider.scan(["000802.SZ"], trade_date=trade_date)

    assert scan.observations["000802.SZ"].valid_raise_count == 3
    assert store.load_auction(trade_date, "000802.SZ")["resolution"] == "seconds"
    assert not list(store.root.rglob("*.part"))


def test_local_history_reports_missing_second_resolution(tmp_path) -> None:
    store = StrategyRawStore(tmp_path)
    trade_date = "2026-08-14"
    auction = SimpleNamespace(
        snapshot_0925=SimpleNamespace(price=6.2, volume=200, trade_amount_yuan=1240),
        pre_close_price=6.1,
        series=None,
        auction_records=(SimpleNamespace(time_minutes=9 * 60 + 24, price=6.0),),
    )
    store.archive_auction(
        trade_date,
        "000802.SZ",
        auction,
        _observation(),
        previous_open_volume=100,
    )

    with pytest.raises(StrongStockDataUnavailable, match="没有秒级竞价"):
        LocalStrategyAuctionProvider(
            store, require_preopen=True, require_seconds=True
        ).scan(["000802.SZ"], trade_date=trade_date)


def test_local_candidates_only_use_previous_sessions(tmp_path) -> None:
    store = StrategyRawStore(tmp_path)
    for trade_date in ("2026-08-11", "2026-08-12", "2026-08-13"):
        store.save_pool(trade_date, [{"代码": "000802", "名称": "北京文化"}])

    candidates = LocalStrategyCandidateProvider(store, lookback_days=3).get_candidates(
        "20260814"
    )

    assert [candidate.symbol for candidate in candidates] == ["000802.SZ"]
    assert "20260814" not in candidates[0].board_note


def test_three_month_period_uses_natural_months_and_last_completed_session() -> None:
    dates = strategy_download_dates(
        "three_months",
        now=datetime(2026, 9, 20, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert dates[0] == date(2026, 7, 1)
    assert dates[-1] == date(2026, 9, 18)


def test_download_cancel_is_reported_as_canceled(tmp_path) -> None:
    with pytest.raises(StrategyRawDownloadCanceled, match="已取消"):
        run_strategy_raw_download(
            tmp_path,
            "month",
            lookback_days=3,
            progress=lambda *_args: None,
            should_cancel=lambda: True,
            pool_fetcher=lambda _date: [],
            kline_provider=object(),
            now=datetime(2026, 9, 1, 16, tzinfo=ZoneInfo("Asia/Shanghai")),
        )


def test_download_error_reports_target_warmup_and_actual_ranges(tmp_path) -> None:
    def auction_factory(store: StrategyRawStore):
        class _AuctionProvider:
            def scan(self, symbols, *, trade_date):
                for symbol in symbols:
                    store.archive_auction(
                        trade_date,
                        symbol,
                        SimpleNamespace(
                            snapshot_0925=SimpleNamespace(
                                price=6.2, volume=200, trade_amount_yuan=1240
                            ),
                            pre_close_price=6.1,
                            series=SimpleNamespace(points=(_point(57, 6.0),)),
                            auction_records=(),
                        ),
                        _observation(),
                        previous_open_volume=100,
                    )

        return _AuctionProvider()

    class _ShortKlines:
        def get_klines(self, _symbol, count=220):
            return [
                KlineBar(
                    date="2026-09-01",
                    open=5,
                    close=5,
                    high=5,
                    low=5,
                    volume=100,
                )
            ]

    with pytest.raises(StrongStockDataUnavailable) as caught:
        run_strategy_raw_download(
            tmp_path,
            "month",
            lookback_days=3,
            symbol_prefixes=("00", "60"),
            progress=lambda *_args: None,
            should_cancel=lambda: False,
            pool_fetcher=lambda _date: [
                {"代码": "000802", "名称": "北京文化"}
            ],
            auction_provider_factory=auction_factory,
            kline_provider=_ShortKlines(),
            now=datetime(2026, 9, 1, 16, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

    message = str(caught.value)
    assert "有 1 个文件不完整" in message
    assert "目标交易区间：2026-09-01～2026-09-01" in message
    assert "日K预热区间" in message
    assert "klines/000802.SZ.json：日K覆盖不足" in message
    assert "实际 2026-09-01～2026-09-01" in message


def test_download_writes_one_day_and_rerun_skips_existing_files(tmp_path) -> None:
    calls = {"pool": 0, "auction": 0, "kline": 0}

    def pool_fetcher(_trade_date: str):
        calls["pool"] += 1
        return [
            {"代码": "000802", "名称": "北京文化"},
            {"代码": "300001", "名称": "创业板样本"},
        ]

    def auction_factory(store: StrategyRawStore):
        class _AuctionProvider:
            def scan(self, symbols, *, trade_date):
                assert symbols == ["000802.SZ"]
                calls["auction"] += 1
                for symbol in symbols:
                    store.archive_auction(
                        trade_date,
                        symbol,
                        SimpleNamespace(
                            snapshot_0925=SimpleNamespace(
                                price=6.2, volume=200, trade_amount_yuan=1240
                            ),
                            pre_close_price=6.1,
                            series=SimpleNamespace(points=(_point(57, 6.0),)),
                            auction_records=(),
                        ),
                        _observation(),
                        previous_open_volume=100,
                    )
                return SimpleNamespace()

        return _AuctionProvider()

    class _Klines:
        def get_klines(self, _symbol, count=220):
            calls["kline"] += 1
            start = date(2026, 8, 17)
            return [
                KlineBar(
                    date=(start + timedelta(days=index)).isoformat(),
                    open=5,
                    close=5,
                    high=5,
                    low=5,
                    volume=100,
                )
                for index in range((date(2026, 9, 1) - start).days + 1)
            ]

    kwargs = {
        "lookback_days": 3,
        "symbol_prefixes": ("00", "60"),
        "progress": lambda *_args: None,
        "should_cancel": lambda: False,
        "pool_fetcher": pool_fetcher,
        "auction_provider_factory": auction_factory,
        "kline_provider": _Klines(),
        "now": datetime(2026, 9, 1, 16, tzinfo=ZoneInfo("Asia/Shanghai")),
    }
    result = run_strategy_raw_download(tmp_path, "month", **kwargs)
    first_calls = dict(calls)
    run_strategy_raw_download(tmp_path, "month", **kwargs)

    assert result["trading_days"] == 1
    assert calls == first_calls
