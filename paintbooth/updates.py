"""Check GitHub for a newer Paintbooth release (only tells the user; nothing is downloaded).

The GUI calls :func:`check` in the background at start-up. The user is often offline, so
every failure (no network, rate limit, unexpected reply) simply means "no update known".
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import urllib.request

log = logging.getLogger(__name__)

REPO = "AL3X-7777/paintbooth"  # github.com/<owner>/<repository>
REPO_URL = f"https://github.com/{REPO}"
RELEASES_URL = f"https://github.com/{REPO}/releases"
#: The version on the main branch, used while no release is published.
MAIN_INIT_URL = f"https://raw.githubusercontent.com/{REPO}/main/paintbooth/__init__.py"
_VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.M)
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"


def parse_version(text: str) -> tuple[int, ...]:
    """'v0.10.0' -> (0, 10, 0); anything unreadable -> ()."""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def is_newer(latest: str, current: str) -> bool:
    """Return True if version ``latest`` is greater than ``current`` (missing parts count as 0)."""
    a, b = parse_version(latest), parse_version(current)
    if not a or not b:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def latest_release(timeout: float = 5.0) -> tuple[str, str] | None:
    """Return (version, page url) of the newest published release, or None.

    None means offline, no releases, a draft/pre-release, or any unexpected answer.

    Args:
        timeout: Seconds to wait for GitHub.
    """
    try:
        req = urllib.request.Request(
            API_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "Paintbooth-update-check"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https:// URL)
            data = json.loads(r.read().decode("utf-8"))
        if not isinstance(data, dict):
            return None
        if data.get("draft") or data.get("prerelease"):
            return None
        tag = str(data.get("tag_name") or "")
        url = str(data.get("html_url") or "")
        # the link is opened in the browser later: accept nothing but this project's own pages
        return tag.lstrip("vV"), url if url.startswith(RELEASES_URL + "/") else RELEASES_URL
    except (OSError, ValueError, http.client.HTTPException) as e:
        # URLError/HTTPError/timeouts are OSErrors; bad JSON is a ValueError; a cut-off or garbled
        # reply is an HTTPException
        log.debug("update check failed: %s", e)
        return None


def main_version(timeout: float = 5.0) -> str | None:
    """Return the version on the repository's main branch, or None (offline, unreadable)."""
    try:
        req = urllib.request.Request(MAIN_INIT_URL, headers={"User-Agent": "Paintbooth-update-check"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https:// URL)
            m = _VERSION_RE.search(r.read(64 * 1024).decode("utf-8", "replace"))
        return m.group(1) if m else None
    except (OSError, ValueError, http.client.HTTPException) as e:
        log.debug("update check failed: %s", e)
        return None


def check(current: str) -> tuple[str, str] | None:
    """Return (version, url) if GitHub has a newer version than ``current``, else None.

    A published release wins (its page has the installer); without one, the version on the
    main branch is compared, and the link goes to the repository.
    """
    rel = latest_release()
    if rel and is_newer(rel[0], current):
        return rel
    if rel is None:
        ver = main_version()
        if ver and is_newer(ver, current):
            return ver, REPO_URL
    return None
