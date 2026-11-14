"""Name rules shared by the validators: SCS tokens such as package, skin and vehicle names.

The game stores unit names as "tokens": 1 to 12 characters from ``a-z``, ``0-9`` and
``_``. Before this module existed, each validator had its own regular expression and
the limits had drifted apart (see review finding F15). Every check of a token now goes
through :func:`token_issue`, so the rule lives in one place; callers only choose the
length limit and word their own message.

Keep this module free of imports from the rest of Paintbooth, like :mod:`paintbooth.util`.
"""

from __future__ import annotations

import enum
import re

__all__ = [
    "TOKEN_CHARS_RE",
    "TOKEN_MAX",
    "TOKEN_RE",
    "TokenIssue",
    "is_token",
    "token_issue",
    "validate_token",
]

TOKEN_MAX = 12
TOKEN_CHARS_RE = re.compile(r"^[a-z0-9_]+$")
# The complete rule as one pattern, for callers that only need a yes/no answer.
TOKEN_RE = re.compile(rf"^[a-z0-9_]{{1,{TOKEN_MAX}}}$")


class TokenIssue(enum.Enum):
    """Why a name is not a valid token."""

    EMPTY = "empty"
    CHARS = "chars"
    LENGTH = "length"


def token_issue(name: str, max_len: int | None = TOKEN_MAX) -> TokenIssue | None:
    """Return what is wrong with ``name`` as a token, or None if it is valid.

    Args:
        name: The name to check, exactly as typed (no trimming).
        max_len: The longest allowed name. Skins use 10 because the exporter appends
            ``_<i>``; None skips the length check.
    """
    if not name:
        return TokenIssue.EMPTY
    if not TOKEN_CHARS_RE.match(name):
        return TokenIssue.CHARS
    if max_len is not None and len(name) > max_len:
        return TokenIssue.LENGTH
    return None


def is_token(name: str, max_len: int | None = TOKEN_MAX) -> bool:
    """Return True if ``name`` is a valid token of at most ``max_len`` characters."""
    return token_issue(name, max_len) is None


def validate_token(name: str, label: str = "Internal name", max_len: int | None = TOKEN_MAX) -> str | None:
    """Return a user-facing error message for an invalid token, or None if ``name`` is valid.

    Args:
        name: The name to check.
        label: How the message refers to the field, e.g. ``"Package name"``.
        max_len: As for :func:`token_issue`.
    """
    issue = token_issue(name, max_len)
    if issue is TokenIssue.EMPTY:
        return f"{label} is empty"
    if issue is TokenIssue.CHARS:
        return f"{label} may only use a-z, 0-9 and _"
    if issue is TokenIssue.LENGTH:
        return f"{label} is longer than {max_len} characters"
    return None
