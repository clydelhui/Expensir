"""Pure replay math (§7.2): order-independent, conserving, per-currency."""

import random

from expensir.domain.allocate import allocate
from expensir.domain.balances import ExpenseEvent, replay


def event(
    payer: int, currency: str, total: int, participants: list[int], seed: int
) -> ExpenseEvent:
    shares = allocate(total, {u: 1 for u in participants}, seed)
    return ExpenseEvent(payer, currency, total, tuple(shares.items()))


def test_golden_two_person_dinner():
    net = replay([event(1, "EUR", 6000, [1, 2], seed=13)])

    assert net[1]["EUR"] == -3000  # Alice paid 60, owes her 30 share
    assert net[2]["EUR"] == 3000


def test_replay_is_order_independent_and_conserves_per_currency():
    rng = random.Random(0)
    users = [1, 2, 3, 4, 5]
    events = [
        event(
            payer=rng.choice(users),
            currency=rng.choice(["EUR", "JPY", "KWD"]),
            total=rng.randint(1, 100_000),
            participants=rng.sample(users, rng.randint(1, len(users))),
            seed=i,
        )
        for i in range(200)
    ]

    net = replay(events)

    shuffled = events[:]
    rng.shuffle(shuffled)
    assert replay(shuffled) == net  # a sum of deltas: order never matters (§0.4)
    for currency in ("EUR", "JPY", "KWD"):
        assert sum(by_ccy.get(currency, 0) for by_ccy in net.values()) == 0


def test_currencies_never_mix():
    net = replay([event(1, "EUR", 6000, [1, 2], seed=1), event(2, "JPY", 500, [1, 2], seed=2)])

    assert net[1] == {"EUR": -3000, "JPY": 250}
    assert net[2] == {"EUR": 3000, "JPY": -250}
