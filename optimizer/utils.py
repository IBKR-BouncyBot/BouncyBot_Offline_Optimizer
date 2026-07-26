"""Small dependency-free helpers shared by analysis and report code."""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def timestamp_seconds(value: Any) -> float | None:
    parsed = parse_datetime(value)
    return parsed.timestamp() if parsed else None


def iso_from_timestamp(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return ""
    return datetime.fromtimestamp(value, tz=timezone.utc).replace(microsecond=0).isoformat()


def finite_float(value: Any) -> float | None:
    # ``bool`` is a subclass of ``int`` in Python, but treating JSON ``true`` as
    # a price of 1.0 or an ATR percentage of 1.0 would silently turn malformed
    # evidence into apparently valid market data.
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def finite_int(value: Any) -> int | None:
    """Return an exact finite integer without bool or fraction coercion.

    JSON and SQLite can represent integer metadata as ``14``, ``14.0`` or
    ``"14.0"``.  All three are accepted.  Booleans, fractions, NaN and
    infinities are rejected so malformed evidence cannot silently change an
    ATR period, format version, sequence, or contract identifier.
    """

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # Beyond 2**53 a binary float cannot represent every integer. Reject
        # that input form rather than silently accepting a rounded identifier,
        # sequence, byte limit, or ATR period. Large exact values remain
        # available through integer, Decimal, or string input.
        if (
            not math.isfinite(value)
            or not value.is_integer()
            or abs(value) > 2**53
        ):
            return None
        return int(value)
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None
    else:
        return None
    if (
        not number.is_finite()
        or number != number.to_integral_value()
        # Avoid an attacker-controlled exponent causing an enormous integer
        # allocation while parsing untrusted manifest or SQLite metadata.
        or number.adjusted() > 100
    ):
        return None
    try:
        return int(number)
    except (OverflowError, ValueError):
        return None


def truthy(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        number = finite_float(value)
        if number == 1.0:
            return True
        if number == 0.0:
            return False
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n", ""}:
        return False
    return default


def median(values: Iterable[float | None]) -> float | None:
    clean = [number for value in values if (number := finite_float(value)) is not None]
    return float(statistics.median(clean)) if clean else None


def percentile(values: Iterable[float | None], percentile_value: float) -> float | None:
    clean = sorted(number for value in values if (number := finite_float(value)) is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    fraction = min(1.0, max(0.0, float(percentile_value)))
    position = fraction * (len(clean) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return clean[lower]
    weight = position - lower
    return clean[lower] * (1.0 - weight) + clean[upper] * weight


def mean(values: Iterable[float | None]) -> float | None:
    clean = [number for value in values if (number := finite_float(value)) is not None]
    return sum(clean) / len(clean) if clean else None


def safe_name(value: str, fallback: str = "unknown") -> str:
    text = "".join(character if character.isalnum() or character in "-_." else "_" for character in str(value))
    text = text.strip("._")
    text = (text or fallback)[:100]
    stem = text.split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update(f"COM{number}" for number in range(1, 10))
    reserved.update(f"LPT{number}" for number in range(1, 10))
    if stem in reserved:
        text = f"_{text}"
    return text


def relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return str(Path(path).resolve())


def ticker_folder_name(ticker: str) -> str:
    """Return a stable, Windows-safe, collision-resistant report folder name."""
    import hashlib

    original = str(ticker).strip().upper()
    sanitized = safe_name(original, fallback="UNKNOWN")
    if sanitized == original and len(original) <= 100:
        return sanitized
    suffix = hashlib.sha256(original.encode("utf-8")).hexdigest()[:10]
    stem = sanitized[: max(1, 89)]
    return f"{stem}-{suffix}"
