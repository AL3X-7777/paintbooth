"""Print the release notes for one version from CHANGELOG.md (used by the release workflow).

Usage: ``python3 .github/release_notes.py 0.10.0 > notes.md``. The text under the
``## 0.10.0`` heading of CHANGELOG.md (up to the next ``## `` heading) is printed, followed by
short install instructions. Without a matching section the notes start with "Paintbooth X.Y.Z".
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

FOOTER = (
    "\n**Install:** download *Paintbooth Setup {v}.exe* below and run it. "
    "Your projects and settings are kept."
    "\n\nNo installer? Unzip the portable zip anywhere and run `Paintbooth.exe`."
    "\n\n[Code signing policy and privacy](https://github.com/AL3X-7777/paintbooth/blob/main/CODE_SIGNING.md)"
)


def changelog_section(text: str, version: str) -> str:
    """Return the body of the ``## <version>`` section of a changelog, stripped, or "".

    The heading must name the whole version (``0.1`` does not match ``## 0.1.0``); anything after
    it on the heading line, such as a date, is ignored.
    """
    m = re.search(rf"^## {re.escape(version)}(?![\w.]).*?$(.*?)(?=^## |\Z)", text, re.S | re.M)
    return m.group(1).strip() if m else ""


def release_notes(version: str, changelog: str) -> str:
    """Compose the full release notes (changelog section or a title, then the install footer)."""
    head = changelog_section(changelog, version) or f"Paintbooth {version}"
    return head + "\n" + FOOTER.format(v=version)


def main(argv: list[str]) -> int:
    """Print the notes for ``argv[1]`` using ``CHANGELOG.md`` in the current folder."""
    path = Path("CHANGELOG.md")
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    print(release_notes(argv[1], text))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
