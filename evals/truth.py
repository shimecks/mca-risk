"""Truth-side helpers shared by eval suites: which true deal does a scorecard / collections row belong to?

``ground_truth/`` does not store a row-level key for these two sources, so the eval reconstructs it from
facts the generator wrote verbatim (only ``evals/`` may read ``ground_truth/``):

* scorecard -> deal: the true deal (funded 2023-2024) whose underwriting features equal the scorecard's
  (monthly revenue to the dollar, positions, state, TIB years = round(tib_months / 12, 1), FICO when the
  scorecard has one) and whose funding date is 1-7 days after the application date. Kept only if unique.
* collections row -> deal: the ``Deal #`` when present. Otherwise the true deals whose event date equals
  the log date (modification: ``modified_date``; default / bankruptcy: ``collections_date``; write-off:
  ``default_date`` + 180 days; settlement: 1-60 days after ``collections_date`` with the same rounded
  settlement amount), narrowed by name similarity to the true merchant's legal name / DBA. Kept only if
  unique.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz


def _d(x) -> date | None:
    if x is None or (isinstance(x, float) and pd.isna(x)) or x is pd.NaT:
        return None
    if isinstance(x, date):
        return x if type(x) is date else x.date()
    return date.fromisoformat(str(x)[:10])


def scorecard_truth_deal(sc: pd.DataFrame, truth: pd.DataFrame) -> dict[int, int]:
    t = truth.copy()
    t["fd"] = t.funding_date.map(_d)
    t = t[t.fd.map(lambda x: x.year <= 2024)]
    t["rev_c"] = (t.monthly_rev.round(0) * 100).astype("int64")
    t["tib_y"] = (t.tib_months / 12).round(1)
    idx: dict[tuple, list] = {}
    for r in t.itertuples():
        idx.setdefault((r.rev_c, r.state), []).append(r)
    out: dict[int, int] = {}
    for s in sc.itertuples():
        if pd.isna(s.avg_monthly_rev_cents):
            continue
        app = _d(s.app_date)
        cands = []
        for r in idx.get((int(s.avg_monthly_rev_cents), s.state), []):
            if not (1 <= (r.fd - app).days <= 7):
                continue
            if not pd.isna(s.positions) and int(s.positions) != int(r.positions_at_funding):
                continue
            if not pd.isna(s.tib_years) and abs(float(s.tib_years) - float(r.tib_y)) > 1e-9:
                continue
            if not pd.isna(s.fico) and int(s.fico) != int(r.fico):
                continue
            cands.append(int(r.deal_id))
        if len(cands) == 1:
            out[int(s.scorecard_id)] = cands[0]
    return out


def collections_truth_deal(cl: pd.DataFrame, truth: pd.DataFrame, gt_dir: Path) -> dict[int, int]:
    m = pd.read_csv(gt_dir / "merchants.csv").set_index("merchant_id")
    t = truth.copy()
    for c in ("modified_date", "collections_date", "default_date"):
        t[c] = t[c].map(_d)
    known = set(t.deal_id.astype(int))
    out: dict[int, int] = {}
    for r in cl.itertuples():
        if not pd.isna(r.deal_id_raw):
            if int(r.deal_id_raw) in known:
                out[int(r.collection_id)] = int(r.deal_id_raw)
            continue
        x = _d(r.log_date)
        if r.event_type == "modification":
            c = t[t.modified_date == x]
        elif r.event_type in ("default", "bankruptcy"):
            c = t[t.collections_date == x]
        elif r.event_type == "write_off":
            c = t[t.default_date.map(lambda v, x=x: v is not None and v + timedelta(days=180) == x)]
        elif r.event_type == "settlement":
            amt = None if pd.isna(r.amount_cents) else int(r.amount_cents)
            c = t[
                t.collections_date.map(lambda v, x=x: v is not None and 1 <= (x - v).days <= 60)
                & t.settlement_amount.map(
                    lambda v, amt=amt: amt is None or (not pd.isna(v) and round(v) * 100 == amt)
                )
            ]
        else:
            continue
        if c.empty:
            continue
        name = str(r.merchant_name_raw).casefold()
        scores = []
        for d in c.itertuples():
            mm = m.loc[d.merchant_id]
            s = max(
                fuzz.ratio(name, str(mm.legal_name).casefold()),
                fuzz.ratio(name, str(mm.dba).casefold()),
                fuzz.ratio(name, str(mm.legal_name).casefold().rsplit(" ", 1)[0]),
            )
            scores.append((s, int(d.deal_id)))
        scores.sort(reverse=True)
        if scores[0][0] >= 85 and (len(scores) == 1 or scores[1][0] < scores[0][0]):
            out[int(r.collection_id)] = scores[0][1]
    return out
