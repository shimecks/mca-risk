"""Merchant-name normalization and pair scoring (METRICS.md §6 Q1).

Q1: "Normalize the name: casefold, strip punctuation, standardize suffixes (LLC/L.L.C., Inc/Inc.),
'&' -> 'and'. Block on the first token. ... fuzzy token-set similarity >= 92 within a block."

:func:`normalize_name` turns a raw name into a :class:`NormName`:

* casefold; ``&`` -> ``and``; ``L.L.C.`` / ``L.L.C`` -> ``llc``; every other punctuation -> space;
  whitespace collapsed.
* A trailing legal-entity suffix is split off and standardized (``llc``, ``inc``, ``corp``, ``co``,
  ``ltd``...). ``core`` is the name without it; ``suffix`` is ``None`` when the name has none (the
  "suffix dropped" variant, or a DBA).
* ``first`` is the blocking token (first token of ``core``).

:func:`name_score` is the fuzzy similarity used inside a block. Two names whose suffixes are both
present and differ (``Amber Physical Therapy Co`` vs ``... Corp``) are different legal entities: score 0.
Otherwise the score is rapidfuzz ``token_set_ratio`` on the cores (Q1). (A plain-ratio variant would
merge ``Keystone Market LLC`` with ``Keystone Marketing LLC`` at 92.7, two different merchants in this
book.) :func:`compact_score` is a stricter last-resort tier used only to look up name-only records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

FUZZY_THRESHOLD = 92.0  # Q1

# raw suffix token -> standardized suffix
SUFFIXES: dict[str, str] = {
    "llc": "llc",
    "inc": "inc",
    "incorporated": "inc",
    "corp": "corp",
    "corporation": "corp",
    "co": "co",
    "company": "co",
    "ltd": "ltd",
    "limited": "ltd",
    "lp": "lp",
    "llp": "llp",
    "pllc": "pllc",
}

_LLC_DOTTED = re.compile(r"\bl\s*\.\s*l\s*\.\s*c\b\.?")
_INC_DOTTED = re.compile(r"\binc\.")
_PUNCT = re.compile(r"[^\w\s]|_")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class NormName:
    raw: str
    core: str  # normalized name without the legal suffix
    suffix: str | None  # standardized suffix, None if absent

    @property
    def full(self) -> str:
        return f"{self.core} {self.suffix}" if self.suffix else self.core

    @property
    def first(self) -> str:
        return self.core.split(" ", 1)[0] if self.core else ""

    @property
    def tokens(self) -> tuple[str, ...]:
        return tuple(self.core.split())


def clean(raw: str) -> str:
    """Casefold, '&' -> 'and', L.L.C. -> llc, punctuation stripped, whitespace collapsed (Q1)."""
    s = (raw or "").casefold()
    s = s.replace("&", " and ")
    s = _LLC_DOTTED.sub(" llc ", s)
    s = _INC_DOTTED.sub(" inc ", s)
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def normalize_name(raw: str) -> NormName:
    """Normalize one raw merchant name (Q1). Never raises; an empty input gives an empty core."""
    s = clean(raw)
    toks = s.split()
    suffix = None
    if len(toks) >= 2 and toks[-1] in SUFFIXES:
        suffix = SUFFIXES[toks[-1]]
        toks = toks[:-1]
    return NormName(raw=raw, core=" ".join(toks), suffix=suffix)


def suffix_conflict(a: NormName, b: NormName) -> bool:
    """Both names carry a legal suffix and the suffixes differ -> different legal entities."""
    return a.suffix is not None and b.suffix is not None and a.suffix != b.suffix


def name_score(a: NormName, b: NormName) -> float:
    """Q1 fuzzy similarity 0-100: rapidfuzz token-set ratio of the cores (0 on a suffix conflict)."""
    if not a.core or not b.core or suffix_conflict(a, b):
        return 0.0
    if a.core == b.core:
        return 100.0
    return float(fuzz.token_set_ratio(a.core, b.core))


def compact_score(a: NormName, b: NormName) -> float:
    """Similarity of the names with all spaces removed (0 on a suffix conflict).

    Used only as the last lookup tier for name-only records (never for merges): it catches typos that
    glue two words together (``AmberrConcrete Corp``, ``Mbeki PizzaaLLC``) which token-set scoring
    cannot see. Threshold :data:`COMPACT_THRESHOLD` (stricter than Q1's 92).
    """
    if not a.core or not b.core or suffix_conflict(a, b):
        return 0.0
    return float(fuzz.ratio(a.full.replace(" ", ""), b.full.replace(" ", "")))


COMPACT_THRESHOLD = 95.0
