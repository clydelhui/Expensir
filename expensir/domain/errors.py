"""User-facing rejections: the whole intent fails with guidance; nothing commits (§0.9)."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from expensir.db.models import Expense, User
    from expensir.intents.schema import Intent


class Rejection(Exception):
    """The handler replies with str(exc); the intent's writes are rolled back."""


class AmbiguousRef(Exception):
    """A reference matching more than one member (§10, §13): never guessed.

    The proposal paths render the candidates as a pick-list stage; paths that
    cannot pick (reads) convert to @username guidance. When raised out of
    apply_intent, `intent` carries what was being applied, so the slash door
    can park it as a proposal instead (§0.7)."""

    def __init__(self, ref: str, candidates: "list[User]") -> None:
        super().__init__(ref)
        self.ref = ref
        self.candidates = candidates
        self.intent: Intent | None = None


class AmbiguousExpense(Exception):
    """A descriptive expense reference matching several expenses (§11 tertiary,
    §13): the expense pick-list's raw material — never guessed."""

    def __init__(self, query: str, candidates: "list[Expense]") -> None:
        super().__init__(query)
        self.query = query
        self.candidates = candidates  # newest first
