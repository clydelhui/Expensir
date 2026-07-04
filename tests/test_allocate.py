from expensir.domain.allocate import allocate

THREE_EQUAL = {1: 1, 2: 1, 3: 1}


def test_golden_three_way_ten_dollars_gives_334_333_333():
    shares = allocate(1000, THREE_EQUAL, seed=42)
    assert sorted(shares.values(), reverse=True) == [334, 333, 333]


def test_golden_three_way_6001_yen_gives_2001_2000_2000():
    shares = allocate(6001, THREE_EQUAL, seed=7)
    assert sorted(shares.values(), reverse=True) == [2001, 2000, 2000]


def test_exact_division_needs_no_tiebreak():
    assert allocate(900, THREE_EQUAL, seed=1) == {1: 300, 2: 300, 3: 300}


def test_extra_unit_recipient_rotates_with_seed_but_is_stable_per_seed():
    recipient_by_seed = {
        seed: max(
            allocate(1000, THREE_EQUAL, seed=seed),
            key=lambda u: allocate(1000, THREE_EQUAL, seed=seed)[u],
        )
        for seed in range(40)
    }
    # rotation: not systematically the same member (ADR-0008)
    assert len(set(recipient_by_seed.values())) > 1
    # stability: same seed, same result, every time
    for seed, recipient in recipient_by_seed.items():
        assert (
            max(
                allocate(1000, THREE_EQUAL, seed=seed),
                key=lambda u: allocate(1000, THREE_EQUAL, seed=seed)[u],
            )
            == recipient
        )


def test_conservation_holds_for_every_total():
    for total in range(1, 400):
        shares = allocate(total, THREE_EQUAL, seed=total)
        assert sum(shares.values()) == total
        assert all(isinstance(v, int) for v in shares.values())


def test_golden_weighted_2_to_1_splits_100_dollars_6667_3333():
    shares = allocate(10000, {1: 2, 2: 1}, seed=5)
    assert shares == {1: 6667, 2: 3333}


def test_golden_fractional_weights_split_exactly():
    # 1.5 : 1 : 0.5 over EUR 100.00 -> 50%, ~33.3%, ~16.7%
    shares = allocate(10000, {1: 1.5, 2: 1, 3: 0.5}, seed=9)
    assert sum(shares.values()) == 10000
    assert shares[1] == 5000
    assert sorted((shares[2], shares[3])) == [1667, 3333]


def test_golden_percent_weights_summing_past_100_normalize():
    # 33.4 + 33.3 + 33.4 = 100.1: within the ±1.0 tolerance, weights normalize (§7.1)
    shares = allocate(1000, {1: 33.4, 2: 33.3, 3: 33.4}, seed=3)
    assert sum(shares.values()) == 1000
    assert sorted(shares.values(), reverse=True) == [334, 333, 333]
    assert shares[2] == 333  # the strictly smaller percent never takes the extra unit


def test_weighted_conservation_holds_for_every_total():
    for total in range(1, 400):
        shares = allocate(total, {1: 60, 2: 39.5, 3: 0.5}, seed=total)
        assert sum(shares.values()) == total
        assert all(isinstance(v, int) for v in shares.values())
