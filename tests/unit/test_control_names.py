"""Every control the templates can render has a name a screen reader can read out.

Over the template files rather than rendered pages, so a control behind a state no test
happens to reach is still checked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = sorted((Path(__file__).resolve().parents[2] / "app" / "templates").glob("*.html"))
_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_CONTROL = re.compile(r"<(select|textarea|input)\b[^>]*>", re.DOTALL)
_ID = re.compile(r'\bid="([^"]+)"')
# These name themselves, or are never presented.
_SELF_NAMED_TYPES = re.compile(r'\btype="(hidden|submit|button)"')


def _has_a_name(source: str, match: re.Match) -> bool:
    tag = match.group(0)
    if "aria-label" in tag or _SELF_NAMED_TYPES.search(tag):
        return True
    identifier = _ID.search(tag)
    if identifier and f'for="{identifier.group(1)}"' in source:
        return True
    before = source[: match.start()]
    return before.rfind("<label") > before.rfind("</label>")


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.name)
def test_no_control_is_left_without_a_name(template: Path) -> None:
    source = _COMMENT.sub("", template.read_text(encoding="utf-8"))

    unnamed = [
        match.group(0) for match in _CONTROL.finditer(source) if not _has_a_name(source, match)
    ]

    assert unnamed == []
