"""Diagnostic logging (ADR-0015): central setup, update correlation, seam trace."""

import logging

import pytest

from expensir import logsetup
from expensir.core.handler import dispatch
from expensir.logsetup import UpdateIdFilter, setup_logging, update_log_fields
from expensir.transports.poll import poll_once
from tests.factories import callback_update, message_update
from tests.test_poll import FakePollClient


@pytest.fixture
def trace(caplog):
    """caplog wired the way our real handlers are: update-id stamping included."""
    caplog.handler.addFilter(UpdateIdFilter())
    caplog.set_level(logging.INFO, logger="expensir")
    return caplog


@pytest.fixture(autouse=True)
def _fresh_logging_state():
    """setup_logging mutates global logging config; undo it so the rest of the
    suite doesn't run with our handlers/levels installed."""
    root = logging.getLogger()
    root_level = root.level  # setup_logging pins root to INFO; put it back
    yield
    for handler in list(logsetup._installed):
        root.removeHandler(handler)
    logsetup._installed.clear()
    root.setLevel(root_level)
    for name in ("expensir", "httpx", "httpcore", "sqlalchemy"):
        logging.getLogger(name).setLevel(logging.NOTSET)


def test_log_level_drives_expensir_loggers_and_pins_noisy_libs_to_warning():
    setup_logging(level="DEBUG")

    assert logging.getLogger("expensir.core.handler").isEnabledFor(logging.DEBUG)
    for lib in ("httpx", "httpcore", "sqlalchemy"):
        assert not logging.getLogger(lib).isEnabledFor(logging.INFO)
        assert logging.getLogger(lib).isEnabledFor(logging.WARNING)


def test_console_lines_carry_the_current_update_id(capsys):
    setup_logging(level="INFO")

    token = logsetup.current_update_id.set(8123)
    try:
        logging.getLogger("expensir.test").info("received chat=-42")
    finally:
        logsetup.current_update_id.reset(token)
    logging.getLogger("expensir.test").info("outside any update")

    err = capsys.readouterr().err
    assert "[u8123] received chat=-42" in err
    assert "outside any update" in err
    assert "[u8123] outside any update" not in err


def test_log_file_gets_the_same_trace_when_set(tmp_path):
    log_file = tmp_path / "dev.log"
    setup_logging(level="INFO", log_file=str(log_file))

    token = logsetup.current_update_id.set(9)
    try:
        logging.getLogger("expensir.test").info("received chat=-42")
    finally:
        logsetup.current_update_id.reset(token)

    assert "[u9] received chat=-42" in log_file.read_text()


def test_an_invalid_log_level_falls_back_to_info_instead_of_crashing():
    setup_logging(level="DEBUGG")  # a typo in .env must not take the bot down at boot

    assert logging.getLogger("expensir.core.handler").isEnabledFor(logging.INFO)
    assert not logging.getLogger("expensir.core.handler").isEnabledFor(logging.DEBUG)


def test_level_names_survive_stray_whitespace_and_case():
    setup_logging(level=" debug ")  # ".env" files grow trailing spaces easily

    assert logging.getLogger("expensir.core.handler").isEnabledFor(logging.DEBUG)


def test_update_log_fields_never_raises_on_a_forged_non_dict_body():
    # runs before the transports' error boundaries: a raise here would swallow
    # the update behind an unreleased dedupe claim
    assert update_log_fields({"update_id": 5, "message": "hello"}) == (
        "chat=None user=None kind=message"
    )


async def test_poll_traces_an_update_from_received_to_done(deps, trace):
    client = FakePollClient([[message_update(update_id=6, chat_id=-42, text="/start")]])

    await poll_once(deps, client, offset=0)

    [received] = [r for r in trace.records if r.getMessage().startswith("received ")]
    [done] = [r for r in trace.records if "outcome=ok" in r.getMessage()]
    [effects] = [r for r in trace.records if "effects sent" in r.getMessage()]
    assert "chat=-42" in received.getMessage()
    assert "kind=message" in received.getMessage()
    assert "send_message=1" in effects.getMessage()
    assert [received.update_id, effects.update_id, done.update_id] == [6, 6, 6]


