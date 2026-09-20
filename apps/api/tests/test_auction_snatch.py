from __future__ import annotations

from app.models import AuctionSnapshotItem, KlineBar, StrongStockCandidate
from app.services.auction_snatch import (
    AuctionSnatchObservation,
    AuctionSnatchScan,
    find_limit_up_3d_symbols,
    has_limit_up_within_days,
    is_limit_up_close,
    limit_up_ratio,
)
from app.services.auction_strategy_runtime import (
    _board_stats,
    _break_days,
    _downloaded_board_stats,
    _matches,
    _sort_key,
    _strategy_item,
    recent_limit_up_candidates,
)
from app.strategies.auction_snatch import _matches_exact, run


def _bar(date_key: str, close: float) -> KlineBar:
    return KlineBar(date=date_key, open=close, close=close, high=close, low=close, volume=0)


def test_limit_up_ratio_by_board_and_st() -> None:
    assert limit_up_ratio("300001.SZ") == 0.20
    assert limit_up_ratio("301234.SZ") == 0.20
    assert limit_up_ratio("688001.SH") == 0.20
    assert limit_up_ratio("430047.BJ") == 0.30
    assert limit_up_ratio("920001.BJ") == 0.30
    assert limit_up_ratio("600001.SH") == 0.10
    assert limit_up_ratio("600001.SH", name="ST某某") == 0.05
    # 创业板/科创板 ST 仍按 20% 涨跌幅
    assert limit_up_ratio("300001.SZ", name="*ST某创") == 0.20


def test_is_limit_up_close_uses_half_up_rounding() -> None:
    assert is_limit_up_close(close=11.0, prev_close=10.0, ratio=0.10) is True
    assert is_limit_up_close(close=10.99, prev_close=10.0, ratio=0.10) is False
    # 10.945 四舍五入到 10.95（避免银行家舍入得到 10.94）
    assert is_limit_up_close(close=10.95, prev_close=9.95, ratio=0.10) is True
    assert is_limit_up_close(close=18.6, prev_close=15.5, ratio=0.20) is True
    assert is_limit_up_close(close=1.0, prev_close=0.0, ratio=0.10) is False


def test_has_limit_up_within_days_counts_recent_completed_days() -> None:
    # 06-30 相对 06-29 的 10.00 涨停（10.00 * 1.10 -> 11.00）
    bars = [
        _bar("2026-06-26", 9.90),
        _bar("2026-06-29", 10.00),
        _bar("2026-06-30", 11.00),
    ]
    assert has_limit_up_within_days(bars, trade_date="2026-07-01") is True


def test_has_limit_up_within_days_ignores_today_and_older_days() -> None:
    # 涨停发生在 trade_date 当天（竞价时今日未收盘），不应计入：
    # 07-01 相对 06-30 的 10.00 是涨停价 11.00，但当天 K 线被排除
    bars = [
        _bar("2026-06-29", 10.00),
        _bar("2026-06-30", 10.00),
        _bar("2026-07-01", 11.00),
    ]
    assert has_limit_up_within_days(bars, trade_date="2026-07-01") is False

    # 涨停发生在 4 个交易日之前，超出 3 日窗口
    bars = [
        _bar("2026-06-24", 10.00),
        _bar("2026-06-25", 11.00),
        _bar("2026-06-26", 10.80),
        _bar("2026-06-29", 10.70),
        _bar("2026-06-30", 10.60),
    ]
    assert has_limit_up_within_days(bars, trade_date="2026-07-01", days=3) is False


def test_has_limit_up_within_days_respects_st_limit() -> None:
    # 主板 ST：5% 涨停。10.00 -> 10.50 封板
    bars = [
        _bar("2026-06-29", 10.00),
        _bar("2026-06-30", 10.50),
    ]
    assert (
        has_limit_up_within_days(bars, trade_date="2026-07-01", symbol="600001.SH", name="ST某某")
        is True
    )
    # 非 ST 主板：10.50 不算涨停
    assert (
        has_limit_up_within_days(bars, trade_date="2026-07-01", symbol="600001.SH", name="普通股")
        is False
    )


def test_has_limit_up_within_days_handles_empty_and_insufficient_data() -> None:
    assert has_limit_up_within_days([], trade_date="2026-07-01") is False
    bars = [_bar("2026-06-30", 11.00)]
    assert has_limit_up_within_days(bars, trade_date="2026-07-01") is False


