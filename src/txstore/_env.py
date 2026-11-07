"""Read ``TX_STORE_*`` environment variables.

The only module that reads `os.environ`. Each helper returns one variable's parsed value, or None
when it is unset or empty; callers resolve an explicit argument first, then the variable, then
their default. Names → [CONFIG.md](CONFIG.md#environment-variables); grammar →
[VALIDATION.md](VALIDATION.md#environment-values).
"""

from __future__ import annotations

import os
import re

ENV_PREFIX = "TX_STORE_"

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
# `int()` also takes `1_0` and non-ASCII digits; the grammar is plain decimal → VALIDATION.md.
_INTEGER_RE = re.compile(r"[+-]?[0-9]+", re.ASCII)


def _raw(name: str) -> str | None:
    """Return the value of variable `name`, or None when unset or empty.

    Raises:
        ValueError: If `name` lacks the ``TX_STORE_`` prefix (a programming error).
    """
    if not name.startswith(ENV_PREFIX):
        raise ValueError(f"environment variable {name!r} lacks the {ENV_PREFIX} prefix")
    value = os.environ.get(name)
    return value if value else None


def env_str(name: str) -> str | None:
    """Return the string value of `name`, or None when unset or empty."""
    return _raw(name)


def env_bool(name: str) -> bool | None:
    """Return the boolean value of `name`, or None when unset or empty.

    Accepts ``1/0``, ``true/false``, ``yes/no``, and ``on/off`` in any case.

    Raises:
        ValueError: If the value is outside that vocabulary; the message names the variable.
    """
    value = _raw(name)
    if value is None:
        return None
    folded = value.strip().lower()
    if folded in _TRUE:
        return True
    if folded in _FALSE:
        return False
    raise ValueError(f"{name}: expected a boolean (1/0, true/false, yes/no, on/off), got {value!r}")


def env_int(name: str) -> int | None:
    """Return the integer value of `name`, or None when unset or empty.

    Raises:
        ValueError: If the value is not a decimal integer; the message names the variable.
    """
    value = _raw(name)
    if value is None:
        return None
    stripped = value.strip()
    if not _INTEGER_RE.fullmatch(stripped):
        raise ValueError(f"{name}: expected an integer, got {value!r}")
    return int(stripped, 10)
