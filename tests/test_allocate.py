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