class _FakeKlineProvider:
    def __init__(self, bars_by_symbol: dict[str, list[KlineBar]]) -> None:
        self._bars_by_symbol = bars_by_symbol

    def get_klines(self, symbol: str, count: int = 220) -> list[KlineBar]:
        if symbol == "600999.SH":
            raise RuntimeError("provider down")
        return self._bars_by_symbol.get(symbol, [])


class _Item:
    def __init__(self, symbol: str, name: str | None = None) -> None:
        self.symbol = symbol
        self.name = name


def test_find_limit_up_3d_symbols_returns_only_qualified_symbols() -> None:
    limit_up = [
        _bar("2026-06-29", 10.00),
        _bar("2026-06-30", 11.00),  # 主板 10% 涨停
    ]
    no_limit = [
        _bar("2026-06-29", 10.00),
        _bar("2026-06-30", 10.50),
    ]
    provider = _FakeKlineProvider(
        {
            "600001.SH": limit_up,
            "600002.SH": no_limit,
            "600999.SH": limit_up,  # provider 抛错，应跳过
        }
    )
    result = find_limit_up_3d_symbols(
        provider,
        [_Item("600001.SH"), _Item("600002.SH"), _Item("600999.SH")],
        trade_date="2026-07-01",
    )
    assert result == {"600001.SH"}


def test_find_limit_up_3d_symbols_empty_items() -> None:
    provider = _FakeKlineProvider({})
    assert find_limit_up_3d_symbols(provider, [], trade_date="2026-07-01") == set()


def _candidate(symbol: str, name: str, dates: str) -> StrongStockCandidate:
    return StrongStockCandidate(
        symbol=symbol,
        name=name,
        board_note=f"涨停日期: {dates}",
    )


def test_recent_limit_up_candidates_uses_three_completed_sessions() -> None:
    candidates = [
        _candidate("000802.SZ", "北京文化", "20260813,20260812,20260811,20260810"),
        _candidate("600001.SH", "过期样本", "20260810"),
        _candidate("600002.SH", "当日涨停", "20260814"),
    ]

    result = recent_limit_up_candidates(candidates, trade_date="2026-08-14")

    assert [item.symbol for item in result] == ["000802.SZ"]


class _FakeCandidateProvider:
    source_name = "测试涨停池"

    def get_candidates(self, trade_date: str) -> list[StrongStockCandidate]:
        assert trade_date == "20260814"
        return [
            _candidate("000802.SZ", "北京文化", "20260813,20260812,20260811"),
            _candidate("600001.SH", "仅有涨停", "20260813"),
            _candidate("300001.SZ", "创业板样本", "20260813"),
            _candidate("600002.SH", "无近期涨停", "20260810"),
        ]


class _FakeAuctionProvider:
    source_name = "测试 eltdx"

    def scan(self, symbols: list[str], *, trade_date: str) -> AuctionSnatchScan:
        assert symbols == ["000802.SZ", "600001.SH"]
        assert trade_date == "2026-08-14"
        return AuctionSnatchScan(
            attempted=2,
            observations={
                "000802.SZ": AuctionSnatchObservation(
                    symbol="000802.SZ",
                    open_price=6.20,
                    open_change_pct=-0.9585,
                    open_volume=188715,
                    open_amount=117003300,
                    previous_price=5.85,
                    previous_time="09:24:57",
                    last_second_pct=5.9829,
                    valid_raise_count=3,
                    previous_open_volume=100000,
                    auction_volume_ratio=1.8872,
                ),
                "600001.SH": AuctionSnatchObservation(
                    symbol="600001.SH",
                    open_price=10.00,
                    open_change_pct=0,
                    open_volume=100,
                    open_amount=100000,
                    previous_price=10.00,
                    previous_time="09:24:57",
                    last_second_pct=0,
                ),
            },
        )


