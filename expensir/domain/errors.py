"""User-facing rejections: the whole intent fails with guidance; nothing commits (§0.9)."""


class Rejection(Exception):
    """The handler replies with str(exc); the intent's writes are rolled back."""
