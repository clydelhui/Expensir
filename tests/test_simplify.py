"""Golden tests for domain/simplify.py (§7.4): deterministic minimum cash-flow per currency."""

from expensir.domain.simplify import simplify


def test_chain_reduces_to_direct_transfer():
    """A→B→C chains reduce to A→C: the middleman nets to zero and drops out."""
    # A owes the pool 1000, B passes through (net 0), C is owed 1000
    net = {1: 1000, 2: 0, 3: -1000}

    assert simplify(net) == [(1, 3, 1000)]


def test_ties_break_by_ascending_user_id():
    """Equal amounts pair the smallest debtor id with the smallest creditor id."""
    net = {4: -500, 2: 500, 1: 500, 3: -500}

    assert simplify(net) == [(1, 3, 500), (2, 4, 500)]


def test_greedy_rematches_largest_after_partial_settlement():
    """After a partial match the CURRENT largest debtor/creditor is re-selected."""
    net = {1: 1000, 2: 800, 3: -600, 4: -600, 5: -600}

    assert simplify(net) == [(1, 3, 600), (2, 4, 600), (1, 5, 400), (2, 5, 200)]


def test_output_ignores_dict_insertion_order():
    """Board stability (§7.4): the same positions render the same regardless of source order."""
    forward = {1: 700, 2: -300, 3: -400}
    backward = {3: -400, 2: -300, 1: 700}

    assert simplify(forward) == simplify(backward) == [(1, 3, 400), (1, 2, 300)]


def test_settled_and_empty_pools_emit_nothing():
    assert simplify({}) == []
    assert simplify({1: 0, 2: 0}) == []
