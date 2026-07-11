"""Minor-unit money: parsing and formatting only (§3, ADR-0008). No floats, ever."""

from decimal import Decimal, InvalidOperation, localcontext

# minor_digits per ISO 4217 code; unknown codes default to 2 (§3)
MINOR_DIGITS: dict[str, int] = {
    # 0-decimal currencies
    "JPY": 0,
    "KRW": 0,
    "VND": 0,
    "CLP": 0,
    "ISK": 0,
    # 3-decimal currencies
    "BHD": 3,
    "KWD": 3,
    "OMR": 3,
    "TND": 3,
    "JOD": 3,
}
DEFAULT_MINOR_DIGITS = 2

# beyond any real ledger; anything larger would overflow the DB int64 at flush
# time and crash the update pipeline instead of replying
MAX_MINOR = 10**15


def minor_digits(currency: str) -> int:
    return MINOR_DIGITS.get(currency.upper(), DEFAULT_MINOR_DIGITS)


def round_half_up(scaled: Decimal) -> int:
    """Half-up at the minor-unit boundary — the ONE rounding rule for money-shaped
    display and parsing alike (§3, §7.6)."""
    with localcontext() as ctx:
        ctx.rounding = "ROUND_HALF_UP"
        return int(scaled.to_integral_value())


def to_minor(amount_str: str, currency: str) -> tuple[int, bool]:
    """Parse a typed major-unit amount into integer minor units.

    Returns (minor, was_rounded); rounds half-up at the minor-unit boundary,
    and the caller must surface the rounding visibly when was_rounded is True.
    """
    try:
        amount = Decimal(amount_str)
    except InvalidOperation:
        raise ValueError(f"not an amount: {amount_str!r}") from None
    if amount <= 0:
        raise ValueError(f"amount must be positive: {amount_str!r}")

    code = currency.upper()
    scaled = amount * (10 ** minor_digits(code))
    minor = round_half_up(scaled)
    if minor == 0:
        raise ValueError(
            f"{amount_str} is smaller than the smallest {code} unit — nothing to record"
        )
    if minor > MAX_MINOR:
        raise ValueError(f"that amount is too large to record: {amount_str}")
    return minor, minor != scaled


def fmt(minor: int, currency: str) -> str:
    """Display only: 'USD 60.50', 'JPY 6001', 'KWD 6.500' (§3)."""
    code = currency.upper()
    digits = minor_digits(code)
    if digits == 0:
        return f"{code} {minor}"
    sign = "-" if minor < 0 else ""
    units, cents = divmod(abs(minor), 10**digits)
    return f"{code} {sign}{units}.{cents:0{digits}d}"
