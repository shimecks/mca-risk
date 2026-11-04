"""E9 Notes extraction (optional LLM; plan.md §4). Gate: report both; LLM accuracy >= regex accuracy.

Population: collections-log rows whose event is a modification or a settlement and whose true deal is
identifiable (:func:`evals.truth.collections_truth_deal`). Target per row:
  modification -> ``deals.csv.modified_payment`` (the new installment) vs extracted ``new_payment``;
  settlement   -> ``deals.csv.settlement_amount`` vs extracted ``settlement_amount``.
A value is correct when ``|extracted - truth| <= max($1, 0.5% of truth)``: the notes themselves round
(``new pmt 248/day`` for $248.37, ``settle for 35.7k``), so exact-cent matching would score the note's
own rounding, not the extractor. A missing extraction is wrong (some notes state no amount at all, e.g.
"Settled - see agreement in drive"; both extractors are penalized equally). ``stated_only`` accuracy
excludes notes that contain no digit.

``llm_cost_usd`` is the total cost recorded in ``cache/llm_notes.jsonl`` (what it cost to build the
cache; cached runs cost nothing). The suite is skipped when the build has no LLM extractions.
"""

from __future__ import annotations

import json
import re

import duckdb
import pandas as pd

from evals.common import EvalContext, SuiteOutput, SuiteSkipped
from evals.truth import collections_truth_deal

SUITE_ID = "E9"


def _ok(pred, truth) -> bool:
    if pred is None or pd.isna(pred) or truth is None or pd.isna(truth):
        return False
    return abs(float(pred) - float(truth)) <= max(1.0, 0.005 * abs(float(truth)))


def run(ctx: EvalContext) -> SuiteOutput:
    truth = pd.read_csv(ctx.ground_truth_dir / "deals.csv")
    con = duckdb.connect(str(ctx.db_path), read_only=True)
    try:
        cl = con.execute(
            "SELECT collection_id, log_date, merchant_name_raw, deal_id_raw, event_type, amount_cents, notes_raw "
            "FROM collections_std WHERE event_type IN ('modification', 'settlement')"
        ).df()
        ne = con.execute("SELECT * FROM notes_extractions").df()
    finally:
        con.close()
    if not (ne.extractor == "llm").any():
        raise SuiteSkipped(
            "no LLM extractions in this build (cache/llm_notes.jsonl empty or not covering notes)"
        )
    td = collections_truth_deal(cl, truth, ctx.ground_truth_dir)
    tv = truth.set_index("deal_id")
    res: dict[str, dict[str, list[int]]] = {}
    errors = []
    for ex in ("regex", "llm"):
        e = ne[ne.extractor == ex].set_index("collection_id")
        acc = {"all": [0, 0], "stated_only": [0, 0], "modification": [0, 0], "settlement": [0, 0]}
        for r in cl.itertuples():
            cid = int(r.collection_id)
            if cid not in td or cid not in e.index:
                continue
            t = tv.loc[td[cid]]
            if r.event_type == "modification":
                target, pred = t.modified_payment, e.loc[cid, "new_payment"]
            else:
                target, pred = t.settlement_amount, e.loc[cid, "settlement_amount"]
            if pd.isna(target):
                continue
            good = _ok(pred, target)
            for k in ("all", r.event_type) + (
                ("stated_only",) if re.search(r"\d", r.notes_raw or "") else ()
            ):
                acc[k][0] += good
                acc[k][1] += 1
            if not good and len(errors) < 60:
                errors.append(
                    {
                        "extractor": ex,
                        "collection_id": cid,
                        "note": r.notes_raw,
                        "pred": None if pd.isna(pred) else float(pred),
                        "truth": float(target),
                    }
                )
        res[ex] = acc

    def rate(ex: str, k: str = "all") -> float:
        c, n = res[ex][k]
        return round(c / n, 6) if n else 0.0

    cost = 0.0
    cache = ctx.root / "cache" / "llm_notes.jsonl"
    if cache.exists():
        for line in cache.read_text(encoding="utf-8").splitlines():
            if line.strip():
                cost += float(json.loads(line).get("usage", {}).get("cost_usd", 0.0) or 0.0)
    metrics = {
        "llm_minus_regex_accuracy": round(rate("llm") - rate("regex"), 6),
        "regex_accuracy": rate("regex"),
        "llm_accuracy": rate("llm"),
        "llm_cost_usd": round(cost, 6),
        "by_group": {
            ex: {
                k: {"correct": c, "n": n, "accuracy": round(c / n, 6) if n else None}
                for k, (c, n) in acc.items()
            }
            for ex, acc in res.items()
        },
    }
    notes = [f"{ex}: {res[ex]['all'][0]}/{res[ex]['all'][1]} correct" for ex in res]
    return SuiteOutput(metrics, details={"errors": errors}, notes=notes)
