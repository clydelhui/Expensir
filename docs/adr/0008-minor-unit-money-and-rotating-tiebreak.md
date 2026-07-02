# Money is integer minor units; the leftover unit rotates per expense

## Status

accepted — reverses ARCHITECTURE-v2.md's locked `e2` money representation (§0, §3) and amends the
allocation tie-break (§7.1, §17). ADR-0006's `[Settle]` amount token is unchanged in behavior but
now denominated in minor units.

## Context

`e2` (integer 1/100 of the smallest circulating unit) was introduced to make cent-splitting fairer
via sub-unit precision. But the spec simultaneously locked allocation to whole smallest units ("no
fake cents"), so the extra precision was never used — while the actual bias lived in the tie-break
rule: leftover units went "ties to payer", so a habitual payer systematically absorbed the extra
cent on every equal split. Truly using the precision (sub-unit shares in balances) was considered
and rejected: it pushes rounding into settlement time, pollutes the board with fractional cents,
and needs an epsilon write-off for sub-cent residue — the unfairness moves rather than disappears.

## Decision

- **Storage is integer minor units** (`amount_minor`, `owed_minor`): $60.00 → `6000`, ¥6000 →
  `6000`. Whole-unit-ness is guaranteed by the representation instead of policed as an invariant;
  `UNIT_E2`, `quantize_e2`, and the dust branch in `allocate` are deleted. Over-precise input
  (¥6000.50) rounds half-up **at parse time**, visibly.
- **The leftover minor unit rotates.** Allocation stays largest-remainder over whole minor units,
  but remainder ties are ordered by a deterministic per-expense hash (`stable_hash(seed, user_id)`,
  seeded by the originating message id) instead of payer-first. No member is systematically biased;
  over many expenses the leftover lands uniformly in expectation. The seed is frozen into the
  pending intent so the shares a proposal shows are exactly the shares that commit (WYSIWYG).

## Consequences

- Allocation no longer needs the payer parameter; per-expense error stays ≤ 1 minor unit but has
  zero drift in expectation.
- `stable_hash` must be deterministic across processes (e.g. sha256), never Python's builtin
  `hash`.
- If sub-unit precision ever becomes real (itemized receipts, interest), the migration is a
  mechanical ×100 of the money columns.
