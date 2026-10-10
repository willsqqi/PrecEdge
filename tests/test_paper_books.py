from dataclasses import replace
from decimal import Decimal

import pytest

from prediction_market.research.benchmark import synthetic_contract
from prediction_market.research.books import Book, DepthPool, FeeSchedule, Level, money
from prediction_market.research.execution import Refusal, commit_plan, plan_pair

NOW = "2029-01-01T00:00:20Z"


def setup_pair():
    left = synthetic_contract("kalshi", "k", "orchid")
    right = synthetic_contract("polymarket", "p", "orchid")
    books = [Book("s1", left.key, left.fingerprint, "yes", "2029-01-01T00:00:00Z",
                  (Level("0.30", 2), Level("0.40", 3))),
             Book("s2", right.key, right.fingerprint, "no", "2029-01-01T00:00:00Z",
                  (Level("0.45", 4), Level("0.50", 1)))]
    fees = [FeeSchedule(c.key, "per_contract", "0.01", "synthetic-fees-v1",
                        "2028-01-01T00:00:00Z", "2030-01-01T00:00:00Z") for c in (left, right)]
    pool = DepthPool()
    for book in books:
        pool.register(book)
    return pool, left, right, books, fees


def plan(quantity=3, **options):
    pool, left, right, books, fees = setup_pair()
    return pool, plan_pair(pool, left, right, *books, *fees, quantity, NOW, **options)


def test_walk_depth_with_decimal_cost_and_explicit_fees():
    pool, result = plan()
    assert result.legs[0].fills == (Level("0.30", 2), Level("0.40", 1))
    assert result.legs[0].cost == Decimal("1.00")
    assert result.debit == Decimal("2.41")
    assert result.conditional_edge == Decimal("0.59")
    assert pool.remaining["s1"] == [2, 3]  # Planning does not fill.
    commit_plan(pool, result)
    assert pool.remaining == {"s1": [0, 2], "s2": [1, 1]}
    with pytest.raises(Refusal, match="insufficient visible depth"):
        _, left, right, books, fees = setup_pair()
        plan_pair(pool, left, right, *books, *fees, 3, NOW)


def test_second_leg_depth_failure_never_consumes_first_leg():
    pool, left, right, books, fees = setup_pair()
    pool.remaining["s2"] = [0, 0]
    with pytest.raises(Refusal, match="insufficient visible depth"):
        plan_pair(pool, left, right, *books, *fees, 1, NOW)
    assert pool.remaining["s1"] == [2, 3]


def test_commit_is_atomic_if_quote_depth_was_consumed():
    pool, result = plan()
    pool.remaining["s2"] = [0, 0]
    with pytest.raises(ValueError, match="changed since quote"):
        commit_plan(pool, result)
    assert pool.remaining["s1"] == [2, 3]


def test_immutable_ids_and_superseded_books():
    pool, left, right, books, fees = setup_pair()
    pool.remaining["s1"][0] = 0
    pool.register(books[0])
    assert pool.remaining["s1"][0] == 0  # Repeated record doesn't refresh depth.
    with pytest.raises(ValueError, match="changed payload"):
        pool.register(replace(books[0], asks=(Level("0.2", 20),)))
    pool.register(replace(books[0], snapshot_id="s3", observed_at="2029-01-01T00:00:10Z"))
    with pytest.raises(Refusal, match="superseded"):
        plan_pair(pool, left, right, *books, *fees, 1, NOW)
    with pytest.raises(ValueError, match="advance observation"):
        pool.register(replace(books[0], snapshot_id="s4"))


@pytest.mark.parametrize("clock,reason", [
    ("2029-01-01T00:00:31Z", "stale_book"),
    ("2028-12-31T23:59:59Z", "future_book"),
    ("2030-01-02T00:00:00Z", "contract_resolution_reached"),
])
def test_replay_clock_is_used(clock, reason):
    pool, left, right, books, fees = setup_pair()
    with pytest.raises(Refusal, match=reason):
        plan_pair(pool, left, right, *books, *fees, 1, clock)