def test_build_auction_snatch_snapshot_strictly_keeps_beijing_culture() -> None:
    result = run(
        _FakeCandidateProvider(),
        _FakeAuctionProvider(),
        trade_date="2026-08-14",
    )

    assert [item.symbol for item in result.items] == ["000802.SZ"]
    assert result.items[0].name == "北京文化"
    assert result.items[0].last_second_pct == 5.9829
    assert result.items[0].prev_node_time == "2026-08-14T09:24:57"
    assert result.items[0].limit_up_3d is True
    assert result.items[0].limit_up_pattern == "3天3板"
    assert result.items[0].valid_raise_count == 3
    assert result.items[0].auction_volume_ratio == 1.8872


def test_snapshot_reports_partial_metric_coverage_without_failing() -> None:
    class PartialProvider(_FakeAuctionProvider):
        def scan(self, symbols: list[str], *, trade_date: str) -> AuctionSnatchScan:
            scan = super().scan(symbols, trade_date=trade_date)
            return AuctionSnatchScan(
                observations={"000802.SZ": scan.observations["000802.SZ"]},
                attempted=2,
                failed=1,
            )

    result = run(
        _FakeCandidateProvider(),
        PartialProvider(),
        trade_date="2026-08-14",
    )

    status = result.source_status[-1]
    assert status.status == "stale"
    assert status.detail == "逐只核验 2 只，可用数据 1 只，策略命中 1 只，1 只重拉后仍缺所需字段"


def test_auction_snatch_excludes_open_gap_below_minus_two() -> None:
    class LowOpenProvider(_FakeAuctionProvider):
        def scan(self, symbols: list[str], *, trade_date: str) -> AuctionSnatchScan:
            scan = super().scan(symbols, trade_date=trade_date)
            observation = scan.observations["000802.SZ"]
            scan.observations["000802.SZ"] = AuctionSnatchObservation(
                symbol=observation.symbol,
                open_price=observation.open_price,
                open_change_pct=-2.01,
                open_volume=observation.open_volume,
                open_amount=observation.open_amount,
                previous_price=observation.previous_price,
                previous_time=observation.previous_time,
                last_second_pct=observation.last_second_pct,
            )
            return scan

    result = run(
        _FakeCandidateProvider(),
        LowOpenProvider(),
        trade_date="2026-08-14",
    )

    assert result.items == []


def test_strategy_item_records_trade_date_close_price() -> None:
    class _Kline:
        def get_klines(self, symbol: str, count: int = 220):
            assert symbol == "000802.SZ"
            return [_bar("2026-08-13", 6.00), _bar("2026-08-14", 6.66)]

    item = _strategy_item(
        _candidate("000802.SZ", "北京文化", "20260813"),
        AuctionSnatchObservation(
            symbol="000802.SZ",
            open_price=6.20,
            open_change_pct=-0.96,
            open_volume=100,
            open_amount=620,
            previous_price=5.85,
            previous_time="09:24:57",
            last_second_pct=5.9829,
        ),
        trade_date="2026-08-14",
        recent_days=3,
        kline_provider=_Kline(),
    )
    assert item.close_price == 6.66


def test_strategy_item_leaves_close_price_empty_before_daily_bar() -> None:
    class _Kline:
        def get_klines(self, symbol: str, count: int = 220):
            return [_bar("2026-08-13", 6.00)]

    item = _strategy_item(
        _candidate("000802.SZ", "北京文化", "20260813"),
        AuctionSnatchObservation(
            symbol="000802.SZ",
            open_price=6.20,
            open_change_pct=-0.96,
            open_volume=100,
            open_amount=620,
            previous_price=5.85,
            previous_time="09:24:57",
            last_second_pct=5.9829,
        ),
        trade_date="2026-08-14",
        recent_days=3,
        kline_provider=_Kline(),
    )
    assert item.close_price is None


def test_days_boards_sort_keeps_zero_open_above_negative_open() -> None:
    zero_open = AuctionSnapshotItem(
        symbol="600001.SH",
        limit_up_pattern_days=8,
        limit_up_board_count=5,
        open_gap_pct=0,
    )
    negative_open = AuctionSnapshotItem(
        symbol="600002.SH",
        limit_up_pattern_days=8,
        limit_up_board_count=5,
        open_gap_pct=-1.99,
    )

    assert _sort_key(zero_open, "days_boards") > _sort_key(negative_open, "days_boards")


