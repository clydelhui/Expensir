"""Reply formatting (§6): results carry the active-ledger prefix and a visible #id."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from expensir.domain.convert import consolidate
from expensir.domain.fx import ResolvedRate, fmt_day, fmt_rate
from expensir.domain.money import fmt
from expensir.format.board import BoardLine, equivalent_suffix, outstanding_line, rates_footer


@dataclass(frozen=True)
class LedgerLine:
    ledger_id: int  # shown as #id: names may repeat, /switch #id always lands (§11's spirit)
    name: str
    is_active: bool
    is_archived: bool
    logging_currency: str | None


def ledgers_reply(lines: list[LedgerLine]) -> str:
    """The /ledgers read (§0.7): the whole list, active marked, archived labeled."""
    rendered = []
    for line in lines:
        marks = [m for m in ("active" if line.is_active else "", line.logging_currency or "") if m]
        suffix = f" ({', '.join(marks)})" if marks else ""
        status = " — archived" if line.is_archived else ""
        rendered.append(f"• 📒 #{line.ledger_id} {line.name}{suffix}{status}")
    return "Ledgers\n" + "\n".join(rendered)


@dataclass(frozen=True)
class MemberLine:
    display_name: str
    username: str | None  # None -> no @handle (Telegram allows no username)
    is_you: bool  # the caller's own line, marked "— you"


def members_reply(lines: list[MemberLine]) -> str:
    """The /members roster (#22, ADR-0011): current members, alphabetical by display
    name (case-insensitive), each with its @handle when set and the caller marked."""
    rendered = []
    for member in sorted(lines, key=lambda m: m.display_name.lower()):
        handle = f" (@{member.username})" if member.username else ""
        you = " — you" if member.is_you else ""
        rendered.append(f"• {member.display_name}{handle}{you}")
    return f"Members ({len(lines)}):\n" + "\n".join(rendered)


_SPLIT_LABEL = {
    "exact": "split exactly",
    "shares": "split by shares",
    "percent": "split by percent",
}


def expense_reply(
    *,
    ledger_name: str,
    expense_id: int,
    amount_minor: int,
    currency: str,
    description: str,
    payer_name: str,
    participant_names: list[str],
    rounded_from: str | None = None,
    split_type: str = "equal",
    shares: list[tuple[str, int]] | None = None,  # (name, owed minor), non-equal splits
) -> str:
    amount = fmt(amount_minor, currency)
    rounded = f" (rounded from {rounded_from})" if rounded_from is not None else ""
    if len(participant_names) == 1:
        split = f"owed entirely by {participant_names[0]}"
    elif split_type == "equal":
        split = f"split equally between {join_names(participant_names)}"
    else:
        assert shares is not None
        each = ", ".join(f"{name} {fmt(minor, currency)}" for name, minor in shares)
        split = f"{_SPLIT_LABEL[split_type]}: {each}"
    return (
        f"📒 {ledger_name} • #{expense_id} {description} — {amount}{rounded} "
        f"paid by {payer_name}, {split}."
    )


def proposal_reply(
    *,
    ledger_name: str,
    amount_minor: int,
    currency: str,
    description: str,
    payer_name: str,
    shares: list[tuple[str, int]],  # (name, owed minor) — ALWAYS shown: WYSIWYG (§7.1, §10)
    rounded_from: str | None = None,
) -> str:
    """A fuzzy intent awaiting Confirm (§10): summary + per-person shares + footer.

    The 📒 prefix names the PINNED ledger — what you see is where it commits."""
    amount = fmt(amount_minor, currency)
    rounded = f" (rounded from {rounded_from})" if rounded_from is not None else ""
    each = " · ".join(f"{name} {fmt(minor, currency)}" for name, minor in shares)
    return (
        f"📒 {ledger_name} • 💡 {description} — {amount}{rounded} paid by {payer_name}\n"
        f"{each}\n"
        f"\n"
        f"↳ reply to correct"
    )


def action_proposal_reply(*, ledger_name: str, summary: str) -> str:
    """A non-expense proposal (§10): one-line summary, same pin + footer."""
    return f"📒 {ledger_name} • 💡 {summary}\n\n↳ reply to correct"


def pick_stage_reply(*, ledger_name: str, gist: str, ref: str) -> str:
    """A proposal waiting on one ambiguous slot (§10, §13): the pick-list stage.

    No shares preview here — they can't be computed until the slot is pinned;
    the same pin prefix and correction footer as every proposal."""
    return (
        f"📒 {ledger_name} • 💡 {gist}\n"
        f"🤔 More than one member here matches “{ref}” — tap who you meant.\n"
        f"\n"
        f"↳ reply to correct"
    )


def expense_pick_stage_reply(
    *, ledger_name: str, gist: str, query: str, shown: int, total: int
) -> str:
    """The expense flavour of the pick stage (§11 tertiary, §13); when the
    candidate list is capped, say what was dropped rather than hide it."""
    capped = (
        f" (showing the {shown} newest of {total} — use its #id for an older one)"
        if total > shown
        else ""
    )
    return (
        f"📒 {ledger_name} • 💡 {gist}\n"
        f"🤔 More than one expense matches “{query}” — tap the one you meant{capped}.\n"
        f"\n"
        f"↳ reply to correct"
    )


def settle_reply(
    *,
    ledger_name: str,
    from_name: str,
    to_name: str,
    amount_minor: int,
    currency: str,
    rounded_from: str | None = None,
) -> str:
    amount = fmt(amount_minor, currency)
    rounded = f" (rounded from {rounded_from})" if rounded_from is not None else ""
    return f"📒 {ledger_name} • 🤝 {from_name} paid {to_name} {amount}{rounded}. Balances updated."


def settle_sheet_reply(
    *,
    ledger_name: str,
    pair_names: tuple[str, str],  # the UNORDERED pair (ADR-0007), in stable order
    transfers: list[BoardLine],
) -> str:
    first, second = pair_names
    if not transfers:
        return f"📒 {ledger_name} • Nothing to settle between {first} and {second}."
    lines = (f"{t.from_name} → {t.to_name} {fmt(t.amount_minor, t.currency)}" for t in transfers)
    return "\n".join([f"📒 {ledger_name} • Settling up {first} ↔ {second}", *lines])


def delete_reply(
    *,
    ledger_name: str,
    expense_id: int,
    amount_minor: int,
    currency: str,
    description: str,
) -> str:
    amount = fmt(amount_minor, currency)
    return f"📒 {ledger_name} • 🗑 Deleted #{expense_id} {description} — {amount}. Balances updated."


def edit_reply(
    *,
    ledger_name: str,
    expense_id: int,
    amount_minor: int,
    currency: str,
    description: str,
    occurred_on: str | None,
) -> str:
    amount = fmt(amount_minor, currency)
    dated = f", dated {occurred_on}" if occurred_on is not None else ""
    return f"📒 {ledger_name} • ✏️ #{expense_id} is now: {description} — {amount}{dated}."


def join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _total_equivalent_line(
    entries: list[tuple[str, dict[str, int]]],
    as_me: bool,
    home: str,
    rates: Mapping[str, ResolvedRate | None],
) -> str | None:
    """The total `≈ home` line (§7.6): what's outstanding overall (each debtor
    bucket converted; scope=me nets the caller's own buckets). Buckets with no
    rate stay behind as explicit remainders, like /convert."""
    buckets = [
        (currency, net)
        for _, by_currency in entries
        for currency, net in by_currency.items()
        if as_me or net > 0  # group scope: the debtor side
    ]
    if not as_me:
        return outstanding_line(buckets, home, rates)  # the board's exact line (§13)
    total, remainders = consolidate(buckets, home, rates)
    if total == 0:
        # a me-scope that nets to zero (or nothing converted): a total would
        # only restate the bucket lines' (≈ n/a)
        return None
    remainder = "".join(f" + {r}" for r in remainders)
    verb = "you owe" if total > 0 else "you're owed"
    return f"In all {verb} ≈ {fmt(abs(total), home)}{remainder}"


@dataclass(frozen=True)
class PinView:
    """One pinned rate as /rates shows it: the pin, who made it, and today's live
    figure beside it so a drifted pin is visible at a glance (§7.6)."""

    base: str
    quote: str
    rate: float
    by_name: str
    on: datetime
    reference: ResolvedRate | None  # today's ECB figure for the same pair


def rates_reply(
    *,
    pins: list[PinView],
    in_play: Mapping[str, ResolvedRate | None],  # non-home bucket currency -> rate to home
    home: str | None,
) -> str:
    lines = ["💱 Rates (display only, approximate)"]
    for pin in pins:
        reference = ""
        if pin.reference is not None:
            when = fmt_day(pin.reference.fetched_at) if pin.reference.stale else "today"
            reference = f" (ECB {when}: {fmt_rate(pin.reference.rate)})"
        lines.append(
            f"📌 1 {pin.base} = {fmt_rate(pin.rate)} {pin.quote} — pinned by "
            f"{pin.by_name}, {fmt_day(pin.on)}{reference}"
        )
    for currency in sorted(in_play):
        rate = in_play[currency]
        if rate is None:
            lines.append(f"{currency}→{home}: no rate yet (≈ n/a)")
            continue
        when = f", {fmt_day(rate.fetched_at)}" if rate.stale else ""
        lines.append(f"1 {currency} = {fmt_rate(rate.rate)} {home} (ECB{when})")
    if len(lines) == 1:
        lines.append("No pinned rates, and every balance is already in one currency.")
    example_home = home if home is not None and home != "USD" else "SGD"
    unpin_pair = f"{pins[0].base} {pins[0].quote}" if pins else f"USD {example_home}"
    lines.append(
        f"Pin: /setrate USD {example_home} 1.35 · back to live: /autorate {unpin_pair} · "
        f"consolidate: /convert {example_home}"
    )
    return "\n".join(lines)


def convert_reply(
    *,
    ledger_name: str,
    target: str,
    entries: list[tuple[str, dict[str, int]]],  # (name, currency -> net minor)
    rates: Mapping[str, ResolvedRate | None],  # bucket currency -> rate to target
    as_me: bool = False,  # scope=me: a single entry, phrased as "you"
) -> str:
    """/convert (§7.6): each member's multi-currency net collapsed into one
    `≈ target` figure — buckets with no rate stay as explicit remainders."""
    rows = []
    for name, by_currency in entries:
        total, remainders = consolidate(by_currency.items(), target, rates)
        if total or remainders:
            rows.append((name, total, remainders))
    header = f"📒 {ledger_name} • ≈ in {target} (approximate)"
    if not rows:
        settled = "You're all settled up" if as_me else "All settled up"
        return f"📒 {ledger_name} • {settled}."
    lines = []
    for name, total, remainders in sorted(rows, key=lambda row: (-row[1], row[0])):
        remainder = "".join(f" + {r}" for r in remainders)
        if as_me:
            if total > 0:
                lines.append(f"You owe ≈ {fmt(total, target)}{remainder}")
            elif total < 0:
                lines.append(f"You're owed ≈ {fmt(-total, target)}{remainder}")
            else:
                lines.append("You: " + " + ".join(remainders))
        elif total > 0:
            lines.append(f"{name} owes ≈ {fmt(total, target)}{remainder}")
        elif total < 0:
            lines.append(f"{name} is owed ≈ {fmt(-total, target)}{remainder}")
        else:
            lines.append(f"{name}: " + " + ".join(remainders))
    footer = rates_footer(target, rates)
    if footer is not None:
        lines.append(footer)
    return "\n".join([header, *lines])


def balance_reply(
    *,
    ledger_name: str,
    entries: list[tuple[str, dict[str, int]]],  # (name, currency -> net minor, + owes the pool)
    as_me: bool = False,  # scope=me: a single entry, phrased as "you"
    home: str | None = None,  # group home currency; None -> no ≈ layer (§7.6)
    rates: Mapping[str, ResolvedRate | None] | None = None,  # bucket currency -> rate to home
) -> str:
    lines: list[str] = []
    saw_foreign_bucket = False
    for currency in sorted({c for _, by_ccy in entries for c in by_ccy}):
        nets = [(name, by_ccy[currency]) for name, by_ccy in entries if by_ccy.get(currency)]
        saw_foreign_bucket |= bool(nets) and home is not None and currency != home
        # debtors first, largest debt first; stable by name within ties
        for name, net in sorted(nets, key=lambda item: (-item[1], item[0])):
            amount = fmt(abs(net), currency) + equivalent_suffix(net, currency, home, rates)
            if as_me:
                lines.append(f"You owe {amount}" if net > 0 else f"You're owed {amount}")
            else:
                verb = "owes" if net > 0 else "is owed"
                lines.append(f"{name} {verb} {amount}")
    if not lines:
        settled = "You're all settled up" if as_me else "All settled up"
        return f"📒 {ledger_name} • {settled}."
    if home is not None and saw_foreign_bucket:
        total = _total_equivalent_line(entries, as_me, home, rates or {})
        if total is not None:
            lines.append(total)
        footer = rates_footer(home, rates or {})
        if footer is not None:
            lines.append(footer)
    return f"📒 {ledger_name} • Balances\n" + "\n".join(lines)
