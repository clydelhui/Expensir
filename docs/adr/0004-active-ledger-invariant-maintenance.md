# Preserving the "active ledger is always an open ledger" invariant

## Status

accepted — refines ARCHITECTURE.md §5 (the invariant) and §8 (undo of `new_ledger`), which stated the invariant but not how each operation keeps it.

## Context

`groups.active_ledger_id` must always point to an open ledger, because every new expense lands in the active ledger. Several operations can orphan that pointer: archiving the active ledger, archiving the last open ledger, and undoing `new_ledger` (which restores the previous pointer and archives the created ledger).

## Decision

- **Archiving the active ledger** repoints `active_ledger_id` to the **most-recently-created open ledger** in the group (deterministic) and announces the switch. The existing "warn, allow" on non-zero balances still applies.
- **Archiving the only open ledger is forbidden** — refused with guidance to create another first. No auto-creation of a replacement.
- **Undoing `new_ledger` is refused once the created ledger has any non-deleted transactions** ("it has expenses; delete or archive them first"), so undo-new-ledger only ever removes the empty shell it created. When allowed, if the previous active ledger has since been archived, repoint by the archive rule (most-recently-created open ledger) rather than restoring a now-archived pointer.

## Consequences

- A group can never reach a state with zero open ledgers or an archived active ledger.
- Two operations are deliberately refused rather than made to "just work"; the refusals are intentional and should not be removed without re-deriving how the invariant would otherwise hold.