def test_exact_conditions_apply_independently_at_boundaries() -> None:
    qualified = AuctionSnapshotItem(
        symbol="000802.SZ",
        valid_raise_count=3,
        limit_up_break_days=1,
        auction_volume_ratio=1.01,
    )

    assert _matches_exact(qualified, {0, 1, 2, 3}) is True
    assert _matches_exact(qualified.model_copy(update={"valid_raise_count": 2}), {0}) is False
    assert _matches_exact(qualified.model_copy(update={"limit_up_break_days": 2}), {1}) is False
    assert _matches_exact(qualified.model_copy(update={"auction_volume_ratio": 1.0}), {2}) is False
    assert _matches_exact(qualified.model_copy(update={"auction_volume_ratio": 0.6}), {3}) is True
    assert (
        _matches_exact(qualified.model_copy(update={"auction_volume_ratio": 0.5999}), {3}) is False
    )
    assert _matches_exact(qualified.model_copy(update={"auction_volume_ratio": 0.1}), set()) is True


def test_historical_match_only_item_can_use_available_rules() -> None:
    item = AuctionSnapshotItem(
        symbol="000802.SZ",
        open_gap_pct=1.2,
        last_second_price_up=None,
        auction_volume_ratio=0.8,
    )

    assert _matches(item, {"require_last_second_price_up": True, "min_open_gap_pct": -2})
    assert _matches_exact(item, {3})


def test_board_stats_excludes_auction_day_and_counts_trailing_break() -> None:
    mindong = _candidate(
        "000993.SZ",
        "闽东电力",
        "20260916,20260915,20260914,20260911,20260910,20260909",
    )
    gapped = _candidate("000802.SZ", "北京文化", "20260813,20260811")
    isolated = _candidate("600001.SH", "仅有涨停", "20260813")

    assert _board_stats(mindong, trade_date="2026-09-18") == (7, 6)
    assert _board_stats(mindong, trade_date="2026-09-17") == (6, 6)
    assert _board_stats(gapped, trade_date="2026-08-14") == (3, 2)
    assert _board_stats(isolated, trade_date="2026-08-14") == (1, 1)
    missing_latest = _candidate(
        "000993.SZ",
        "闽东电力",
        "20260915,20260914,20260911,20260910,20260909",
    )
    assert _board_stats(missing_latest, trade_date="2026-09-18") == (7, 5)
    assert _board_stats(
        missing_latest, trade_date="2026-09-18", extra_dates={"20260916"}
    ) == (7, 6)
    eleven_boards = _candidate(
        "600001.SH",
        "十一连板样本",
        "20260917,20260916,20260915,20260914,20260911,20260910,20260909,20260908,20260907,20260904,20260903",
    )
    assert _board_stats(eleven_boards, trade_date="2026-09-18") == (10, 10)


def test_downloaded_board_stats_uses_ths_high_days_only_when_present() -> None:
    downloaded = StrongStockCandidate(
        symbol="603626.SH",
        name="科森科技",
        board_note="涨停日期: 20260918,20260917; 同花顺几天几板: 5天3板",
    )
    live = StrongStockCandidate(
        symbol="603626.SH",
        name="科森科技",
        board_note="涨停日期: 20260918,20260917",
    )

    assert _downloaded_board_stats(downloaded) == (5, 3)
    assert _downloaded_board_stats(live) is None
    assert _downloaded_board_stats(
        downloaded.model_copy(update={"board_note": "同花顺几天几板: 首板"})
    ) == (1, 1)

    item = _strategy_item(
        downloaded,
        AuctionSnatchObservation(
            symbol=downloaded.symbol,
            open_price=10,
            open_change_pct=1,
            open_volume=100,
            open_amount=1000,
            previous_price=9.9,
            previous_time="09:24:57",
            last_second_pct=1.01,
        ),
        trade_date="2026-09-21",
        recent_days=3,
    )
    assert item.limit_up_pattern == "5天3板"


def test_break_days_uses_latest_real_limit_up_date_before_auction() -> None:
    huadong = _candidate("002248.SZ", "华东数控", "20260812")
    xuguang = _candidate("600353.SH", "旭光电子", "20260814")
    yiming = _candidate("605179.SH", "一鸣食品", "20260813,20260812")

    assert _break_days(huadong, trade_date="2026-08-17", days=3) == 2
    assert _break_days(xuguang, trade_date="2026-08-17", days=3) == 0
    assert _break_days(yiming, trade_date="2026-08-17", days=3) == 1