def test_age_boundary_and_timezone_equivalent_clock():
    pool, left, right, books, fees = setup_pair()
    assert plan_pair(pool, left, right, *books, *fees, 1, "2028-12-31T19:00:30-05:00")


@pytest.mark.parametrize("change,reason", [
    ({"rules_text": "new rules"}, "contract_fingerprint_mismatch"),
    ({"resolution_source": "different"}, "settlement_evidence"),
    ({"reviewed_by": ""}, "settlement_evidence"),
    ({"reviewed_at": "2029-01-01T01:00:00Z"}, "future_contract_review"),
])
def test_evidence_and_fingerprints_fail_closed(change, reason):
    pool, left, right, books, fees = setup_pair()
    left = replace(left, **change)
    if reason == "future_contract_review":
        books[0] = replace(books[0], contract_fingerprint=left.fingerprint)
    with pytest.raises(Refusal, match=reason):
        plan_pair(pool, left, right, *books, *fees, 1, NOW)


def test_unknown_expired_and_wrong_fee_schedules():
    pool, left, right, books, fees = setup_pair()
    for fee, reason in [(None, "unknown_fee_schedule"),
                        (replace(fees[0], contract_key="other"), "fee_contract_mismatch"),
                        (replace(fees[0], valid_until=NOW), "fee_outside_validity"),
                        (replace(fees[0], reviewed_at="2029-01-01T01:00:00Z"), "fee_outside_validity")]:
        with pytest.raises(Refusal, match=reason):
            plan_pair(pool, left, right, *books, fee, fees[1], 1, NOW)


def test_fee_rounding_is_per_aggregate_leg_and_zero_is_explicit():
    fee = setup_pair()[-1][0]
    quadratic = replace(fee, model="quadratic", rate=Decimal("0.07"))
    assert quadratic.charge((Level("0.30", 2), Level("0.40", 1))) == Decimal("0.05")
    assert replace(fee, rate=Decimal(0)).charge((Level("0.30", 100),)) == 0
    assert replace(fee, rate=Decimal(0), minimum=Decimal("0.025")).charge((Level("0.30", 1),)) == Decimal("0.03")
    with pytest.raises(ValueError, match="evidence"):
        replace(fee, reference="unknown")


def test_fees_and_minimum_edge_can_remove_apparent_spread():
    pool, left, right, books, fees = setup_pair()
    with pytest.raises(Refusal, match="insufficient_edge"):
        plan_pair(pool, left, right, *books, *fees, 3, NOW, minimum_edge="0.59")
    with pytest.raises(Refusal, match="insufficient_edge"):
        plan_pair(pool, left, right, *books, replace(fees[0], rate=Decimal("1")), fees[1], 1, NOW)
    assert pool.remaining["s1"] == [2, 3]


def test_requires_opposite_outcomes_and_exact_registered_books():
    pool, left, right, books, fees = setup_pair()
    with pytest.raises(Refusal, match="complementary"):
        plan_pair(pool, left, right, books[0], replace(books[1], outcome="yes"), *fees, 1, NOW)
    with pytest.raises(Refusal, match="unregistered"):
        plan_pair(pool, left, right, replace(books[0], snapshot_id="absent"), books[1], *fees, 1, NOW)


@pytest.mark.parametrize("value", ["NaN", "Infinity", True, "oops"])
def test_invalid_money_is_rejected(value):
    with pytest.raises(ValueError):
        money(value)


@pytest.mark.parametrize("quantity", [0, -1, True, 1.5])
def test_whole_positive_contract_quantities_only(quantity):
    with pytest.raises(ValueError):
        Level("0.3", quantity)


@pytest.mark.parametrize("prices", [["0.3", "0.2"], ["0.3", "0.3"], ["-0.1"], ["1.1"]])
def test_invalid_or_duplicate_ask_prices(prices):
    book = setup_pair()[3][0]
    with pytest.raises(ValueError):
        replace(book, asks=tuple(Level(p, 1) for p in prices))


def test_book_roundtrip_and_digest_bind_all_levels():
    book = setup_pair()[3][0]
    assert Book.from_dict(book.to_dict()) == book
    assert replace(book, asks=(Level("0.3", 1),)).fingerprint != book.fingerprint
