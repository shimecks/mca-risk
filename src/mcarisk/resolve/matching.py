"""Scorecard <-> deal (METRICS.md §6 Q3) and collections <-> deal (Q4).

Q3: "Same resolved merchant, application date 0-10 days before funding. If several match, take the
closest."  Days are calendar days (``0 <= funding_date - app_date <= 10``). The scorecard name is
resolved to *candidate* merchants through the alias index (a shared DBA or a suffix-dropped name can fit
several); every candidate merchant's deals are eligible. Ties on the day gap are broken by the name
score, then the lower deal id, and queued for review. A deal claimed by two scorecards is queued too.

Q4: "Deal number if present; otherwise the resolved merchant's deal that is open on the log date."
A deal is open on ``x`` when ``funding_date <= x`` and it has not closed before ``x`` (``close``: payoff
date, or the date cumulative cash reached RTR). Several open deals -> the most recently funded, queued
for review. None open -> the most recently funded deal on or before ``x`` at confidence 0.5, queued.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from mcarisk.resolve.merchants import AliasIndex, Candidate

Q3_MAX_DAYS = 10


@dataclass(frozen=True)
class DealRef:
    deal_id: int
    merchant_key: str
    funding_date: date
    close_date: date | None = None


def _deals_by_key(deals: list[DealRef]) -> dict[str, list[DealRef]]:
    out: dict[str, list[DealRef]] = {}
    for d in deals:
        out.setdefault(d.merchant_key, []).append(d)
    return out


def _cands_str(cands: list[Candidate]) -> str:
    return ", ".join(f"{c.merchant_key}({c.method} {c.score:g})" for c in cands[:5])


def match_scorecards(
    scorecards: list[tuple[int, str, date]],  # (scorecard_id, business name, app_date)
    index: AliasIndex,
    deals: list[DealRef],
) -> tuple[list[dict], list[dict]]:
    by_key = _deals_by_key(deals)
    rows, exc = [], []
    for sid, name, app in sorted(scorecards):
        cands: list[Candidate] = []
        options: list[tuple] = []
        for tier in index.lookup_tiers(name):  # best name tier that yields an eligible deal
            cands = cands or tier
            for c in tier:
                for d in by_key.get(c.merchant_key, []):
                    gap = (d.funding_date - app).days
                    if 0 <= gap <= Q3_MAX_DAYS:
                        options.append((gap, -c.score, d.deal_id, d, c))
            if options:
                cands = tier
                break
        rec = {
            "scorecard_id": sid,
            "business_name_raw": name,
            "app_date": app,
            "n_name_candidates": len(cands),
        }
        if not options:
            rec.update(deal_id=None, merchant_key=None, method="unmatched", confidence=0.0, day_gap=None)
            exc.append(
                _exc(
                    "Q3",
                    "scorecard",
                    sid,
                    "scorecard_unmatched",
                    f"'{name}' {app}; name candidates: {_cands_str(cands) or 'none'}",
                )
            )
            rows.append(rec)
            continue
        options.sort(key=lambda o: o[:3])
        gap, _, _, d, c = options[0]
        conf = round(c.score / 100 * (0.9 if c.method == "fuzzy_cross_block" else 1.0), 3)
        ties = [o for o in options if o[0] == gap and o[3].deal_id != d.deal_id]
        if ties:
            conf = min(conf, 0.6)
            exc.append(
                _exc(
                    "Q3",
                    "scorecard",
                    sid,
                    "scorecard_tie",
                    f"'{name}' {app}: deals "
                    f"{[d.deal_id] + [o[3].deal_id for o in ties]} all {gap} days later",
                )
            )
        rec.update(
            deal_id=d.deal_id,
            merchant_key=d.merchant_key,
            method=f"name_{c.method}+date",
            confidence=conf,
            day_gap=gap,
        )
        rows.append(rec)
    seen: dict[int, int] = {}
    for r in rows:
        if r["deal_id"] is None:
            continue
        if r["deal_id"] in seen:
            exc.append(
                _exc(
                    "Q3",
                    "scorecard",
                    r["scorecard_id"],
                    "deal_has_several_scorecards",
                    f"deal {r['deal_id']} also matched by scorecard {seen[r['deal_id']]}",
                )
            )
        seen.setdefault(r["deal_id"], r["scorecard_id"])
    return rows, exc


def is_open(d: DealRef, x: date) -> bool:
    return d.funding_date <= x and (d.close_date is None or x <= d.close_date)


def match_collections(
    rows_in: list[tuple[int, str, int | None, date]],  # (collection_id, name, deal_id_raw, log_date)
    index: AliasIndex,
    deals: list[DealRef],
) -> tuple[list[dict], list[dict]]:
    by_id = {d.deal_id: d for d in deals}
    by_key = _deals_by_key(deals)
    out, exc = [], []
    for cid, name, did, x in sorted(rows_in):
        rec = {"collection_id": cid, "merchant_name_raw": name, "log_date": x, "deal_id_raw": did}
        if did is not None and did in by_id:
            d = by_id[did]
            rec.update(deal_id=did, merchant_key=d.merchant_key, method="deal_number", confidence=1.0)
            cands = index.lookup(name)
            if cands and d.merchant_key not in {c.merchant_key for c in cands}:
                exc.append(
                    _exc(
                        "Q4",
                        "collection",
                        cid,
                        "name_disagrees_with_deal_number",
                        f"'{name}' resolves to {_cands_str(cands)}; deal {did} is {d.merchant_key}",
                    )
                )
            out.append(rec)
            continue
        if did is not None:
            exc.append(
                _exc("Q4", "collection", cid, "deal_number_unknown", f"deal {did} not in tracker or ACH")
            )
        cands: list[Candidate] = []
        pool: list[tuple[DealRef, Candidate]] = []
        for tier in index.lookup_tiers(name):  # best name tier that yields a deal funded by x
            cands = cands or tier
            pool = [(d, c) for c in tier for d in by_key.get(c.merchant_key, []) if d.funding_date <= x]
            if pool:
                cands = tier
                break
        open_ = [(d, c) for d, c in pool if is_open(d, x)]
        if open_:
            open_.sort(key=lambda o: (-o[1].score, o[0].funding_date, o[0].deal_id), reverse=False)
            best_score = open_[0][1].score
            top = [o for o in open_ if o[1].score == best_score]
            top.sort(key=lambda o: (o[0].funding_date, o[0].deal_id), reverse=True)
            d, c = top[0]
            conf = round(c.score / 100, 3)
            if len({o[0].deal_id for o in top}) > 1:
                conf = min(conf, 0.6)
                exc.append(
                    _exc(
                        "Q4",
                        "collection",
                        cid,
                        "several_open_deals",
                        f"'{name}' {x}: open deals {sorted({o[0].deal_id for o in top})}; took most recent",
                    )
                )
            rec.update(
                deal_id=d.deal_id,
                merchant_key=d.merchant_key,
                method=f"name_{c.method}+open_deal",
                confidence=conf,
            )
        elif pool:
            pool.sort(key=lambda o: (o[0].funding_date, o[1].score, o[0].deal_id), reverse=True)
            d, c = pool[0]
            rec.update(
                deal_id=d.deal_id,
                merchant_key=d.merchant_key,
                method=f"name_{c.method}+latest_deal",
                confidence=0.5,
            )
            exc.append(
                _exc(
                    "Q4",
                    "collection",
                    cid,
                    "no_open_deal_on_log_date",
                    f"'{name}' {x}: no open deal; took latest funded {d.deal_id}",
                )
            )
        else:
            rec.update(deal_id=None, merchant_key=None, method="unmatched", confidence=0.0)
            exc.append(
                _exc(
                    "Q4",
                    "collection",
                    cid,
                    "collection_unmatched",
                    f"'{name}' {x}; name candidates: {_cands_str(cands) or 'none'}",
                )
            )
        out.append(rec)
    return out, exc


def _exc(rule: str, entity: str, eid: int, reason: str, detail: str) -> dict:
    return {"rule_id": rule, "entity": entity, "entity_id": str(eid), "reason": reason, "detail": detail}


def close_dates(
    payoff_dates: Mapping[int, date],
    rtr: Mapping[int, int | None],
    cash_rows: list[tuple[int, date, int]],  # (deal_id, date, signed cents of debit/return/settlement)
) -> dict[int, date]:
    """Deal close date: its payoff date, or the first date cumulative cash reached RTR (whichever first)."""
    out = dict(payoff_dates)
    cum: dict[int, int] = {}
    for deal, d, amt in sorted(cash_rows, key=lambda r: (r[0], r[1])):
        cum[deal] = cum.get(deal, 0) + amt
        r = rtr.get(deal)
        if r and cum[deal] >= r - 1 and (deal not in out or d < out[deal]):
            out[deal] = d
    return out
