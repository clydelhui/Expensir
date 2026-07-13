"""Central diagnostic-log setup (ADR-0015).

Named logsetup, not logging — the obvious name shadows the stdlib module.
Metadata at INFO, message content at DEBUG only: the level is the privacy
boundary. LOG_LEVEL drives expensir loggers alone; noisy libraries stay
pinned to WARNING so LOG_LEVEL=DEBUG remains readable.
"""

import logging
import logging.handlers
import sys
from contextvars import ContextVar
from typing import Any

# the update being handled, set by the transports; correlates every line of one
# update's trace without threading ids through signatures
current_update_id: ContextVar[int | None] = ContextVar("current_update_id", default=None)

# libraries that flood INFO/DEBUG with per-request and connection-pool chatter
_PINNED_TO_WARNING = ("httpx", "httpcore", "sqlalchemy")

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(update_tag)s%(message)s"
_DATEFMT = "%H:%M:%S"

# handlers this module installed, so a re-setup (tests) replaces instead of stacking
_installed: list[logging.Handler] = []


class UpdateIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        update_id = current_update_id.get()
        record.update_id = update_id
        record.update_tag = "" if update_id is None else f"[u{update_id}] "
        return True


_LEVEL_NAMES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in _installed:
        root.removeHandler(handler)
    _installed.clear()

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        # dev-only (never set in prod: Cloud Run ingests stderr); capped so a
        # forgotten local bot can't fill a disk
        handlers.append(
            logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3)
        )
    for handler in handlers:
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        handler.addFilter(UpdateIdFilter())
        root.addHandler(handler)
        _installed.append(handler)

    # a mis-set LOG_LEVEL (typo, stray whitespace from .env) must not take the
    # bot down at boot — it's a diagnostics knob, so degrade to the default
    level_name = level.strip().upper()
    if level_name not in _LEVEL_NAMES:
        logging.getLogger(__name__).warning("unknown LOG_LEVEL %r — falling back to INFO", level)
        level_name = "INFO"
    logging.getLogger("expensir").setLevel(level_name)
    for lib in _PINNED_TO_WARNING:
        logging.getLogger(lib).setLevel(logging.WARNING)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def update_log_fields(update: dict[str, Any]) -> str:
    """chat/user/kind summary of a raw Telegram update — ids only, never text (ADR-0015).

    Never raises: this runs in the transports before their error boundaries, so a
    forged shape (a non-dict body) must degrade to None fields, not a 500. The kinds
    tuple mirrors dispatch()'s branches — keep in sync; anything else is kind=other.
    """
    kinds = ("message", "callback_query", "my_chat_member")
    kind = next((k for k in kinds if k in update), "other")
    body = _dict(update.get(kind))
    chat = (_dict(body.get("chat")) or _dict(_dict(body.get("message")).get("chat"))).get("id")
    user = _dict(body.get("from")).get("id")
    return f"chat={chat} user={user} kind={kind}"
