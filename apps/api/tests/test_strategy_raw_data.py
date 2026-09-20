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
    _kline_coverage_warning,
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
    provider = LocalStrategyAuctionProvider(store, require_preopen=True, require_seconds=True)

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
        LocalStrategyAuctionProvider(store, require_preopen=True, require_seconds=True).scan(
            ["000802.SZ"], trade_date=trade_date
        )


def test_local_candidates_only_use_previous_sessions(tmp_path) -> None:
    store = StrategyRawStore(tmp_path)
    for trade_date in ("2026-08-11", "2026-08-12", "2026-08-13"):
        store.save_pool(trade_date, [{"代码": "000802", "名称": "北京文化"}])

    candidates = LocalStrategyCandidateProvider(store, lookback_days=3).get_candidates("20260814")

    assert [candidate.symbol for candidate in candidates] == ["000802.SZ"]
    assert "20260814" not in candidates[0].board_note


def test_local_candidates_keep_downloaded_ths_high_days(tmp_path) -> None:
    store = StrategyRawStore(tmp_path)
    for trade_date in ("2026-09-15", "2026-09-16"):
        store.save_pool(trade_date, [], source="同花顺涨停揭秘")
    store.save_pool(
        "2026-09-17",
        [{"code": "603248", "name": "锡华科技", "high_days": "3天3板"}],
        source="同花顺涨停揭秘",
    )

    candidates = LocalStrategyCandidateProvider(store, lookback_days=3).get_candidates("20260918")

    assert [candidate.symbol for candidate in candidates] == ["603248.SH"]
    assert "同花顺几天几板: 3天3板" in (candidates[0].board_note or "")


def test_raw_store_replaces_pool_from_another_source(tmp_path) -> None:
    store = StrategyRawStore(tmp_path)
    assert store.save_pool("2026-09-18", [{"代码": "603248", "名称": "锡华科技"}])

    assert store.save_pool(
        "2026-09-18",
        [{"code": "603248", "name": "锡华科技", "high_days": "4天4板"}],
        source="同花顺涨停揭秘",
    )

    payload = store.load_pool("2026-09-18")
    assert payload is not None
    assert payload["source"] == "同花顺涨停揭秘"
    assert payload["rows"][0]["high_days"] == "4天4板"


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


def test_download_reports_partial_coverage_as_warning_and_request_error_as_failure(
    tmp_path,
) -> None:
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

    kwargs = {
        "lookback_days": 3,
        "symbol_prefixes": ("00", "60"),
        "progress": lambda *_args: None,
        "should_cancel": lambda: False,
        "pool_fetcher": lambda _date: [{"代码": "000802", "名称": "北京文化"}],
        "auction_provider_factory": auction_factory,
        "now": datetime(2026, 9, 1, 16, tzinfo=ZoneInfo("Asia/Shanghai")),
    }
    result = run_strategy_raw_download(
        tmp_path,
        "month",
        kline_provider=_ShortKlines(),
        **kwargs,
    )

    message = str(result["warning"])
    assert result["warning_count"] == 1
    assert message.startswith("下载完成，有 1 个文件需要注意。\n")
    assert "目标交易区间：2026-09-01～2026-09-01" in message
    assert "日K预热区间" in message
    assert "警告明细：\nklines/000802.SZ.json：起始历史不足（可能为新股）" in message
    assert "实际 2026-09-01～2026-09-01" in message
    suspension = _kline_coverage_warning(
        "klines/603400.SH.json",
        {"start_date": "2026-04-03", "end_date": "2026-09-14"},
        required_start=date(2026, 8, 18),
        required_end=date(2026, 9, 18),
    )
    assert "末端无交易数据（可能停牌）" in suspension

    class _FailedKlines:
        def get_klines(self, _symbol, count=220):
            raise RuntimeError("上游超时")

    with pytest.raises(StrongStockDataUnavailable) as caught:
        failure_kwargs = {
            **kwargs,
            "pool_fetcher": lambda _date: [{"代码": "000802", "名称": "北京文化"}],
        }
        run_strategy_raw_download(
            tmp_path / "failed",
            "month",
            kline_provider=_FailedKlines(),
            **failure_kwargs,
        )

    failure = str(caught.value)
    assert failure.startswith("下载已完成，但有 1 个文件失败。\n")
    assert "失败明细：\nklines/000802.SZ.json：日K请求失败（RuntimeError: 上游超时）" in failure


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
