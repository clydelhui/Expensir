"""FX display slice (#16): pins, live rates, ≈ equivalents — display only (§7.5, §7.6)."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from expensir.core.handler import dispatch
from expensir.db.models import Action, FxRate, Group
from expensir.domain.fx import resolve_rate
from tests.factories import bot_added_update, callback_update, message_update, user
from tests.fakes import FakeFx, PoisonedFx, UnavailableFx

BOB = user(1002, "Bob", "bob")
TODAY = date(2026, 7, 11)


def api_row(quote: str, rate: float, fetched_on: date = TODAY) -> FxRate:
    """A cached Frankfurter row: EUR-based, deployment-global (§7.5)."""
    return FxRate(
        group_id=None,
        base_currency="EUR",
        quote_currency=quote,
        rate=rate,
        source="api",
        fetched_at=datetime(fetched_on.year, fetched_on.month, fetched_on.day, 9, tzinfo=UTC),
    )


async def group_id_of(deps, chat_id: int) -> int:
    async with deps.session_factory() as session:
        return (
            (await session.execute(select(Group).where(Group.platform_chat_id == chat_id)))
            .scalar_one()
            .id
        )


async def test_balance_shows_home_equivalents_a_total_and_the_rates_footer(deps):
    deps.fx = FakeFx({"USD": 1.08, "JPY": 160.0, "SGD": 1.46})
    await dispatch(bot_added_update(chat_id=-42), deps)
    await dispatch(message_update(update_id=3, chat_id=-42, text="/homecurrency SGD"), deps)
    await dispatch(message_update(update_id=4, chat_id=-42, text="hi", from_user=BOB), deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/equal 21.60 USD dinner"), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/equal 200 JPY snacks"), deps)

    [reply] = await dispatch(message_update(update_id=7, chat_id=-42, text="/balance"), deps)

    # each non-home bucket carries its ≈ home line (§7.6): 10.80 × 1.46/1.08, 100 × 1.46/160
    assert "Bob owes USD 10.80 (≈ SGD 14.60)" in reply.text
    assert "Bob owes JPY 100 (≈ SGD 0.91)" in reply.text
    assert "Total outstanding ≈ SGD 15.51" in reply.text
    # the rates footer (§13): what's behind each pair in play, fresh ECB undated
    assert "1 USD = 1.351852 SGD (ECB)" in reply.text
    assert "1 JPY = 0.009125 SGD (ECB)" in reply.text


async def test_fx_down_falls_back_to_the_cached_rate_and_surfaces_its_date(deps):
    deps.fx = UnavailableFx()
    await dispatch(bot_added_update(chat_id=-42), deps)
    await dispatch(message_update(update_id=3, chat_id=-42, text="/homecurrency SGD"), deps)
    await dispatch(message_update(update_id=4, chat_id=-42, text="hi", from_user=BOB), deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/equal 21.60 USD dinner"), deps)
    async with deps.session_factory() as session:
        session.add_all(
            [
                api_row("USD", 1.08, fetched_on=date(2026, 7, 9)),
                api_row("SGD", 1.46, fetched_on=date(2026, 7, 9)),
            ]
        )
        await session.commit()

    [reply] = await dispatch(message_update(update_id=6, chat_id=-42, text="/balance"), deps)

    assert deps.fx.requested  # the stale cache DID trigger a refetch attempt (§7.5)
    assert "(≈ SGD 14.60)" in reply.text  # ...which failed, so the cached rate stands
    assert "(ECB, 9 Jul)" in reply.text  # ...with its date surfaced in the footer


async def test_no_rate_at_all_shows_na_and_never_blocks(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)  # deps.fx is None: FX unconfigured
    await dispatch(message_update(update_id=3, chat_id=-42, text="/homecurrency SGD"), deps)
    await dispatch(message_update(update_id=4, chat_id=-42, text="hi", from_user=BOB), deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/equal 21.60 USD dinner"), deps)

    [reply] = await dispatch(message_update(update_id=6, chat_id=-42, text="/balance"), deps)

    assert "Bob owes USD 10.80 (≈ n/a)" in reply.text
    # nothing converted: a total of pure n/a would only repeat the lines above
    assert "Total outstanding" not in reply.text


async def test_the_board_shows_equivalents_a_total_and_the_pinned_footer(deps):
    from tests.test_board import FakeBoardMessenger

    deps.client = FakeBoardMessenger()
    await dispatch(bot_added_update(chat_id=-42), deps)
    await dispatch(message_update(update_id=3, chat_id=-42, text="/homecurrency SGD"), deps)
    await dispatch(message_update(update_id=4, chat_id=-42, text="hi", from_user=BOB), deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/setrate USD SGD 1.35"), deps)

    outbound = await dispatch(
        message_update(update_id=6, chat_id=-42, text="/equal 21.60 USD dinner"), deps
    )

    [edit] = [a for a in outbound if a.kind == "edit_message"]
    assert "Bob → Alice USD 10.80 (≈ SGD 14.58)" in edit.text  # 10.80 × the pinned 1.35
    assert "Total outstanding ≈ SGD 14.58" in edit.text
    assert "≈ 1 USD = 1.35 SGD (pinned)" in edit.text  # no FX provider needed: pins suffice


async def test_rates_lists_pins_with_live_references_then_in_play_api_rates(deps):
    deps.fx = FakeFx({"USD": 1.08, "SGD": 1.46, "JPY": 160.0})
    await dispatch(bot_added_update(chat_id=-42), deps)
    await dispatch(message_update(update_id=3, chat_id=-42, text="/homecurrency SGD"), deps)
    await dispatch(message_update(update_id=4, chat_id=-42, text="hi", from_user=BOB), deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/setrate USD SGD 1.35"), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/equal 200 JPY snacks"), deps)

    [reply] = await dispatch(message_update(update_id=7, chat_id=-42, text="/rates"), deps)

    # pins first, each with the live ECB figure so a drifted pin is visible at a glance
    assert "📌 1 USD = 1.35 SGD — pinned by Alice" in reply.text
    assert "(ECB today: 1.351852)" in reply.text
    # then API rates for the pairs in play on the active ledger (JPY bucket × home)
    assert "1 JPY = 0.009125 SGD (ECB)" in reply.text
    # and the copy-pasteable roads: pin, back-to-live, consolidate
    assert "/autorate USD SGD" in reply.text
    assert "/convert SGD" in reply.text


async def test_rates_with_nothing_in_play_still_answers_with_examples(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)

    [reply] = await dispatch(message_update(update_id=3, chat_id=-42, text="/rates"), deps)

    assert "/setrate USD SGD 1.35" in reply.text  # never silence (no-silent-replies)


async def convert_setup(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)
    await dispatch(message_update(update_id=3, chat_id=-42, text="/homecurrency SGD"), deps)
    await dispatch(message_update(update_id=4, chat_id=-42, text="hi", from_user=BOB), deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/equal 21.60 USD dinner"), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/equal 200 JPY snacks"), deps)


async def test_convert_consolidates_each_members_position_into_the_target(deps):
    deps.fx = FakeFx({"USD": 1.08, "JPY": 160.0, "SGD": 1.46})
    await convert_setup(deps)

    [reply] = await dispatch(message_update(update_id=7, chat_id=-42, text="/convert SGD"), deps)

    # one line per member (§7.6): a ledger-wide sum is always ≈0 by pool construction
    assert "Bob owes ≈ SGD 15.51" in reply.text  # 14.60 + 0.91
    assert "Alice is owed ≈ SGD 15.51" in reply.text
    assert "approximate" in reply.text
    assert "1 USD = 1.351852 SGD (ECB)" in reply.text  # the same rates footer


async def test_convert_keeps_unconvertible_buckets_as_explicit_remainders(deps):
    deps.fx = FakeFx({"USD": 1.08, "SGD": 1.46})  # JPY unsupported
    await convert_setup(deps)

    [reply] = await dispatch(message_update(update_id=7, chat_id=-42, text="/convert SGD"), deps)

    assert "Bob owes ≈ SGD 14.60 + JPY 100 (≈ n/a)" in reply.text


async def test_convert_guides_on_a_missing_or_unknown_target(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)

    [reply] = await dispatch(message_update(update_id=3, chat_id=-42, text="/convert"), deps)
    assert "Usage" in reply.text and "/convert SGD" in reply.text

    [reply] = await dispatch(message_update(update_id=4, chat_id=-42, text="/convert ZZZ"), deps)
    assert "ZZZ" in reply.text


async def test_a_read_that_refreshes_stale_rates_re_renders_the_board(deps):
    from tests.test_board import FakeBoardMessenger

    deps.fx = FakeFx({"USD": 1.08, "SGD": 1.46})
    deps.client = FakeBoardMessenger()
    await dispatch(bot_added_update(chat_id=-42), deps)
    await dispatch(message_update(update_id=3, chat_id=-42, text="/homecurrency SGD"), deps)
    await dispatch(message_update(update_id=4, chat_id=-42, text="hi", from_user=BOB), deps)
    # the write path never fetches (§0.11): with an empty cache the board pins (≈ n/a)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/equal 21.60 USD dinner"), deps)

    outbound = await dispatch(message_update(update_id=6, chat_id=-42, text="/balance"), deps)

    # the read fetched fresh rates, so it re-rendered the stale board (§13)
    [edit] = [a for a in outbound if a.kind == "edit_message"]
    assert "(≈ SGD 14.60)" in edit.text

    # a second read finds the cache fresh: no fetch, no board edit
    outbound = await dispatch(message_update(update_id=7, chat_id=-42, text="/balance"), deps)
    assert [a for a in outbound if a.kind == "edit_message"] == []
    assert len(deps.fx.requested) == 1


async def test_pinned_rates_never_trigger_a_fetch_or_board_refresh(deps):
    from tests.test_board import FakeBoardMessenger

    deps.fx = FakeFx({"USD": 1.08, "SGD": 1.46})
    deps.client = FakeBoardMessenger()
    await dispatch(bot_added_update(chat_id=-42), deps)
    await dispatch(message_update(update_id=3, chat_id=-42, text="/homecurrency SGD"), deps)
    await dispatch(message_update(update_id=4, chat_id=-42, text="hi", from_user=BOB), deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/setrate USD SGD 1.35"), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/equal 21.60 USD dinner"), deps)

    outbound = await dispatch(message_update(update_id=7, chat_id=-42, text="/balance"), deps)

    assert deps.fx.requested == []  # the only pair in play is pinned (§13)
    assert [a for a in outbound if a.kind == "edit_message"] == []


async def test_setrate_and_its_undo_re_render_the_active_boards_equivalents(deps):
    from tests.test_board import FakeBoardMessenger

    deps.client = FakeBoardMessenger()
    await dispatch(bot_added_update(chat_id=-42), deps)
    await dispatch(message_update(update_id=3, chat_id=-42, text="/homecurrency SGD"), deps)
    await dispatch(message_update(update_id=4, chat_id=-42, text="hi", from_user=BOB), deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/equal 21.60 USD dinner"), deps)

    outbound = await dispatch(
        message_update(update_id=6, chat_id=-42, text="/setrate USD SGD 1.35"), deps
    )
    [edit] = [a for a in outbound if a.kind == "edit_message"]
    assert "(≈ SGD 14.58)" in edit.text
    assert "1 USD = 1.35 SGD (pinned)" in edit.text

    action = await latest_action(deps)
    outbound = await dispatch(
        callback_update(update_id=7, chat_id=-42, data=f"v1:undo:{action.id}"), deps
    )
    [board_edit] = [a for a in outbound if a.kind == "edit_message" and "• Board" in a.text]
    assert "(≈ n/a)" in board_edit.text  # the pin is gone and nothing else can rate USD


async def test_no_write_path_ever_touches_fx_transport(deps):
    """ADR-0001 / #16 acceptance: settlement and balance math never call FX —
    every write below would crash if any of it reached for the provider."""
    from tests.test_board import FakeBoardMessenger

    deps.fx = PoisonedFx()
    deps.client = FakeBoardMessenger()
    await dispatch(bot_added_update(chat_id=-42), deps)
    await dispatch(message_update(update_id=3, chat_id=-42, text="/homecurrency SGD"), deps)
    await dispatch(message_update(update_id=4, chat_id=-42, text="hi", from_user=BOB), deps)
    # expense write + its board render (foreign bucket, empty cache: renders n/a)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/equal 21.60 USD dinner"), deps)
    # explicit pin + unpin (only the BARE /setrate form may fetch, and pre-lock)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/setrate USD SGD 1.35"), deps)
    await dispatch(message_update(update_id=7, chat_id=-42, text="/autorate USD SGD"), deps)
    # settlement write + board render
    [settled, *_] = await dispatch(
        message_update(update_id=8, chat_id=-42, text="/settle @bob @alice 5 USD"), deps
    )
    assert "5.00" in settled.text
    # undo/redo of the settlement (the toggle re-renders the board too)
    action = await latest_action(deps)
    await dispatch(callback_update(update_id=9, chat_id=-42, data=f"v1:undo:{action.id}"), deps)
    await dispatch(callback_update(update_id=10, chat_id=-42, data=f"v1:redo:{action.id}"), deps)
    # archive computes outstanding balances (pure pool math) + ledger lifecycle
    await dispatch(message_update(update_id=11, chat_id=-42, text="/newledger Tokyo"), deps)
    await dispatch(message_update(update_id=12, chat_id=-42, text="/archive Tokyo"), deps)


async def test_nl_pin_and_unpin_ride_the_propose_confirm_loop(deps):
    from expensir.db.models import PendingIntent
    from expensir.llm.wire import WireClearFxRate, WireSetFxRate
    from tests.fakes import FakeLLM
    from tests.test_nl import ALICE, arrange_group, mention

    await arrange_group(deps)
    deps.llm = FakeLLM(
        [
            WireSetFxRate(base="USD", quote="SGD", rate="1.35"),
            WireClearFxRate(base="USD", quote="SGD"),
        ]
    )

    (proposal,) = await mention(deps, "pin the rate 1 usd = 1.35 sgd")
    assert "1.35" in proposal.text and "reply to correct" in proposal.text
    assert await read_pins(deps) == []  # nothing commits at propose
    async with deps.session_factory() as session:
        pending_id = (await session.execute(select(PendingIntent))).scalar_one().id

    ack, edit, *_ = await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=555), deps
    )
    assert "Pinned" in edit.text
    [pin] = await read_pins(deps)
    assert pin.rate == 1.35

    (proposal,) = await mention(deps, "go back to the live usd rate", update_id=30, message_id=40)
    async with deps.session_factory() as session:
        pending_id = (
            (await session.execute(select(PendingIntent).order_by(PendingIntent.id.desc())))
            .scalars()
            .first()
            .id
        )
    ack, edit, *_ = await dispatch(
        callback_update(
            update_id=31, data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=556
        ),
        deps,
    )
    assert await read_pins(deps) == []


async def test_a_confirmed_pin_lands_on_the_active_board_not_the_pinned_ledger(deps):
    """A pin is group-wide (§13): confirming a proposal made under an older
    active ledger must stamp and refresh the CURRENT active board."""
    from expensir.db.models import Group, Ledger, PendingIntent
    from expensir.llm.wire import WireSetFxRate
    from tests.fakes import FakeLLM
    from tests.test_board import FakeBoardMessenger
    from tests.test_nl import ALICE, arrange_group, mention

    deps.client = FakeBoardMessenger()
    await arrange_group(deps)
    deps.llm = FakeLLM([WireSetFxRate(base="USD", quote="SGD", rate="1.35")])
    await mention(deps, "pin the rate 1 usd = 1.35 sgd")
    # the active ledger moves between propose and confirm
    await dispatch(message_update(update_id=40, text="/newledger Tokyo"), deps)
    async with deps.session_factory() as session:
        pending_id = (await session.execute(select(PendingIntent))).scalar_one().id

    outbound = await dispatch(
        callback_update(update_id=41, data=f"v1:confirm:{pending_id}", from_user=ALICE), deps
    )

    async with deps.session_factory() as session:
        group = (await session.execute(select(Group))).scalar_one()
        action = (
            await session.execute(select(Action).where(Action.kind == "set_fx_rate"))
        ).scalar_one()
        tokyo = (await session.execute(select(Ledger).where(Ledger.name == "Tokyo"))).scalar_one()
    assert group.active_ledger_id == tokyo.id
    assert action.ledger_id == tokyo.id  # NOT the propose-time pinned ledger
    board_edits = [a for a in outbound if a.kind == "edit_message" and "• Board" in a.text]
    assert [e.message_id for e in board_edits] == [tokyo.board_message_id]


async def test_duplicate_global_api_rows_are_blocked_by_the_unique_backstop(deps):
    """Concurrent unlocked reads race to insert the same EUR leg (§5): the
    partial unique index must reject the second row."""
    from sqlalchemy.exc import IntegrityError

    async with deps.session_factory() as session:
        session.add(api_row("USD", 1.08))
        await session.commit()
    async with deps.session_factory() as session:
        session.add(api_row("USD", 1.09))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_nl_me_scope_convert_speaks_in_the_second_person(deps):
    from expensir.llm.wire import WireShowBalance
    from tests.fakes import FakeLLM
    from tests.test_nl import arrange_group, mention

    await arrange_group(deps)
    await dispatch(message_update(update_id=20, text="/equal 20 USD taxi"), deps)
    deps.llm = FakeLLM([WireShowBalance(scope="me", convert_to="USD")])

    (reply,) = await mention(deps, "what do I owe in usd", update_id=21, message_id=22)

    assert "You're owed ≈ USD 10.00" in reply.text  # never third-person about yourself
    assert "Alice" not in reply.text


async def test_rates_ignores_fully_settled_buckets(deps):
    deps.fx = FakeFx({"USD": 1.08, "SGD": 1.46})
    await dispatch(bot_added_update(chat_id=-42), deps)
    await dispatch(message_update(update_id=3, chat_id=-42, text="/homecurrency SGD"), deps)
    await dispatch(message_update(update_id=4, chat_id=-42, text="hi", from_user=BOB), deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/equal 21.60 USD dinner"), deps)
    await dispatch(
        message_update(update_id=6, chat_id=-42, text="/settle @bob @alice 10.80 USD"), deps
    )

    [reply] = await dispatch(message_update(update_id=7, chat_id=-42, text="/rates"), deps)

    # the USD bucket nets to zero: balance and board hide it, so /rates must too
    assert "1 USD" not in reply.text


async def test_nl_convert_answers_immediately_as_a_read(deps):
    from expensir.llm.wire import WireShowBalance
    from tests.fakes import FakeLLM
    from tests.test_nl import arrange_group, mention

    await arrange_group(deps)
    await dispatch(message_update(update_id=20, chat_id=-100500, text="/equal 20 USD taxi"), deps)
    deps.llm = FakeLLM([WireShowBalance(convert_to="USD")])

    (reply,) = await mention(deps, "convert everything to usd", update_id=21, message_id=22)

    assert "≈ in USD" in reply.text


async def test_nl_fetch_and_pin_points_at_the_slash_command(deps):
    from expensir.llm.wire import WireSetFxRate
    from tests.fakes import FakeLLM
    from tests.test_nl import arrange_group, mention

    await arrange_group(deps)
    deps.llm = FakeLLM([WireSetFxRate(base="USD", quote="SGD", rate=None)])

    (reply,) = await mention(deps, "pin today's usd rate")

    assert "/setrate USD SGD" in reply.text  # fetch-and-pin is slash-only
    assert await read_pins(deps) == []


async def test_a_pin_resolves_both_directions_and_beats_the_api(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)
    await dispatch(message_update(update_id=3, chat_id=-42, text="/setrate USD SGD 1.35"), deps)
    group_id = await group_id_of(deps, -42)

    async with deps.session_factory() as session:
        # an API path for the same pair exists; the pin must win (§7.5)
        session.add_all([api_row("USD", 1.08), api_row("SGD", 1.46)])
        await session.commit()

    async with deps.session_factory() as session:
        pinned = await resolve_rate(session, group_id, "USD", "SGD", today=TODAY)
        reverse = await resolve_rate(session, group_id, "SGD", "USD", today=TODAY)

    assert pinned is not None and pinned.source == "manual"
    assert pinned.rate == 1.35
    assert reverse is not None and reverse.source == "manual"
    assert reverse.rate == 1 / 1.35  # reciprocal: the pin covers the unordered pair


async def read_pins(deps) -> list[FxRate]:
    async with deps.session_factory() as session:
        return list(
            (await session.execute(select(FxRate).where(FxRate.source == "manual"))).scalars().all()
        )


async def latest_action(deps) -> Action:
    async with deps.session_factory() as session:
        return (await session.execute(select(Action).order_by(Action.id.desc()))).scalars().first()


async def test_api_rates_triangulate_via_eur_and_pins_never_serve_as_legs(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)
    # a wildly-off pin on USD/EUR: if triangulation ever used it as a leg, the
    # JPY→USD figure below would be unrecognizable (§7.5: API rows only)
    await dispatch(message_update(update_id=3, chat_id=-42, text="/setrate USD EUR 9.9"), deps)
    group_id = await group_id_of(deps, -42)

    async with deps.session_factory() as session:
        session.add_all([api_row("USD", 1.08), api_row("JPY", 160.0)])
        await session.commit()

    async with deps.session_factory() as session:
        cross = await resolve_rate(session, group_id, "JPY", "USD", today=TODAY)
        from_eur = await resolve_rate(session, group_id, "EUR", "JPY", today=TODAY)
        unsupported = await resolve_rate(session, group_id, "USD", "SGD", today=TODAY)

    assert cross is not None and cross.source == "api"
    assert cross.rate == 1.08 / 160.0
    assert from_eur is not None and from_eur.rate == 160.0
    assert unsupported is None  # no rate: callers render (≈ n/a), never block


async def test_a_yesterday_api_rate_still_resolves_but_is_flagged_stale(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)
    group_id = await group_id_of(deps, -42)

    async with deps.session_factory() as session:
        session.add(api_row("USD", 1.08, fetched_on=date(2026, 7, 10)))
        await session.commit()

    async with deps.session_factory() as session:
        resolved = await resolve_rate(session, group_id, "EUR", "USD", today=TODAY)

    assert resolved is not None
    assert resolved.stale  # renders with its date until a refetch lands (§7.5)


async def test_undo_restores_the_prior_pin_and_redo_reinstates_the_new_one(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)
    await dispatch(message_update(update_id=3, chat_id=-42, text="/setrate USD SGD 1.35"), deps)
    await dispatch(message_update(update_id=4, chat_id=-42, text="/setrate USD SGD 1.40"), deps)
    repin = await latest_action(deps)

    await dispatch(callback_update(update_id=6, chat_id=-42, data=f"v1:undo:{repin.id}"), deps)
    [pin] = await read_pins(deps)
    assert pin.rate == 1.35

    await dispatch(callback_update(update_id=7, chat_id=-42, data=f"v1:redo:{repin.id}"), deps)
    [pin] = await read_pins(deps)
    assert pin.rate == 1.4


async def test_undo_of_a_first_pin_unpins_the_pair(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)
    await dispatch(message_update(update_id=3, chat_id=-42, text="/setrate USD SGD 1.35"), deps)
    action = await latest_action(deps)

    outbound = await dispatch(
        callback_update(update_id=6, chat_id=-42, data=f"v1:undo:{action.id}"), deps
    )

    assert "undone" in outbound[0].text.lower()
    assert await read_pins(deps) == []


async def test_repinning_either_direction_restates_the_one_row_for_the_pair(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)

    await dispatch(message_update(update_id=3, chat_id=-42, text="/setrate USD SGD 1.35"), deps)
    # the reverse direction is the SAME unordered pair (§7.5): restated, not added
    await dispatch(message_update(update_id=4, chat_id=-42, text="/setrate SGD USD 0.75"), deps)

    [pin] = await read_pins(deps)
    assert (pin.base_currency, pin.quote_currency, pin.rate) == ("SGD", "USD", 0.75)

    async with deps.session_factory() as session:
        actions = (await session.execute(select(Action).order_by(Action.id))).scalars().all()
    assert [a.kind for a in actions] == ["set_fx_rate", "set_fx_rate"]
    first, second = actions
    # undo restores from before_image (§8): no pin, then the first pin as stated
    assert first.before_image == {"pin": None}
    assert second.before_image["pin"]["base"] == "USD"
    assert second.before_image["pin"]["rate"] == 1.35


async def test_autorate_unpins_the_pair_and_undo_restores_the_pin(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)
    await dispatch(message_update(update_id=3, chat_id=-42, text="/setrate USD SGD 1.35"), deps)

    # either direction names the same unordered pair (§7.5)
    [reply] = await dispatch(
        message_update(update_id=4, chat_id=-42, text="/autorate SGD USD"), deps
    )

    assert "live" in reply.text.lower()
    assert await read_pins(deps) == []

    action = await latest_action(deps)
    assert action.kind == "clear_fx_rate"
    await dispatch(callback_update(update_id=6, chat_id=-42, data=f"v1:undo:{action.id}"), deps)
    [pin] = await read_pins(deps)
    assert pin.rate == 1.35


async def test_autorate_on_an_unpinned_pair_guides_and_writes_nothing(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)

    [reply] = await dispatch(
        message_update(update_id=3, chat_id=-42, text="/autorate USD SGD"), deps
    )

    assert "isn't pinned" in reply.text
    assert "/rates" in reply.text
    async with deps.session_factory() as session:
        assert (await session.execute(select(Action))).scalars().all() == []


async def test_bare_setrate_fetches_todays_rate_and_pins_it_frozen(deps):
    deps.fx = FakeFx({"USD": 1.08, "SGD": 1.46})  # EUR-based, like Frankfurter (§7.5)
    await dispatch(bot_added_update(chat_id=-42), deps)

    [reply] = await dispatch(
        message_update(update_id=3, chat_id=-42, text="/setrate USD SGD"), deps
    )

    [pin] = await read_pins(deps)
    assert pin.source == "manual"  # frozen from this moment, never auto-refreshed
    assert pin.rate == pytest.approx(1.46 / 1.08)
    assert "Frozen" in reply.text


async def test_bare_setrate_with_fx_down_rejects_loudly_and_writes_nothing(deps):
    deps.fx = UnavailableFx()
    await dispatch(bot_added_update(chat_id=-42), deps)

    [reply] = await dispatch(
        message_update(update_id=3, chat_id=-42, text="/setrate USD SGD"), deps
    )

    assert "/setrate USD SGD 1.35" in reply.text  # the explicit-number road stays open
    assert await read_pins(deps) == []
    async with deps.session_factory() as session:
        assert (await session.execute(select(Action))).scalars().all() == []


async def test_setrate_rejects_bad_input_loudly_and_writes_nothing(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)

    # unrecognized code: reject with a correction, never fold into anything (ADR-0009)
    [reply] = await dispatch(
        message_update(update_id=3, chat_id=-42, text="/setrate USD ZZZ 1.2"), deps
    )
    assert "ZZZ" in reply.text

    # a currency against itself
    [reply] = await dispatch(
        message_update(update_id=4, chat_id=-42, text="/setrate USD USD 1.2"), deps
    )
    assert "itself" in reply.text

    # malformed: usage with a copy-pasteable example
    [reply] = await dispatch(message_update(update_id=5, chat_id=-42, text="/setrate USD"), deps)
    assert "Usage" in reply.text and "/setrate USD SGD 1.35" in reply.text

    assert await read_pins(deps) == []
    async with deps.session_factory() as session:
        assert (await session.execute(select(Action))).scalars().all() == []


async def test_setrate_pins_a_manual_display_rate_for_the_group(deps):
    await dispatch(bot_added_update(chat_id=-42), deps)

    [reply] = await dispatch(
        message_update(update_id=3, chat_id=-42, text="/setrate USD SGD 1.35"), deps
    )

    assert "1.35" in reply.text
    assert "USD" in reply.text and "SGD" in reply.text
    [pin] = await read_pins(deps)
    assert (pin.base_currency, pin.quote_currency, pin.rate) == ("USD", "SGD", 1.35)
    assert pin.source == "manual"
    assert pin.group_id is not None
