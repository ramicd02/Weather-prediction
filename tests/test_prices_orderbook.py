import pytest

from weatherbot.data.book_recorder import BookRecorder
from weatherbot.data.orderbook import OrderBook, SequenceGap, SequenceTracker
from weatherbot.data.storage import Storage
from weatherbot.kalshi.prices import dollars_to_e4, no_bid_to_yes_ask, size_to_e2


def test_price_conversion_exact():
    assert dollars_to_e4("0.0800") == 800
    assert dollars_to_e4("1.00") == 10000
    assert dollars_to_e4("0.0050") == 50  # sub-penny
    with pytest.raises(ValueError):
        dollars_to_e4("0.00001")  # finer than 1/10,000
    with pytest.raises(ValueError):
        dollars_to_e4("1.5")


def test_size_conversion():
    assert size_to_e2("300.00") == 30000
    assert size_to_e2("-54.00") == -5400


def test_no_bid_to_yes_ask():
    assert no_bid_to_yes_ask(dollars_to_e4("0.40")) == dollars_to_e4("0.60")
    assert no_bid_to_yes_ask(0) == 10000


def snapshot(ticker="KXHIGHCHI-26NOV05-B54.5"):
    return {
        "market_ticker": ticker,
        "yes_dollars_fp": [["0.3000", "100.00"], ["0.3200", "50.00"]],
        "no_dollars_fp": [["0.6000", "200.00"]],
    }


def test_book_snapshot_and_top_of_book():
    b = OrderBook("T")
    b.apply_snapshot(snapshot(), seq=1)
    assert b.best_yes_bid == 3200
    assert b.best_yes_ask == 4000  # NO bid 0.60 -> YES ask 0.40
    assert b.top_of_book() == (3200, 5000, 4000, 20000)


def test_book_delta_add_remove():
    b = OrderBook("T")
    b.apply_snapshot(snapshot(), seq=1)
    b.apply_delta({"side": "yes", "price_dollars": "0.3300", "delta_fp": "10.00"}, seq=2)
    assert b.best_yes_bid == 3300
    b.apply_delta({"side": "yes", "price_dollars": "0.3300", "delta_fp": "-10.00"}, seq=3)
    assert b.best_yes_bid == 3200 and 3300 not in b.yes
    with pytest.raises(ValueError):
        b.apply_delta({"side": "no", "price_dollars": "0.6000", "delta_fp": "-999.00"}, seq=4)


def test_delta_before_snapshot_rejected():
    with pytest.raises(ValueError):
        OrderBook("T").apply_delta({"side": "yes", "price_dollars": "0.1", "delta_fp": "1"})


def test_sequence_gap_detected_per_sid():
    t = SequenceTracker()
    t.check(1, 10)
    t.check(1, 11)
    t.check(2, 5)  # other subscription, independent
    with pytest.raises(SequenceGap):
        t.check(1, 13)


def _recorder():
    s = Storage(":memory:")
    return BookRecorder(s, interval_s=60), s


def test_recorder_saves_snapshot_and_tob_changes_only():
    rec, s = _recorder()
    rec.handle({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": snapshot("M")})
    # deep-book change: top unchanged -> not saved immediately
    rec.handle(
        {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 2,
            "msg": {
                "market_ticker": "M",
                "side": "yes",
                "price_dollars": "0.1000",
                "delta_fp": "5.00",
            },
        }
    )
    # top-of-book change -> saved
    rec.handle(
        {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 3,
            "msg": {
                "market_ticker": "M",
                "side": "yes",
                "price_dollars": "0.3500",
                "delta_fp": "5.00",
            },
        }
    )
    reasons = [r["reason"] for r in s.query("SELECT reason FROM book_snapshots ORDER BY id")]
    assert reasons == ["snapshot", "top_of_book"]


def test_recorder_interval_flush():
    rec, s = _recorder()
    rec.handle({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": snapshot("M")})
    rec.handle(
        {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 2,
            "msg": {
                "market_ticker": "M",
                "side": "yes",
                "price_dollars": "0.1000",
                "delta_fp": "5.00",
            },
        }
    )
    last = rec._last_saved["M"]
    assert rec.flush_interval(now=last + 30) == 0
    assert rec.flush_interval(now=last + 61) == 1
    assert rec.flush_interval(now=last + 200) == 0  # nothing changed since


def test_recorder_raises_on_gap():
    rec, _ = _recorder()
    rec.handle({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": snapshot("M")})
    with pytest.raises(SequenceGap):
        rec.handle(
            {
                "type": "orderbook_delta",
                "sid": 1,
                "seq": 5,
                "msg": {
                    "market_ticker": "M",
                    "side": "yes",
                    "price_dollars": "0.1000",
                    "delta_fp": "5.00",
                },
            }
        )


def test_recorder_trades_deduplicated():
    rec, s = _recorder()
    t = {
        "type": "trade",
        "sid": 2,
        "seq": 1,
        "msg": {
            "trade_id": "abc",
            "market_ticker": "M",
            "yes_price_dollars": "0.4200",
            "count_fp": "12.00",
            "taker_side": "yes",
            "ts": 1762300000,
            "ts_ms": 1762300000123,
        },
    }
    rec.handle(t)
    rec.handle(t)
    rows = s.query("SELECT * FROM trades")
    assert len(rows) == 1
    assert rows[0]["yes_price_e4"] == 4200 and rows[0]["count_e2"] == 1200
    assert rows[0]["ts"].endswith("Z")
