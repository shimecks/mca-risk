"""Merchant entity resolution (METRICS.md §6 Q1).

Q1: "Normalize the name ... Block on the first token. Match on EIN when present; otherwise fuzzy
token-set similarity >= 92 within a block, plus DBA alias tables. Every merge records its method and
score."

Unit of clustering
------------------
The atomic record is a **deal** (tracker deal or tracker-missing deal reconstructed from ACH, Q5). Every
name observed on a deal belongs to that deal's merchant: the tracker name, every ACH ``Merchant Name``
posted to the deal, the collections-log name of rows that carry the deal number, the 2023 tracker DBA.
A deal's **anchor name** is its most frequent ACH name (the processor's account-holder name; ties: the
tracker name, then the longest, then alphabetical). Deals are merged into merchants by, in order:

1. ``exact``: identical normalized anchor (core + standardized suffix). Score 100.
2. ``ein``: identical EIN (2025+ trackers). Score 100. EIN is authoritative across name variants.
3. ``fuzzy``: within a first-token block, :func:`names.name_score` >= 92 between any two anchor names of
   the two clusters, unless both clusters carry different EINs (``ein_conflict`` veto) or the suffixes
   conflict (score 0).

Every union is one ``merchant_merges`` row (method, score, the two records). Name-only records
(scorecards, collections rows without a deal number) are resolved afterwards against the
:class:`AliasIndex` (every name seen on each merchant's deals, plus DBA aliases), returning *candidate*
merchants; the date rule of Q3/Q4 picks among candidates when a name (typically a shared DBA or a
suffix-dropped name) fits several merchants. First-token blocking misses a name whose first token has
a typo; those are retried across blocks (``fuzzy_cross_block``) at lower confidence.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from mcarisk.resolve.names import (
    COMPACT_THRESHOLD,
    FUZZY_THRESHOLD,
    NormName,
    compact_score,
    name_score,
    normalize_name,
)

M_EXACT = "exact"
M_EIN = "ein"
M_FUZZY = "fuzzy"
M_SEED = "seed"
# alias-lookup methods (name-only records)
A_EXACT = "alias_exact"
A_CORE = "alias_core"  # suffix dropped in the query or the alias: same core, compatible suffix
A_DBA = "dba_alias"
A_FUZZY = "fuzzy"
A_CROSS = "fuzzy_cross_block"
A_COMPACT = "fuzzy_compact"
CROSS_BLOCK_FIRST_TOKEN_MIN = 75.0  # first tokens this similar are searched when the block misses


@dataclass
class DealNames:
    """Names observed on one deal."""

    deal_id: int
    ach: Counter = field(default_factory=Counter)  # raw ACH name -> rows
    tracker: str | None = None
    dba: str | None = None  # 2023 tracker DBA column
    collections: Counter = field(default_factory=Counter)
    ein: str | None = None

    def anchor(self) -> str:
        """Most frequent ACH name; ties -> tracker name, then longest, then alphabetical."""
        if not self.ach:
            return self.tracker or ""
        best = max(self.ach.values())
        tied = sorted(n for n, c in self.ach.items() if c == best)
        if self.tracker in tied:
            return self.tracker
        return sorted(tied, key=lambda s: (-len(s), s))[0]

    def all_names(self) -> Iterable[tuple[str, str, int]]:
        """(raw name, source, observations)."""
        for n, c in self.ach.items():
            yield n, "ach", c
        if self.tracker:
            yield self.tracker, "tracker", 1
        if self.dba:
            yield self.dba, "dba", 1
        for n, c in self.collections.items():
            yield n, "collections", c


class _UF:
    def __init__(self, items: Iterable[int]) -> None:
        self.p = {i: i for i in items}

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        lo, hi = min(ra, rb), max(ra, rb)
        self.p[hi] = lo
        return True


def merchant_key_for(name: NormName) -> str:
    """Stable key: hash of the canonical normalized name (same input -> same key, run after run)."""
    return "MK" + hashlib.sha1(name.full.encode("utf-8")).hexdigest()[:10].upper()


@dataclass
class Resolution:
    deal_key: dict[int, str]  # deal_id -> merchant_key
    deal_method: dict[int, tuple[str, float]]  # deal_id -> (method that attached it, score)
    merges: list[dict]  # merchant_merges rows
    merchants: list[dict]  # one row per merchant_key
    aliases: list[dict]  # merchant_aliases rows
    exceptions: list[dict]
    index: AliasIndex


def resolve_deals(deals: Mapping[int, DealNames]) -> Resolution:
    """Cluster deals into merchants (Q1). Deterministic: deals are visited in deal-id order."""
    ids = sorted(deals)
    uf = _UF(ids)
    anchors = {d: normalize_name(deals[d].anchor()) for d in ids}
    merges: list[dict] = []
    exceptions: list[dict] = []
    attach: dict[int, tuple[str, float]] = {}

    def _merge(a: int, b: int, method: str, score: float, detail: str) -> None:
        ra, rb = uf.find(a), uf.find(b)
        if uf.union(a, b):
            merges.append(
                {
                    "record_a": f"deal:{a}",
                    "record_b": f"deal:{b}",
                    "root_a": ra,
                    "root_b": rb,
                    "method": method,
                    "score": round(float(score), 2),
                    "detail": detail,
                }
            )
            attach.setdefault(b, (method, score))

    # 1. exact normalized anchor
    by_full: dict[str, list[int]] = defaultdict(list)
    for d in ids:
        by_full[anchors[d].full].append(d)
    for full, ds in by_full.items():
        for other in ds[1:]:
            _merge(ds[0], other, M_EXACT, 100.0, f"anchor '{full}'")

    # 2. EIN
    by_ein: dict[str, list[int]] = defaultdict(list)
    for d in ids:
        if deals[d].ein:
            by_ein[deals[d].ein].append(d)
    for ein, ds in sorted(by_ein.items()):
        for other in ds[1:]:
            _merge(ds[0], other, M_EIN, 100.0, f"ein {ein}")

    # 3. fuzzy within first-token block (cluster level)
    def cluster_eins() -> dict[int, set[str]]:
        out: dict[int, set[str]] = defaultdict(set)
        for d in ids:
            if deals[d].ein:
                out[uf.find(d)].add(deals[d].ein)
        return out

    eins = cluster_eins()
    reps: dict[int, dict[str, NormName]] = defaultdict(dict)  # root -> {full: NormName}
    for d in ids:
        reps[uf.find(d)][anchors[d].full] = anchors[d]
    blocks: dict[str, list[int]] = defaultdict(list)
    for root, names in reps.items():
        for first in sorted({n.first for n in names.values()}):
            blocks[first].append(root)
    for first in sorted(blocks):
        roots = sorted(set(blocks[first]))
        for i, ra in enumerate(roots):
            for rb in roots[i + 1 :]:
                if uf.find(ra) == uf.find(rb):
                    continue
                best, pair = 0.0, None
                for na in reps[ra].values():
                    for nb in reps[rb].values():
                        s = name_score(na, nb)
                        if s > best:
                            best, pair = s, (na, nb)
                if best < FUZZY_THRESHOLD or pair is None:
                    continue
                ea, eb = eins.get(uf.find(ra), set()), eins.get(uf.find(rb), set())
                if ea and eb and not (ea & eb):
                    exceptions.append(
                        {
                            "rule_id": "Q1",
                            "entity": "merchant",
                            "entity_id": f"deal:{ra}|deal:{rb}",
                            "reason": "fuzzy_match_vetoed_by_ein",
                            "detail": f"'{pair[0].raw}' ~ '{pair[1].raw}' score {best:.1f} but EINs differ",
                        }
                    )
                    continue
                _merge(ra, rb, M_FUZZY, best, f"'{pair[0].raw}' ~ '{pair[1].raw}' (block '{first}')")
                eins = cluster_eins()

    # components -> keys
    comps: dict[int, list[int]] = defaultdict(list)
    for d in ids:
        comps[uf.find(d)].append(d)
    deal_key: dict[int, str] = {}
    merchants: list[dict] = []
    root_key: dict[int, str] = {}
    for root, ds in sorted(comps.items()):
        cnt = Counter(anchors[d].full for d in ds)
        canon_full = sorted(cnt, key=lambda f: (-cnt[f], f))[0]
        canon = next(anchors[d] for d in ds if anchors[d].full == canon_full)
        raw_cnt = Counter(deals[d].anchor() for d in ds if anchors[d].full == canon_full)
        canon_raw = sorted(raw_cnt, key=lambda r: (-raw_cnt[r], r))[0]
        key = merchant_key_for(canon)
        if key in root_key.values():  # pragma: no cover - two clusters with one canonical name
            key = f"{key}-{root}"
        root_key[root] = key
        cl_eins = sorted({deals[d].ein for d in ds if deals[d].ein})
        if len(cl_eins) > 1:
            exceptions.append(
                {
                    "rule_id": "Q1",
                    "entity": "merchant",
                    "entity_id": key,
                    "reason": "multiple_eins_in_cluster",
                    "detail": f"EINs {cl_eins} on deals {ds}",
                }
            )
        for d in ds:
            deal_key[d] = key
        merchants.append(
            {
                "merchant_key": key,
                "canonical_name": canon_raw,
                "name_norm": canon.full,
                "block": canon.first,
                "ein": cl_eins[0] if len(cl_eins) == 1 else None,
                "n_eins": len(cl_eins),
                "n_deals": len(ds),
                "deal_ids": ",".join(str(d) for d in ds),
            }
        )
    for m in merges:
        m["merchant_key"] = root_key[uf.find(m["root_a"])]
        del m["root_a"], m["root_b"]
    deal_method = {d: attach.get(d, (M_SEED, 100.0)) for d in ids}

    # alias table + index
    alias_rows: dict[tuple[str, str, str], dict] = {}
    index = AliasIndex()
    for d in ids:
        key = deal_key[d]
        for raw, src, n in deals[d].all_names():
            k = (key, raw, src)
            nn = normalize_name(raw)
            # a name on the deal that is not a variant of its anchor is a DBA (trade name) alias
            kind = "dba" if src == "dba" or name_score(nn, anchors[d]) < FUZZY_THRESHOLD else "legal_variant"
            if k not in alias_rows:
                alias_rows[k] = {
                    "merchant_key": key,
                    "name_raw": raw,
                    "name_norm": nn.full,
                    "source": src,
                    "alias_kind": kind,
                    "n_seen": 0,
                }
            alias_rows[k]["n_seen"] += n
            index.add(raw, key, "dba" if kind == "dba" else src, n)
    # tracker name that does not fit its own cluster's anchor -> review
    for d in ids:
        t = deals[d].tracker
        if not t:
            continue
        tn = normalize_name(t)
        a = anchors[d]
        s = name_score(tn, a)
        if s < FUZZY_THRESHOLD and tn.core not in index.cores_of(deal_key[d]):
            exceptions.append(
                {
                    "rule_id": "Q1",
                    "entity": "deal",
                    "entity_id": str(d),
                    "reason": "tracker_name_differs_from_ach_anchor",
                    "detail": f"tracker '{t}' vs ACH anchor '{a.raw}' (score {s:.1f}); kept the ACH anchor",
                }
            )
    return Resolution(
        deal_key=deal_key,
        deal_method=deal_method,
        merges=merges,
        merchants=merchants,
        aliases=sorted(alias_rows.values(), key=lambda r: (r["merchant_key"], r["source"], r["name_raw"])),
        exceptions=exceptions,
        index=index,
    )


@dataclass(frozen=True)
class Candidate:
    merchant_key: str
    score: float
    method: str


class AliasIndex:
    """Names known to belong to each merchant, for resolving name-only records (Q1 + DBA aliases)."""

    def __init__(self) -> None:
        self.by_full: dict[str, Counter] = defaultdict(Counter)
        self.by_core: dict[str, dict[str, set[str | None]]] = defaultdict(lambda: defaultdict(set))
        self.dba_cores: dict[str, set[str]] = defaultdict(set)
        self.by_first: dict[str, set[str]] = defaultdict(set)  # first token -> fulls
        self.norms: dict[str, NormName] = {}
        self._cores_by_key: dict[str, set[str]] = defaultdict(set)

    def add(self, raw: str, key: str, source: str, n: int = 1) -> None:
        nn = normalize_name(raw)
        if not nn.core:
            return
        self.by_full[nn.full][key] += n
        self.by_core[nn.core][key].add(nn.suffix)
        if source == "dba":
            self.dba_cores[nn.core].add(key)
        self.by_first[nn.first].add(nn.full)
        self.norms[nn.full] = nn
        self._cores_by_key[key].add(nn.core)

    def cores_of(self, key: str) -> set[str]:
        return self._cores_by_key.get(key, set())

    def lookup(self, raw: str) -> list[Candidate]:
        """Candidate merchants from the best tier that has any. Empty if nothing reaches a threshold."""
        for tier in self.lookup_tiers(raw):
            if tier:
                return tier
        return []

    def lookup_tiers(self, raw: str) -> Iterator[list[Candidate]]:
        """Candidates tier by tier, best tier first (lazy: later tiers are computed only if asked for).

        1. ``alias_exact``  the query (with its suffix) equals a known alias;
        2. ``alias_core`` / ``dba_alias``  same core, compatible suffix (suffix dropped, or a DBA);
        3. ``fuzzy``  token-set >= 92 within the first-token block (Q1);
        4. ``fuzzy_cross_block``  the same against blocks whose first token is similar (first-token typo);
        5. ``fuzzy_compact``  space-free ratio >= 95 against every alias (glued-words typo).
        """
        q = normalize_name(raw)
        if not q.core:
            return
        if q.suffix is not None and q.full in self.by_full:
            yield [Candidate(k, 100.0, A_EXACT) for k in sorted(self.by_full[q.full])]
        if q.core in self.by_core:
            out = []
            for k, sufs in sorted(self.by_core[q.core].items()):
                if q.suffix is None or None in sufs or q.suffix in sufs:
                    method = A_DBA if k in self.dba_cores.get(q.core, set()) else A_CORE
                    out.append(Candidate(k, 100.0, method))
            yield out
        block = self.by_first.get(q.first, set())
        yield self._fuzzy(q, block, A_FUZZY, name_score, FUZZY_THRESHOLD)
        near: set[str] = set()
        for first, fs in self.by_first.items():
            if first != q.first and fuzz.ratio(first, q.first) >= CROSS_BLOCK_FIRST_TOKEN_MIN:
                near |= fs
        yield self._fuzzy(q, near, A_CROSS, name_score, FUZZY_THRESHOLD)
        # glued words change the first token itself, so the compact tier scans every alias
        yield self._fuzzy(q, self.norms.keys(), A_COMPACT, compact_score, COMPACT_THRESHOLD)

    def _fuzzy(
        self, q: NormName, fulls: Iterable[str], method: str, scorer, threshold: float
    ) -> list[Candidate]:
        best: dict[str, float] = {}
        for f in fulls:
            s = scorer(q, self.norms[f])
            if s >= threshold:
                for k in self.by_full[f]:
                    best[k] = max(best.get(k, 0.0), s)
        return [
            Candidate(k, round(s, 2), method) for k, s in sorted(best.items(), key=lambda x: (-x[1], x[0]))
        ]