async def test_a_crashed_update_logs_outcome_error_with_the_traceback(deps, trace, monkeypatch):
    from expensir.transports import poll as poll_mod

    async def dispatch_that_blows_up(update, deps):
        raise RuntimeError("handler blew up")

    monkeypatch.setattr(poll_mod, "dispatch", dispatch_that_blows_up)
    client = FakePollClient([[message_update(update_id=5, chat_id=-42, text="poison")]])

    await poll_once(deps, client, offset=0)

    [error] = [r for r in trace.records if "outcome=error" in r.getMessage()]
    assert error.update_id == 5
    assert error.exc_info is not None  # the traceback rides along
    assert not [r for r in trace.records if "outcome=ok" in r.getMessage()]


async def test_dispatch_logs_the_resolved_command_intent(deps, trace):
    await dispatch(message_update(update_id=7, chat_id=-42, text="/balance"), deps)

    assert any(r.getMessage() == "intent=cmd:balance" for r in trace.records)


async def test_nl_dispatch_logs_the_wire_kind_but_never_the_text_at_info(deps, trace):
    from tests.fakes import FakeLLM
    from tests.test_nl import DINNER_WITH_SAM, arrange_group, mention

    await arrange_group(deps)
    deps.llm = FakeLLM([DINNER_WITH_SAM])

    await mention(deps, "I paid 40 for dinner, split with Sam")

    assert any(r.getMessage() == "intent=nl:add_expense" for r in trace.records)
    # the sentence itself is content: DEBUG only (ADR-0015)
    assert not any("split with Sam" in r.getMessage() for r in trace.records)


async def test_dispatch_logs_the_callback_verb(deps, trace):
    await dispatch(callback_update(chat_id=-42, data="v1:undo:1"), deps)

    assert any(r.getMessage() == "intent=cb:undo" for r in trace.records)


async def test_webhook_crash_logs_a_tagged_outcome_error_with_the_traceback(
    deps, trace, monkeypatch
):
    """The webhook mirror of the poll crash test: 500 to Telegram, but the trace
    keeps its grammar — a correlated outcome=error carrying the traceback."""
    import httpx

    from expensir.transports.webhook import create_app
    from tests.factories import bot_added_update
    from tests.test_executor import FakeTelegramClient

    async def dispatch_that_blows_up(update, deps):
        raise RuntimeError("handler blew up")

    monkeypatch.setattr("expensir.transports.webhook.dispatch", dispatch_that_blows_up)
    app = create_app(deps=deps, telegram=FakeTelegramClient(), webhook_secret="s3cret")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(
            "/webhook",
            json=bot_added_update(update_id=44, chat_id=-42),
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )

    assert response.status_code == 500
    [error] = [r for r in trace.records if "outcome=error" in r.getMessage()]
    assert error.update_id == 44
    assert error.exc_info is not None  # tagged AND carrying the traceback
    assert not [r for r in trace.records if "outcome=ok" in r.getMessage()]


async def test_webhook_send_failure_after_commit_still_logs_a_done_line(deps, trace):
    """An execute() failure must not leave a received with no matching done."""
    import httpx

    from expensir.transports.webhook import create_app
    from tests.factories import bot_added_update
    from tests.test_webhook import FlakyOnceClient

    app = create_app(deps=deps, telegram=FlakyOnceClient(failures_left=1), webhook_secret="s3cret")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(
            "/webhook",
            json=bot_added_update(update_id=45, chat_id=-42),
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )

    assert response.status_code == 500
    [error] = [r for r in trace.records if "outcome=error" in r.getMessage()]
    assert error.update_id == 45
    assert "claim kept" in error.getMessage()
