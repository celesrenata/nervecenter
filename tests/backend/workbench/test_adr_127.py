"""ADR test: NS-ADR-127 is unique and has its three sections.

**Validates: Requirements 11.1**
"""

import re
from pathlib import Path

DECISIONS = Path(__file__).resolve().parents[3] / ".kiro/specs/nerve-shield/decisions.md"


def _rows() -> list[str]:
    text = DECISIONS.read_text(encoding="utf-8")
    return [ln for ln in text.splitlines() if re.match(r"\|\s*NS-ADR-127\s*\|", ln)]


def test_identifier_is_unique_row() -> None:
    assert len(_rows()) == 1


def test_identifier_appears_once_as_a_row_id() -> None:
    ids = re.findall(r"^\|\s*(NS-ADR-\d+)\s*\|", DECISIONS.read_text(encoding="utf-8"), re.M)
    assert ids.count("NS-ADR-127") == 1


def test_status_is_proposed_or_accepted() -> None:
    cells = [c.strip() for c in _rows()[0].split("|")]
    assert cells[2] in {"PROPOSED", "ACCEPTED"}


def test_three_sections_exist() -> None:
    row = _rows()[0]
    a = re.search(r"Section \(a\):[^.]*NS-ADR-106", row)
    b = re.search(r"Section \(b\):\s*storage", row, re.I)
    c = re.search(r"Section \(c\):\s*isolation", row, re.I)
    assert a, "section (a) extension of NS-ADR-106 missing"
    assert b, "section (b) storage missing"
    assert c, "section (c) isolation missing"
    assert a.start() < b.start() < c.start()
