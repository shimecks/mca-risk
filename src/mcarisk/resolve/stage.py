"""Stage 3 ``resolve``: entity resolution and linkage. ``python -m mcarisk.resolve`` / ``make resolve``.

Reads the Part 2 tables (``deals_std``, ``payments_std``, ``scorecards_std``, ``collections_std``) and
writes, in one transaction:

  deals_resolved      every deal (tracker rows + Q5 tracker-missing reconstructions) with merchant_key,
                      resolved prior/next deal, link method/confidence, ACH funding amount
  merchants           one row per resolved merchant (Q1)
  merchant_aliases    every observed name per merchant (source, alias_kind legal_variant/dba)
  merchant_merges     every Q1 union: method (exact / ein / fuzzy), score, records
  deal_links          one row per renewal deal N: prior deal P, method (explicit/regex/inferred), confidence
  payoff_classes      one row per ACH PAYOFF: internal_payoff vs early_payoff_cash (L1) + criteria
  payments_resolved   every payments_std row with deal_id (repaired, L3) and deal_id_posted, ledger_kind
  misposting_repairs  one row per L3 reassignment (the repair log)
  scorecard_links     Q3; collections_links Q4
  notes_extractions   regex + LLM terms per collections note (both kept); collections_terms = chosen
  exceptions          the human-review queue (rule, reason, detail, lineage)

and updates ``data_quality`` (Q1-Q5, L1, L3), regenerates ``build/dq_report.md``, records the stage in
the run manifest (plus LLM usage).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from mcarisk import paths
from mcarisk.manifest import RunManifest, StageRecorder, open_run
from mcarisk.notes import extract as notes_extract
from mcarisk.resolve import completeness, matching, misposting, payoffs, renewals
from mcarisk.resolve.merchants import DealNames, resolve_deals

LINEAGE = ["src_file", "src_sheet", "src_row"]
N_SAMPLES = 5

LEDGER_KIND = {
    "FUNDING": "funding_out",
    "DEBIT": "debit",
    "RETURN": "return",
    "SETTLEMENT": "settlement",
}

# ------------------------------------------------------------------------------------------ DDL
DDL: dict[str, list[tuple[str, str]]] = {
    "merchants": [
        ("merchant_key", "VARCHAR NOT NULL"),
        ("canonical_name", "VARCHAR NOT NULL"),
        ("name_norm", "VARCHAR NOT NULL"),
        ("block", "VARCHAR"),
        ("ein", "VARCHAR"),
        ("n_eins", "BIGINT"),
        ("n_deals", "BIGINT NOT NULL"),
        ("deal_ids", "VARCHAR NOT NULL"),
        ("first_funding_date", "DATE"),
        ("last_funding_date", "DATE"),
    ],
    "merchant_aliases": [
        ("merchant_key", "VARCHAR NOT NULL"),
        ("name_raw", "VARCHAR NOT NULL"),
        ("name_norm", "VARCHAR"),
        ("source", "VARCHAR NOT NULL"),
        ("alias_kind", "VARCHAR NOT NULL"),
        ("n_seen", "BIGINT NOT NULL"),
    ],
    "merchant_merges": [
        ("merge_id", "BIGINT NOT NULL"),
        ("merchant_key", "VARCHAR NOT NULL"),
        ("record_a", "VARCHAR NOT NULL"),
        ("record_b", "VARCHAR NOT NULL"),
        ("method", "VARCHAR NOT NULL"),
        ("score", "DOUBLE NOT NULL"),
        ("detail", "VARCHAR"),
    ],
    "deal_links": [
        ("deal_id", "BIGINT NOT NULL"),
        ("prior_deal_id", "BIGINT NOT NULL"),
        ("method", "VARCHAR NOT NULL"),
        ("confidence", "DOUBLE NOT NULL"),
        ("evidence", "VARCHAR"),
        ("src_file", "VARCHAR NOT NULL"),
        ("src_sheet", "VARCHAR NOT NULL"),
        ("src_row", "INTEGER NOT NULL"),
    ],
    "payoff_classes": [
        ("payment_id", "BIGINT NOT NULL"),
        ("deal_id", "BIGINT NOT NULL"),
        ("txn_date", "DATE NOT NULL"),
        ("amount_cents", "BIGINT NOT NULL"),
        ("description", "VARCHAR"),
        ("payoff_class", "VARCHAR NOT NULL"),
        ("successor_deal_id", "BIGINT"),
        ("bday_gap", "BIGINT"),
        ("n_candidates", "BIGINT NOT NULL"),
        ("l1_tracker_link", "BOOLEAN"),
        ("l1_payoff_amt", "BOOLEAN"),
        ("l1_funding_formula", "BOOLEAN"),
        ("method", "VARCHAR NOT NULL"),
        ("confidence", "DOUBLE NOT NULL"),
        ("description_hint", "VARCHAR"),
        ("hint_agrees", "BOOLEAN"),
        ("src_file", "VARCHAR NOT NULL"),
        ("src_sheet", "VARCHAR NOT NULL"),
        ("src_row", "INTEGER NOT NULL"),
    ],
    "payments_resolved": [
        ("payment_id", "BIGINT NOT NULL"),
        ("txn_date", "DATE NOT NULL"),
        ("deal_id", "BIGINT NOT NULL"),
        ("deal_id_posted", "BIGINT NOT NULL"),
        ("repaired", "BOOLEAN NOT NULL"),
        ("repair_reason", "VARCHAR"),
        ("merchant_key", "VARCHAR"),
        ("txn_type", "VARCHAR NOT NULL"),
        ("ledger_kind", "VARCHAR NOT NULL"),
        ("is_cash", "BOOLEAN NOT NULL"),
        ("amount_cents", "BIGINT NOT NULL"),
        ("return_code", "VARCHAR"),
        ("batch_id", "VARCHAR"),
        ("description", "VARCHAR"),
        ("merchant_name_raw", "VARCHAR"),
        ("src_file", "VARCHAR NOT NULL"),
        ("src_sheet", "VARCHAR NOT NULL"),
        ("src_row", "INTEGER NOT NULL"),
    ],
    "misposting_repairs": [
        ("payment_id", "BIGINT NOT NULL"),
        ("txn_date", "DATE NOT NULL"),
        ("deal_id_posted", "BIGINT NOT NULL"),
        ("deal_id_repaired", "BIGINT NOT NULL"),
        ("reason", "VARCHAR NOT NULL"),
        ("detail", "VARCHAR"),
        ("src_file", "VARCHAR NOT NULL"),
        ("src_sheet", "VARCHAR NOT NULL"),
        ("src_row", "INTEGER NOT NULL"),
    ],
    "scorecard_links": [
        ("scorecard_id", "BIGINT NOT NULL"),
        ("business_name_raw", "VARCHAR"),
        ("app_date", "DATE"),
        ("deal_id", "BIGINT"),
        ("merchant_key", "VARCHAR"),
        ("method", "VARCHAR NOT NULL"),
        ("confidence", "DOUBLE NOT NULL"),
        ("day_gap", "BIGINT"),
        ("n_name_candidates", "BIGINT NOT NULL"),
        ("src_file", "VARCHAR NOT NULL"),
        ("src_sheet", "VARCHAR NOT NULL"),
        ("src_row", "INTEGER NOT NULL"),
    ],
    "collections_links": [
        ("collection_id", "BIGINT NOT NULL"),
        ("log_date", "DATE"),
        ("merchant_name_raw", "VARCHAR"),
        ("deal_id_raw", "BIGINT"),
        ("deal_id", "BIGINT"),
        ("merchant_key", "VARCHAR"),
        ("method", "VARCHAR NOT NULL"),
        ("confidence", "DOUBLE NOT NULL"),
        ("event_type", "VARCHAR"),
        ("src_file", "VARCHAR NOT NULL"),
        ("src_sheet", "VARCHAR NOT NULL"),
        ("src_row", "INTEGER NOT NULL"),
    ],
    "notes_extractions": [
        ("collection_id", "BIGINT NOT NULL"),
        ("extractor", "VARCHAR NOT NULL"),
        ("used", "BOOLEAN NOT NULL"),
        ("new_payment", "DOUBLE"),
        ("payment_period", "VARCHAR"),
        ("previous_payment", "DOUBLE"),
        ("settlement_amount", "DOUBLE"),
        ("settlement_installments", "BIGINT"),
    ],
    "collections_terms": [
        ("collection_id", "BIGINT NOT NULL"),
        ("deal_id", "BIGINT"),
        ("log_date", "DATE"),
        ("event_type", "VARCHAR"),
        ("extractor", "VARCHAR NOT NULL"),
        ("new_payment_cents", "BIGINT"),
        ("payment_period", "VARCHAR"),
        ("settlement_amount_cents", "BIGINT"),
        ("settlement_installments", "BIGINT"),
        ("amount_cents", "BIGINT"),
        ("notes_raw", "VARCHAR"),
        ("src_file", "VARCHAR NOT NULL"),
        ("src_sheet", "VARCHAR NOT NULL"),
        ("src_row", "INTEGER NOT NULL"),
    ],
    "exceptions": [
        ("exception_id", "BIGINT NOT NULL"),
        ("rule_id", "VARCHAR NOT NULL"),
        ("entity", "VARCHAR NOT NULL"),
        ("entity_id", "VARCHAR NOT NULL"),
        ("reason", "VARCHAR NOT NULL"),
        ("detail", "VARCHAR"),
        ("src_file", "VARCHAR"),
        ("src_sheet", "VARCHAR"),
        ("src_row", "INTEGER"),
    ],
}

DEALS_EXTRA: list[tuple[str, str]] = [
    ("merchant_key", "VARCHAR NOT NULL"),
    ("merchant_method", "VARCHAR NOT NULL"),
    ("merchant_score", "DOUBLE NOT NULL"),
    ("anchor_name", "VARCHAR"),
    ("tracker_missing", "BOOLEAN NOT NULL"),
    ("funding_payment_id", "BIGINT"),
    ("funding_net_cents", "BIGINT"),
    ("payment_cents_ach", "BIGINT"),
    ("frequency_ach", "VARCHAR"),
    ("prior_deal_id_explicit", "BIGINT"),
    ("prior_deal_id", "BIGINT"),
    ("link_method", "VARCHAR"),
    ("link_confidence", "DOUBLE"),
    ("next_deal_id", "BIGINT"),
    ("closed_by_payoff", "VARCHAR"),
    ("payoff_date", "DATE"),
]

_PD_TYPES = {"BIGINT": "Int64", "INTEGER": "Int64", "DOUBLE": "float64", "BOOLEAN": "boolean"}


def _df(rows: list[dict[str, Any]], ddl: list[tuple[str, str]]) -> pd.DataFrame:
    cols = [c for c, _ in ddl]
    df = pd.DataFrame([{c: r.get(c) for c in cols} for r in rows], columns=cols)
    for c, t in ddl:
        base = t.split()[0]
        if base in _PD_TYPES:
            df[c] = df[c].astype(_PD_TYPES[base])
        else:
            df[c] = df[c].astype(object).where(df[c].notna(), None)
    return df


def write_table(
    con: duckdb.DuckDBPyConnection, name: str, rows: list[dict], ddl: list[tuple[str, str]]
) -> int:
    con.execute(f'CREATE OR REPLACE TABLE "{name}" (' + ", ".join(f'"{c}" {t}' for c, t in ddl) + ")")
    if not rows:
        return 0
    df = _df(rows, ddl)
    con.register("_rs_tmp", df)
    try:
        sel = ", ".join(f'CAST("{c}" AS {t.split()[0]})' for c, t in ddl)
        con.execute(f'INSERT INTO "{name}" SELECT {sel} FROM _rs_tmp')
    finally:
        con.unregister("_rs_tmp")
    return len(rows)


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _lin(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r[k] for k in LINEAGE}


def _jsonable(v: Any) -> Any:
    return v.isoformat() if isinstance(v, date) else v


@dataclass
class ResolveResult:
    counts: dict[str, int]
    data_quality: dict[str, dict[str, Any]]
    exceptions_by_reason: dict[str, int]
    notes: dict[str, Any]
    seconds: float


# ------------------------------------------------------------------------------------------ main logic
def resolve(
    db_path: Path = paths.DB_PATH,
    *,
    dq_report_path: Path | None = paths.DQ_REPORT_PATH,
    run: RunManifest | None = None,
    l1_policy: str | None = None,
    notes_mode: str | None = None,
    notes_cache: notes_extract.NotesCache | None = None,
    notes_cfg: notes_extract.LLMConfig | None = None,
) -> ResolveResult:
    t0 = time.perf_counter()
    policy = l1_policy or payoffs.policy_from_env()
    con = duckdb.connect(str(db_path))
    try:
        deals_std = _rows(con, "SELECT * FROM deals_std ORDER BY deal_id")
        pays = _rows(
            con,
            "SELECT payment_id, txn_date, deal_id_posted, merchant_name_raw, amount_cents, txn_type, return_code, "
            "batch_id, description, src_file, src_sheet, src_row FROM payments_std ORDER BY payment_id",
        )
        scs = _rows(
            con,
            "SELECT scorecard_id, business_name_raw, app_date, src_file, src_sheet, src_row "
            "FROM scorecards_std ORDER BY scorecard_id",
        )
        cols = _rows(
            con,
            "SELECT collection_id, log_date, merchant_name_raw, deal_id_raw, event_type, "
            "amount_cents, notes_raw, src_file, src_sheet, src_row FROM collections_std "
            "ORDER BY collection_id",
        )
        exc: list[dict] = []

        # ---- Q5: tracker-missing deals from ACH
        tracker_ids = {d["deal_id"] for d in deals_std}
        recon, e = completeness.reconstruct_missing(tracker_ids, pays)
        exc += e
        recon_by_id = {r["deal_id"]: r for r in recon}

        # ---- Q1: names per deal -> merchants
        names: dict[int, DealNames] = {}
        for d in deals_std:
            names[d["deal_id"]] = DealNames(
                d["deal_id"], tracker=d["merchant_name_raw"], dba=d["dba_raw"], ein=d["ein"]
            )
        for r in recon:
            names[r["deal_id"]] = DealNames(r["deal_id"])
        for p in pays:
            names[p["deal_id_posted"]].ach[p["merchant_name_raw"]] += 1
        for c in cols:
            if c["deal_id_raw"] is not None and c["deal_id_raw"] in names:
                names[c["deal_id_raw"]].collections[c["merchant_name_raw"]] += 1
        res = resolve_deals(names)
        exc += res.exceptions
        key_of = res.deal_key

        funding_row: dict[int, dict] = {}
        for p in pays:
            if p["txn_type"] == "FUNDING" and p["deal_id_posted"] not in funding_row:
                funding_row[p["deal_id_posted"]] = p
        fdate: dict[int, date] = {d["deal_id"]: d["funding_date"] for d in deals_std}
        for r in recon:
            fdate[r["deal_id"]] = r["funding_date"]

        # ---- Q2 (tracker part): explicit field + notes regex
        tdeals = [
            renewals.TrackerDeal(
                d["deal_id"],
                d["funding_date"],
                None if d["prior_deal_id"] is None else int(d["prior_deal_id"]),
                d["notes_raw"],
            )
            for d in deals_std
        ]
        tlinks, e = renewals.tracker_links(tdeals, fdate, key_of)
        exc += e

        # ---- L1
        infos = {}
        for d in deals_std:
            fr = funding_row.get(d["deal_id"])
            infos[d["deal_id"]] = payoffs.DealInfo(
                d["deal_id"],
                key_of[d["deal_id"]],
                d["funding_date"],
                d["advance_cents"],
                d["fee_cents"],
                None if fr is None else -fr["amount_cents"],
                d["payoff_amt_cents"],
                False,
            )
        for r in recon:
            infos[r["deal_id"]] = payoffs.DealInfo(
                r["deal_id"],
                key_of[r["deal_id"]],
                r["funding_date"],
                None,
                None,
                r["funding_net_cents"],
                None,
                True,
            )
        po_rows = [p for p in pays if p["txn_type"] == "PAYOFF"]
        po_in = [
            payoffs.Payoff(
                p["payment_id"], p["deal_id_posted"], p["txn_date"], p["amount_cents"], p["description"]
            )
            for p in po_rows
        ]
        classes, e = payoffs.classify_payoffs(
            po_in, infos, {n: r["prior_deal_id"] for n, r in tlinks.items()}, policy
        )
        exc += e
        po_by_id = {p["payment_id"]: p for p in po_rows}
        for c in classes:
            c.update(_lin(po_by_id[c["payment_id"]]))

        # ---- Q2 final links (+ L1 inference)
        links, e = renewals.final_links(tlinks, classes, tdeals)
        exc += e
        deal_lin = {d["deal_id"]: _lin(d) for d in deals_std}
        deal_lin.update({r["deal_id"]: _lin(r) for r in recon})
        for lk in links:
            lk.update(deal_lin[lk["deal_id"]])
        prior_of = {lk["deal_id"]: lk for lk in links}
        next_of: dict[int, int] = {}
        for lk in links:
            next_of.setdefault(lk["prior_deal_id"], lk["deal_id"])

        # ---- L3
        internal = {
            c["deal_id"]: (c["txn_date"], c["successor_deal_id"])
            for c in classes
            if c["payoff_class"] == payoffs.INTERNAL
        }
        cash_po = {c["deal_id"]: c["txn_date"] for c in classes if c["payoff_class"] == payoffs.CASH}
        payoff_date = {c["deal_id"]: c["txn_date"] for c in classes}
        debits = [
            misposting.Debit(p["payment_id"], p["deal_id_posted"], p["txn_date"])
            for p in pays
            if p["txn_type"] == "DEBIT"
        ]
        repaired, repair_log, e = misposting.repair_mispostings(debits, internal, cash_po, fdate, payoff_date)
        exc += e
        pay_by_id = {p["payment_id"]: p for p in pays}
        for r in repair_log:
            r.update(_lin(pay_by_id[r["payment_id"]]))
        cls_by_pid = {c["payment_id"]: c["payoff_class"] for c in classes}
        pres = []
        for p in pays:
            deal = repaired.get(p["payment_id"], p["deal_id_posted"])
            kind = cls_by_pid[p["payment_id"]] if p["txn_type"] == "PAYOFF" else LEDGER_KIND[p["txn_type"]]
            pres.append(
                {
                    **p,
                    "deal_id": deal,
                    "repaired": p["payment_id"] in repaired,
                    "repair_reason": misposting.REASON if p["payment_id"] in repaired else None,
                    "merchant_key": key_of.get(deal),
                    "ledger_kind": kind,
                    "is_cash": kind != payoffs.INTERNAL,
                }
            )

        # ---- close dates (for Q4 "open on the log date")
        rtr = {d["deal_id"]: d["rtr_cents"] for d in deals_std}
        cash_rows = [
            (p["deal_id"], p["txn_date"], p["amount_cents"])
            for p in pres
            if p["txn_type"] in ("DEBIT", "RETURN", "SETTLEMENT")
        ]
        close = matching.close_dates(payoff_date, rtr, cash_rows)
        refs = [matching.DealRef(d, key_of[d], fdate[d], close.get(d)) for d in sorted(fdate)]

        # ---- Q3 / Q4
        sc_links, e = matching.match_scorecards(
            [(s["scorecard_id"], s["business_name_raw"], s["app_date"]) for s in scs], res.index, refs
        )
        exc += e
        sc_by = {s["scorecard_id"]: s for s in scs}
        for r in sc_links:
            r.update(_lin(sc_by[r["scorecard_id"]]))
        col_links, e = matching.match_collections(
            [
                (
                    c["collection_id"],
                    c["merchant_name_raw"],
                    None if c["deal_id_raw"] is None else int(c["deal_id_raw"]),
                    c["log_date"],
                )
                for c in cols
            ],
            res.index,
            refs,
        )
        exc += e
        col_by = {c["collection_id"]: c for c in cols}
        for r in col_links:
            r.update(_lin(col_by[r["collection_id"]]))
            r["event_type"] = col_by[r["collection_id"]]["event_type"]

        # ---- notes (E9; optional LLM)
        nx = notes_extract.extract_all(
            [(c["collection_id"], c["notes_raw"]) for c in cols],
            mode=notes_mode,
            cache=notes_cache,
            cfg=notes_cfg,
        )
        link_by = {r["collection_id"]: r for r in col_links}
        terms = []
        for r in nx.rows:
            if not r["used"]:
                continue
            c = col_by[r["collection_id"]]
            terms.append(
                {
                    "collection_id": c["collection_id"],
                    "deal_id": link_by[c["collection_id"]]["deal_id"],
                    "log_date": c["log_date"],
                    "event_type": c["event_type"],
                    "extractor": r["extractor"],
                    "new_payment_cents": None if r["new_payment"] is None else round(r["new_payment"] * 100),
                    "payment_period": r["payment_period"],
                    "settlement_amount_cents": None
                    if r["settlement_amount"] is None
                    else round(r["settlement_amount"] * 100),
                    "settlement_installments": r["settlement_installments"],
                    "amount_cents": c["amount_cents"],
                    "notes_raw": c["notes_raw"],
                    **_lin(c),
                }
            )
            if c["event_type"] == "modification" and r["new_payment"] is None:
                exc.append(
                    {
                        "rule_id": "E9",
                        "entity": "collection",
                        "entity_id": str(c["collection_id"]),
                        "reason": "modification_without_new_payment",
                        "detail": f"notes '{c['notes_raw']}'",
                    }
                )

        # ---- deals_resolved
        deal_rows = []
        std_cols = [c for c in deals_std[0] if c not in ("prior_deal_id",)] if deals_std else []
        for d in deals_std:
            row = {k: d[k] for k in std_cols}
            row["prior_deal_id_explicit"] = d["prior_deal_id"]
            deal_rows.append(row)
        for r in recon:
            row = {k: None for k in std_cols}
            row.update(
                deal_id=r["deal_id"],
                source_family="ach_reconstructed",
                merchant_name_raw=r["merchant_name_raw"],
                funding_date=r["funding_date"],
                std_flags=completeness.FLAG,
                **_lin(r),
                payment_cents_ach=r["payment_cents_ach"],
                frequency_ach=r["frequency_ach"],
            )
            deal_rows.append(row)
        cls_by_deal = {c["deal_id"]: c for c in classes}
        for row in deal_rows:
            did = row["deal_id"]
            fr = funding_row.get(did)
            m, s = res.deal_method[did]
            lk = prior_of.get(did)
            pc = cls_by_deal.get(did)
            row.update(
                merchant_key=key_of[did],
                merchant_method=m,
                merchant_score=s,
                anchor_name=names[did].anchor(),
                tracker_missing=did in recon_by_id,
                funding_payment_id=None if fr is None else fr["payment_id"],
                funding_net_cents=None if fr is None else -fr["amount_cents"],
                prior_deal_id=None if lk is None else lk["prior_deal_id"],
                link_method=None if lk is None else lk["method"],
                link_confidence=None if lk is None else lk["confidence"],
                next_deal_id=next_of.get(did),
                closed_by_payoff=None if pc is None else pc["payoff_class"],
                payoff_date=None if pc is None else pc["txn_date"],
            )
            if did not in recon_by_id and row.get("payment_cents_ach") is None:
                row["payment_cents_ach"], row["frequency_ach"] = None, None
        deal_rows.sort(key=lambda r: r["deal_id"])
        std_ddl = [(c, t) for c, t in _describe(con, "deals_std") if c != "prior_deal_id"]
        deals_ddl = std_ddl + [x for x in DEALS_EXTRA if x[0] not in dict(std_ddl)]
        # tracker-missing rows have NULL contract fields: relax NOT NULL on deals_std columns
        deals_ddl = [
            (c, t.replace(" NOT NULL", "") if c not in ("deal_id", *LINEAGE) else t) for c, t in deals_ddl
        ]

        # ---- merchants: funding range
        f_by_key: dict[str, list[date]] = defaultdict(list)
        for did, k in key_of.items():
            f_by_key[k].append(fdate[did])
        for m in res.merchants:
            ds = f_by_key[m["merchant_key"]]
            m["first_funding_date"], m["last_funding_date"] = min(ds), max(ds)
        for i, mg in enumerate(res.merges, start=1):
            mg["merge_id"] = i

        # ---- exceptions: attach lineage
        lin_lookup = {
            "deal": deal_lin,
            "payment": {p["payment_id"]: _lin(p) for p in pays},
            "scorecard": {s["scorecard_id"]: _lin(s) for s in scs},
            "collection": {c["collection_id"]: _lin(c) for c in cols},
        }
        for i, x in enumerate(exc, start=1):
            x["exception_id"] = i
            try:
                lin = lin_lookup.get(x["entity"], {}).get(int(x["entity_id"]))
            except ValueError:
                lin = None
            x.update(lin or {k: None for k in LINEAGE})

        # ---- write
        con.execute("BEGIN TRANSACTION")
        counts = {
            "deals_resolved": write_table(con, "deals_resolved", deal_rows, deals_ddl),
            "merchants": write_table(con, "merchants", res.merchants, DDL["merchants"]),
            "merchant_aliases": write_table(con, "merchant_aliases", res.aliases, DDL["merchant_aliases"]),
            "merchant_merges": write_table(con, "merchant_merges", res.merges, DDL["merchant_merges"]),
            "deal_links": write_table(con, "deal_links", links, DDL["deal_links"]),
            "payoff_classes": write_table(con, "payoff_classes", classes, DDL["payoff_classes"]),
            "payments_resolved": write_table(con, "payments_resolved", pres, DDL["payments_resolved"]),
            "misposting_repairs": write_table(
                con, "misposting_repairs", repair_log, DDL["misposting_repairs"]
            ),
            "scorecard_links": write_table(con, "scorecard_links", sc_links, DDL["scorecard_links"]),
            "collections_links": write_table(con, "collections_links", col_links, DDL["collections_links"]),
            "notes_extractions": write_table(con, "notes_extractions", nx.rows, DDL["notes_extractions"]),
            "collections_terms": write_table(con, "collections_terms", terms, DDL["collections_terms"]),
            "exceptions": write_table(con, "exceptions", exc, DDL["exceptions"]),
        }
        dq = _data_quality(
            deals_std=deals_std,
            recon=recon,
            res=res,
            links=links,
            classes=classes,
            repair_log=repair_log,
            sc_links=sc_links,
            col_links=col_links,
            exc=exc,
            policy=policy,
            n_names=len(res.aliases),
        )
        for rid, r in dq.items():
            con.execute(
                "UPDATE data_quality SET status = ?, hits = ?, denominator = ?, sample_rows = ?, detail = ? "
                "WHERE rule_id = ?",
                [r["status"], r["hits"], r["denominator"], r["sample_rows"], r["detail"], rid],
            )
        con.execute("COMMIT")
        if dq_report_path is not None:
            Path(dq_report_path).parent.mkdir(parents=True, exist_ok=True)
            Path(dq_report_path).write_text(dq_report(con, counts, exc, nx), encoding="utf-8")
    finally:
        con.close()

    ex_reasons = Counter(f"{x['rule_id']}:{x['reason']}" for x in exc)
    out = ResolveResult(
        counts=counts,
        data_quality={
            k: {"status": v["status"], "hits": v["hits"], "denominator": v["denominator"]}
            for k, v in dq.items()
        },
        exceptions_by_reason=dict(sorted(ex_reasons.items())),
        notes={
            "chosen": nx.chosen,
            "detail": nx.detail,
            "model": nx.model,
            "fallback_notes": nx.fallback_notes,
            "llm_calls": nx.usage.calls,
            "cache_hits": nx.usage.cache_hits,
            "errors": nx.usage.errors,
        },
        seconds=round(time.perf_counter() - t0, 3),
    )
    if run is not None:
        _record(run, out, nx, repair_log, classes, links, recon, policy, len(pays))
    return out


def _describe(con: duckdb.DuckDBPyConnection, table: str) -> list[tuple[str, str]]:
    out = []
    for name, typ, null, *_ in con.execute(f'DESCRIBE "{table}"').fetchall():
        out.append((name, typ + (" NOT NULL" if null == "NO" else "")))
    return out


# ------------------------------------------------------------------------------------------ DQ
def _dq(status: str, hits: int | None, den: int | None, samples: list[dict], detail: str) -> dict[str, Any]:
    return {
        "status": status,
        "hits": hits,
        "denominator": den,
        "sample_rows": json.dumps([{k: _jsonable(v) for k, v in s.items()} for s in samples[:N_SAMPLES]]),
        "detail": detail,
    }


def _data_quality(
    *, deals_std, recon, res, links, classes, repair_log, sc_links, col_links, exc, policy, n_names
) -> dict[str, dict[str, Any]]:
    by_rule: dict[str, list[dict]] = defaultdict(list)
    for x in exc:
        by_rule[x["rule_id"]].append(x)

    def ex_samples(rule: str) -> list[dict]:
        return [
            {**{k: x.get(k) for k in LINEAGE}, "reason": x["reason"], "entity_id": x["entity_id"]}
            for x in by_rule[rule]
        ]

    n_deals = len(deals_std) + len(recon)
    mcount = Counter(m for m, _ in res.deal_method.values())
    merges = Counter(mg["method"] for mg in res.merges)
    lm = Counter(lk["method"] for lk in links)
    pc = Counter(c["payoff_class"] for c in classes)
    pm = Counter(c["method"] for c in classes)
    sc_ok = sum(r["deal_id"] is not None for r in sc_links)
    cm = Counter(
        r["method"].split("+")[-1] if r["method"] != "deal_number" else "deal_number" for r in col_links
    )
    hint_dis = sum(c["hint_agrees"] is False for c in classes)
    return {
        "Q1": _dq(
            "evaluated",
            len(res.merchants),
            n_deals,
            ex_samples("Q1"),
            f"{n_deals} deals -> {len(res.merchants)} merchants; merges {dict(merges)}; deal attach "
            f"{dict(mcount)}; {n_names} aliases; {len(by_rule['Q1'])} review exceptions",
        ),
        "Q2": _dq(
            "evaluated",
            len(links),
            n_deals,
            ex_samples("Q2"),
            f"renewal links by method {dict(sorted(lm.items()))}; {len(by_rule['Q2'])} review exceptions",
        ),
        "Q3": _dq(
            "evaluated",
            sc_ok,
            len(sc_links),
            ex_samples("Q3"),
            f"{sc_ok}/{len(sc_links)} scorecards linked to a deal (same merchant, app date 0-10 days "
            f"before funding, closest); {len(by_rule['Q3'])} review exceptions",
        ),
        "Q4": _dq(
            "evaluated",
            sum(r["deal_id"] is not None for r in col_links),
            len(col_links),
            ex_samples("Q4"),
            f"collections rows linked by {dict(sorted(cm.items()))}; {len(by_rule['Q4'])} review exceptions",
        ),
        "Q5": _dq(
            "evaluated",
            len(recon),
            n_deals,
            [{**_lin(r), "deal_id": r["deal_id"], "funding_date": r["funding_date"]} for r in recon],
            f"{len(recon)} deals with ACH activity and no tracker row reconstructed from ACH "
            f"(flag {completeness.FLAG}; contract fields NULL)",
        ),
        "L1": _dq(
            "evaluated",
            pc.get(payoffs.INTERNAL, 0),
            len(classes),
            ex_samples("L1"),
            f"PAYOFF rows: {dict(pc)}; by method {dict(sorted(pm.items()))}; description hint disagrees "
            f"on {hint_dis}; tracker-missing policy '{policy}'",
        ),
        "L3": _dq(
            "evaluated",
            len(repair_log),
            None,
            [
                {
                    **_lin(r),
                    "payment_id": r["payment_id"],
                    "from": r["deal_id_posted"],
                    "to": r["deal_id_repaired"],
                }
                for r in repair_log
            ],
            f"{len(repair_log)} DEBIT rows reassigned from the paid-off prior deal to its active renewal "
            f"(misposting_repairs); {len(by_rule['L3'])} debits after a payoff left as posted (review)",
        ),
    }


def dq_report(con: duckdb.DuckDBPyConnection, counts: dict[str, int], exc: list[dict], nx) -> str:
    from mcarisk.standardize import dq_report_md

    dq = _rows(con, "SELECT * FROM data_quality")
    order = {r: i for i, r in enumerate(["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "L1", "L2", "L3"])}
    dq.sort(key=lambda r: (order.get(r["rule_id"], 99), r["rule_id"]))
    acc = _rows(con, "SELECT * FROM std_accounting")
    reasons = Counter()
    for t, f, a, rsn, n in con.execute(
        "SELECT target_table, field, action, reason, COUNT(*) FROM std_log GROUP BY ALL"
    ).fetchall():
        reasons[(t, f, a, rsn)] = n
    md = dq_report_md(dq, acc, reasons).replace(
        "# Data-quality report (Part 2: standardize)",
        "# Data-quality report (Parts 2-3: standardize, resolve)",
    )
    ex = Counter((x["rule_id"], x["reason"]) for x in exc)
    lines = ["", "## Resolution tables (Part 3)", "", "| Table | Rows |", "|---|---:|"]
    lines += [f"| `{k}` | {v:,} |" for k, v in counts.items()]
    lines += ["", "## Review queue (`exceptions`)", "", "| Rule | Reason | Rows |", "|---|---|---:|"]
    lines += [f"| {r} | {rsn} | {n:,} |" for (r, rsn), n in sorted(ex.items())]
    lines += ["", f"Notes extraction: {nx.detail} (model {nx.model or 'n/a'}).", ""]
    return md + "\n".join(lines)


def _record(
    run: RunManifest, out: ResolveResult, nx, repair_log, classes, links, recon, policy, n_pay
) -> None:
    st = StageRecorder(run.data["stages"]["resolve"])
    for name, n in out.counts.items():
        if name == "payments_resolved":
            st.table(
                name,
                accepted=n,
                repaired=len(repair_log),
                reasons={"repaired": {misposting.REASON: len(repair_log)}},
            )
        elif name == "deals_resolved":
            st.table(
                name, accepted=n, repaired=len(recon), reasons={"repaired": {completeness.FLAG: len(recon)}}
            )
        else:
            st.table(name, accepted=n)
    st.extra("data_quality", out.data_quality)
    st.extra("exceptions_by_reason", out.exceptions_by_reason)
    st.extra("payoff_classes", dict(Counter(c["payoff_class"] for c in classes)))
    st.extra("deal_links_by_method", dict(Counter(lk["method"] for lk in links)))
    st.extra("l1_tracker_missing_policy", policy)
    st.extra("notes_extraction", out.notes)
    st.note(
        f"payments_resolved: {n_pay} rows = payments_std; {len(repair_log)} reassigned by L3 (deal_id_posted kept)"
    )
    st.note(f"deals_resolved: {len(recon)} rows reconstructed from ACH (flag {completeness.FLAG})")
    u = nx.usage
    if nx.model and (u.calls or u.cache_hits):
        run.add_llm_usage(
            model=nx.model,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cost_usd=u.cost_usd,
            calls=u.calls,
            cache_hits=u.cache_hits,
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Entity resolution and linkage (Part 3).")
    ap.add_argument("--db", type=Path, default=paths.DB_PATH)
    ap.add_argument("--manifest", type=Path, default=paths.MANIFEST_PATH)
    ap.add_argument("--dq-report", type=Path, default=paths.DQ_REPORT_PATH)
    ap.add_argument("--l1-policy", choices=[payoffs.POLICY_STRICT, payoffs.POLICY_INFER], default=None)
    args = ap.parse_args(argv)
    if not args.db.exists():
        print(f"[resolve] {args.db} not found; run `make ingest standardize` first", file=sys.stderr)
        return 1
    run = open_run(args.manifest)
    with run.stage("resolve"):
        res = resolve(args.db, dq_report_path=args.dq_report, run=run, l1_policy=args.l1_policy)
    print(f"[resolve] done in {res.seconds:.2f}s -> {args.db}")
    for k, v in res.counts.items():
        print(f"  {k:<20} {v:>7}")
    for k, v in res.data_quality.items():
        print(f"  {k}: {v['status']} hits={v['hits']} of {v['denominator']}")
    print(f"  notes: {res.notes['detail']}")
    print(f"  dq report -> {args.dq_report}")
    return 0
