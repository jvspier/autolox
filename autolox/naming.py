"""Roster-name to Loxone-name transformation.

Rules from docs/workflow.md, decided by the operator (not this code):

- lowercase
- spaces become dots
- name particles ("de", "van", "der", ...) are kept and dotted
- accents folded to plain ASCII via `unidecode` — this is the point where
  `unicodedata.normalize('NFKD', ...)` would fail: it mangles Łukasz to ukasz,
  ø to nothing, đ to d only inconsistently. `unidecode` handles all of them.
- dashes are preserved
- collisions (same result for two different rows) are surfaced to a human
  reviewer — never silently auto-numbered

This module is pure functions on strings. Testable without a Miniserver.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from unidecode import unidecode


# Anything that isn't a-z, 0-9, dot, or dash after unidecode+lowercase gets
# flagged so a human can look. We don't strip it silently.
_ALLOWED = re.compile(r"^[a-z0-9.\-]+$")


@dataclass(frozen=True)
class TransformedName:
    """One row of the preview table."""
    roster: str
    loxone: str
    warnings: tuple[str, ...] = ()

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings)


def transform(roster: str) -> TransformedName:
    """Apply the naming rules to a single roster string.

    Returns the transformed name + any warnings that a human should read.
    Never mutates or invents — if a row is unusable it's returned as-is
    with warnings so the operator can fix or skip it manually.
    """
    warnings: list[str] = []

    stripped = roster.strip()
    if not stripped:
        return TransformedName(roster=roster, loxone="", warnings=("blank line",))

    # Fold accents and special characters (ø, ú, ß, đ, ł, ...) to ASCII.
    folded = unidecode(stripped)
    if folded != stripped:
        # This is a normal, expected case — but worth surfacing so the
        # operator sees what the fold produced.
        warnings.append(f"folded from {stripped!r}")

    # Lowercase and replace whitespace runs with a single dot.
    lower = folded.lower()
    dotted = re.sub(r"\s+", ".", lower)

    # A single-word roster entry (no space -> no dot in the result) used
    # to warn — but it's a legitimate input (nicknames, mononyms,
    # deliberate short usernames), so it's silently accepted now.

    # Anything left that isn't a-z 0-9 . - is a problem.
    if not _ALLOWED.match(dotted):
        # Show the offending characters
        bad = sorted({ch for ch in dotted if not _ALLOWED.match(ch)})
        warnings.append(f"unexpected characters: {bad!r}")

    return TransformedName(roster=roster, loxone=dotted, warnings=tuple(warnings))


def transform_all(roster: list[str]) -> list[TransformedName]:
    """Transform a full roster, flagging duplicates.

    Duplicate detection is on the transformed form (`loxone`) — if two rows
    map to the same result, both get a warning. The operator decides how to
    resolve (rename one, append a digit, drop one, etc.). This module never
    resolves collisions itself — CLAUDE.md and docs/workflow.md both require
    a human in the loop.
    """
    rows = [transform(r) for r in roster]
    seen: dict[str, list[int]] = {}
    for i, row in enumerate(rows):
        if row.loxone:
            seen.setdefault(row.loxone, []).append(i)
    for loxone, positions in seen.items():
        if len(positions) > 1:
            for i in positions:
                rows[i] = TransformedName(
                    roster=rows[i].roster,
                    loxone=rows[i].loxone,
                    warnings=rows[i].warnings + (
                        f"collides with row(s) {[p+1 for p in positions if p != i]}",
                    ),
                )
    return rows
