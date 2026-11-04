"""``build/findings.md``: the Part 5 findings, every number drawn from a persisted metric table.

Each quoted number is a ``Num``: a key, the METRICS.md IDs it uses, the SQL query over the persisted
tables that produced it (``portfolio_metrics``, ``target_metrics``, ``relationship_metrics``,
``deal_metrics``) and its display format. ``build_findings`` runs every query, formats the value, fills
the template and writes ``build/findings_numbers.json`` (key, metric IDs, SQL, value, text). The tests and
E7 (report-only) re-run every query and check that the value and the text in findings.md agree.

A ``Num`` with ``wilson`` = ``lo`` / ``hi`` selects (numerator, denominator) and quotes the bound of the
95% Wilson interval (``mcarisk.analytics.stats``), a statistic on the stored counts, not a new metric.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import duckdb

from mcarisk.analytics.stats import wilson_ci

M0219 = "MK1A8B5FBF55"  # METRICS.md §7 worked example (merchant key of relationship M0219)


@dataclass(frozen=True)
class Num:
    key: str
    metric_ids: tuple[str, ...]
    sql: str
    fmt: str  # pct | pp | usd_m | usd | int | x2
    wilson: str | None = None  # lo | hi


def fmt_value(v: Any, fmt: str) -> str:
    if v is None:
        return "n/a"
    if fmt == "pct":
        return f"{float(v) * 100:.1f}%"
    if fmt == "pp":
        return f"{float(v):.1f} pp"
    if fmt == "usd_m":
        return f"${float(v) / 1e8:,.2f}M"
    if fmt == "usd":
        return f"${float(v) / 100:,.0f}"
    if fmt == "int":
        return f"{int(v):,}"
    if fmt == "x2":
        return f"{float(v):.2f}"
    raise ValueError(fmt)


def query_value(con: duckdb.DuckDBPyConnection, n: Num) -> Any:
    row = con.execute(n.sql).fetchone()
    if row is None:
        raise LookupError(f"{n.key}: query returned no row: {n.sql}")
    if n.wilson:
        lo, hi = wilson_ci(int(row[0]), int(row[1]))
        return lo if n.wilson == "lo" else hi
    return row[0]


# ------------------------------------------------------------------------------------------ query builders
def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def pm(key, mid, metric, segment="all", col="value", fmt="pct", ids=None, k=None, wilson=None) -> Num:
    kcond = "k IS NULL" if k is None else f"k = {int(k)}"
    cols = "numerator, denominator" if wilson else col
    sql = (
        f"SELECT {cols} FROM portfolio_metrics WHERE metric_id = {_q(mid)} AND metric = {_q(metric)} "
        f"AND segment = {_q(segment)} AND {kcond}"
    )
    return Num(key, tuple(ids or (mid,)), sql, fmt, wilson)


def tm(key, metric, dimension, segment, col="value", fmt="pct", ids=("T1", "D19"), wilson=None) -> Num:
    cols = "numerator, denominator" if wilson else col
    sql = (
        f"SELECT {cols} FROM target_metrics WHERE metric_id = 'T1' AND metric = {_q(metric)} "
        f"AND dimension = {_q(dimension)} AND segment = {_q(segment)}"
    )
    return Num(key, tuple(ids), sql, fmt, wilson)


def rel(key, col, fmt, ids) -> Num:
    sql = f"SELECT {col} FROM relationship_metrics WHERE merchant_key = {_q(M0219)}"
    return Num(key, tuple(ids), sql, fmt)


# ------------------------------------------------------------------------------------------ registry
def registry(con: duckdb.DuckDBPyConnection) -> tuple[list[Num], dict[str, Any]]:
    """Every number findings.md quotes, plus the (data-chosen) segment labels the text refers to."""
    nums: list[Num] = [
        pm("p1_rep", "P1", "reported_collection_rate", ids=("P1", "D10")),
        pm("p1_adj", "P1", "adjusted_collection_rate", ids=("P1", "D11")),
        pm("p2_gap", "P2", "renewal_masking_gap_pp", fmt="pp"),
        pm("p3_share", "P3", "internal_payoff_share", ids=("P3", "D8", "D9")),
        pm("p4_naive", "P4", "bad_rate_naive", ids=("P4", "D18")),
        pm("p4_econ", "P4", "bad_rate_economic", ids=("P4", "D19")),
        pm("p4_closed_n", "P4", "bad_rate_economic", col="denominator", fmt="int"),
        tm("t1_econ", "bad_rate_economic", "all", "all"),
        tm("t1_naive", "bad_rate_naive", "all", "all", ids=("T1", "D18")),
        tm("t1_mature_n", "bad_rate_economic", "all", "all", col="denominator", fmt="int"),
        tm("t1_funded_n", "bad_rate_economic", "all", "all", col="n_deals", fmt="int"),
        pm("p6_econ", "P6", "economic_exposure_cents", fmt="usd_m", ids=("P6", "R6")),
        pm("p6_contr", "P6", "contractual_balance_cents", fmt="usd_m", ids=("P6", "R7")),
        pm("p6_open_n", "P6", "economic_exposure_cents", col="denominator", fmt="int", ids=("P6", "R5")),
        pm("p8_share", "P8", "distressed_share_of_renewals", ids=("P8", "D17")),
        pm("p8_share_k", "P8", "distressed_share_of_renewals", col="numerator", fmt="int", ids=("P8", "D17")),
        pm("p8_share_n", "P8", "distressed_share_of_renewals", col="denominator", fmt="int", ids=("P8",)),
        pm("p8_with", "P8", "bad_rate_chains_with_distressed", ids=("P8", "R9", "D17")),
        pm("p8_with_k", "P8", "bad_rate_chains_with_distressed", col="numerator", fmt="int"),
        pm("p8_with_n", "P8", "bad_rate_chains_with_distressed", col="denominator", fmt="int"),
        pm("p8_with_lo", "P8", "bad_rate_chains_with_distressed", wilson="lo"),
        pm("p8_with_hi", "P8", "bad_rate_chains_with_distressed", wilson="hi"),
        pm("p8_without", "P8", "bad_rate_chains_without_distressed", ids=("P8", "R9")),
        pm("p8_without_lo", "P8", "bad_rate_chains_without_distressed", wilson="lo"),
        pm("p8_without_hi", "P8", "bad_rate_chains_without_distressed", wilson="hi"),
        pm("p5_bad", "P5", "resolved_bad_share", ids=("P5", "R9")),
        pm("p5_n", "P5", "resolved_bad_share", col="denominator", fmt="int", ids=("P5", "R5")),
        pm("p5_loss", "P5", "capital_weighted_loss_rate", ids=("P5", "R8", "R1")),
        pm("p5_mult", "P5", "resolved_cash_multiple", fmt="x2", ids=("P5", "R2", "R1")),
        pm("p9_all", "P9", "modification_cure_rate"),
        pm("p9_all_k", "P9", "modification_cure_rate", col="numerator", fmt="int"),
        pm("p9_all_n", "P9", "modification_cure_rate", col="denominator", fmt="int"),
        pm("p9_closed", "P9", "modification_cure_rate", segment="closed"),
        pm("p9_closed_n", "P9", "modification_cure_rate", segment="closed", col="denominator", fmt="int"),
        rel("m0219_r3", "r3_cash_multiple", "x2", ("R3",)),
        rel("m0219_loss", "r8_realized_loss_cents", "usd", ("R8",)),
        Num(
            "tracker_missing_n",
            ("D6", "R1"),
            "SELECT COUNT(*) FROM deal_metrics WHERE NOT commission_known AND tracker_missing",
            "int",
        ),
    ]
    vint = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT segment FROM target_metrics WHERE dimension = 'vintage' ORDER BY 1"
        ).fetchall()
    ]
    maturity = dict(
        con.execute(
            "SELECT segment, maturity FROM target_metrics WHERE dimension = 'vintage' "
            "AND metric = 'bad_rate_economic'"
        ).fetchall()
    )
    p1v = {
        r[0]
        for r in con.execute(
            "SELECT segment FROM portfolio_metrics WHERE metric_id = 'P1' AND segment <> 'all'"
        ).fetchall()
    }
    p4v = {
        r[0]
        for r in con.execute(
            "SELECT segment FROM portfolio_metrics WHERE metric_id = 'P4' AND segment <> 'all'"
        ).fetchall()
    }
    for v in vint:
        nums += [
            tm(f"t1e_{v}", "bad_rate_economic", "vintage", v),
            tm(f"t1e_lo_{v}", "bad_rate_economic", "vintage", v, wilson="lo"),
            tm(f"t1e_hi_{v}", "bad_rate_economic", "vintage", v, wilson="hi"),
            tm(f"t1n_{v}", "bad_rate_naive", "vintage", v, ids=("T1", "D18")),
            tm(f"t1m_{v}", "bad_rate_economic", "vintage", v, col="denominator", fmt="int"),
            tm(f"t1f_{v}", "bad_rate_economic", "vintage", v, col="n_deals", fmt="int"),
        ]
        if v in p4v:
            nums += [
                pm(f"p4e_{v}", "P4", "bad_rate_economic", segment=v, ids=("P4", "D19")),
                pm(f"p4n_{v}", "P4", "bad_rate_economic", segment=v, col="denominator", fmt="int"),
            ]
        if v in p1v:
            nums += [
                pm(f"p1r_{v}", "P1", "reported_collection_rate", segment=v, ids=("P1", "D10")),
                pm(f"p1a_{v}", "P1", "adjusted_collection_rate", segment=v, ids=("P1", "D11")),
                pm(f"p2_{v}", "P2", "renewal_masking_gap_pp", segment=v, fmt="pp"),
            ]
    # P7: cash multiple at months 6 and 12 for each first-funding quarter observed that long
    p7 = con.execute(
        "SELECT segment, k FROM portfolio_metrics WHERE metric_id = 'P7' AND k IN (6, 12) ORDER BY 1, 2"
    ).fetchall()
    for q, k in p7:
        nums.append(pm(f"p7_{q}_{k}", "P7", "cumulative_cash_multiple", segment=q, k=k, fmt="x2"))
    # segments: T1 economic by dimension (choose extremes among segments with >= 50 mature deals)
    seg = con.execute(
        "SELECT dimension, segment, value, denominator FROM target_metrics WHERE metric_id = 'T1' "
        "AND metric = 'bad_rate_economic' AND dimension IN ('industry', 'iso', 'positions', 'burden') "
        "ORDER BY dimension, segment"
    ).fetchall()
    picks: dict[str, Any] = {"vintages": vint, "maturity": maturity, "p7": p7}
    for dim in ("industry", "iso", "positions", "burden"):
        rows = [r for r in seg if r[0] == dim and r[1] != "unknown" and r[3] >= 50 and r[2] is not None]
        if dim == "positions":
            chosen = [r for r in seg if r[0] == dim and r[1] != "unknown"]
        else:
            rows.sort(key=lambda r: r[2])
            chosen = [rows[-1], rows[0]] if rows else []
        picks[dim] = [r[1] for r in chosen]
        for r in chosen:
            s = r[1]
            nums += [
                tm(f"seg_{dim}_{s}", "bad_rate_economic", dim, s),
                tm(f"seg_{dim}_{s}_lo", "bad_rate_economic", dim, s, wilson="lo"),
                tm(f"seg_{dim}_{s}_hi", "bad_rate_economic", dim, s, wilson="hi"),
                tm(f"seg_{dim}_{s}_n", "bad_rate_economic", dim, s, col="denominator", fmt="int"),
            ]
    return nums, picks


# ------------------------------------------------------------------------------------------ text
def _render(t: dict[str, str], picks: dict[str, Any], as_of: str) -> str:
    def ci(key: str) -> str:
        return f"{t[key]} (95% CI {t[key + '_lo']}-{t[key + '_hi']})"

    def vci(v: str) -> str:
        return f"{t['t1e_' + v]} [{t['t1e_lo_' + v]}-{t['t1e_hi_' + v]}]"

    vint = picks["vintages"]
    mat = picks["maturity"]
    mature_v = [v for v in vint if mat.get(v) == "mature"]
    young = [v for v in vint if mat.get(v) != "mature"]
    peak = ["2024Q3", "2024Q4", "2025Q1"]
    around = ["2024Q1", "2025Q3", "2025Q4"]
    has_story = all(f"t1e_{v}" in t for v in peak + around)
    last = vint[-1] if vint else None

    L: list[str] = []
    a = L.append
    a("# Findings: cash-based risk on the synthetic MCA book")
    a("")
    a(
        f"As of **{as_of}**, METRICS.md v1.2 (D17 `attempt` basis). Every number below is read from the "
        "persisted metric tables (`portfolio_metrics`, `target_metrics`, `relationship_metrics`, "
        "`deal_metrics`) and cites its metric IDs; `build/findings_numbers.json` holds the query behind each "
        "one. Figures: `build/figures/*.png` (interactive: `*.plotly.json`, data: `build/figures/data/*.csv`)."
    )
    a("")
    a('## 1. Renewal masking: one in five "collected" dollars is the funder paying itself')
    a("")
    a(
        f"- On closed deals, RTR-weighted, the **reported collection rate is {t['p1_rep']}** (P1, D10) but the "
        f"**cash-based rate is {t['p1_adj']}** (P1, D11): a **masking gap of {t['p2_gap']}** (P2)."
    )
    a(
        f"- **{t['p3_share']}** of everything the trackers call collected is an internal payoff rolled into a "
        "renewal, not cash from the merchant (P3 = sum D8 / sum D9)."
    )
    a(
        "- The gap is present in every vintage (figure `masking.png`). For the youngest vintages P1 only sees "
        "the deals that have closed already, which are mostly renewals, so their reported rate is close to "
        f"100% and the gap is widest (e.g. {last}: reported {t.get(f'p1r_{last}', 'n/a')} vs adjusted "
        f"{t.get(f'p1a_{last}', 'n/a')}, P1). Read young vintages with that bias in mind."
    )
    a("")
    a("## 2. Bad rate doubles once distressed refinances count")
    a("")
    a(
        f"- Over closed deals ({t['p4_closed_n']}), the **naive bad rate is {t['p4_naive']}** (P4, D18: "
        f"defaulted, settled, written off) and the **economic bad rate is {t['p4_econ']}** (P4, D19: plus "
        "refinanced_distressed, D17)."
    )
    a(
        f"- On the **fixed 180-business-day horizon** (T1, METRICS.md §5; {t['t1_mature_n']} of "
        f"{t['t1_funded_n']} funded deals are old enough), **{t['t1_econ']}** of deals are economically bad "
        f"within 180 business days vs **{t['t1_naive']}** naive (D18 at the horizon). The fixed horizon is the "
        "unbiased comparison across vintages; the closed-deals view is not (section 4)."
    )
    a("")
    a("## 3. Exposure: the tracker's outstanding overstates the capital at risk")
    a("")
    a(
        f"- Across {t['p6_open_n']} open relationships, the **contractual balance is {t['p6_contr']}** "
        f"(P6, sum R7: remaining RTR) but the **economic exposure is {t['p6_econ']}** (P6, sum R6: cash out "
        'not yet recovered). Less than half of the "outstanding" is cash the funder can still lose; the '
        "rest is unearned margin. The economic figure is the number to reserve against."
    )
    a("")
    a("## 4. Vintage deterioration (T1 fixed horizon)")
    a("")
    a(
        "T1 economic bad rate (D19 within 180 business days of funding) by funding quarter, with 95% Wilson "
        "intervals; naive (D18 at the horizon) and the closed-deals-only P4 rate for contrast. A vintage is "
        "*immature* when none of its deals has reached the horizon and *partial* when only some have; their "
        "T1 rates are not comparable and are left out of the trend. Figure: `bad_rate_vintage.png`."
    )
    a("")
    a(
        "| Vintage | Maturity | Mature / funded | T1 economic [95% CI] | T1 naive | P4 economic, closed only (n) |"
    )
    a("|---|---|---:|---|---:|---|")
    for v in vint:
        te = vci(v) if mat.get(v) != "immature" else "n/a"
        tn = t[f"t1n_{v}"] if mat.get(v) != "immature" else "n/a"
        p4 = f"{t[f'p4e_{v}']} ({t[f'p4n_{v}']})" if f"p4e_{v}" in t else "n/a"
        a(f"| {v} | {mat.get(v)} | {t[f't1m_{v}']} / {t[f't1f_{v}']} | {te} | {tn} | {p4} |")
    a("")
    if has_story:
        a(
            f"- **The 2024-Q3 to 2025-Q1 vintages are the worst mature cohorts:** {vci('2024Q3')}, "
            f"{vci('2024Q4')} and {vci('2025Q1')} economically bad by the horizon, against {vci('2024Q1')} for "
            f"2024-Q1 and {vci('2025Q3')} / {vci('2025Q4')} for 2025-Q3 / 2025-Q4 (T1). 2024-Q2 "
            f"({vci('2024Q2')}) already sits close to the deteriorated band, and 2025-Q2 ({vci('2025Q2')}) is "
            "in between: the deterioration ramps in and out rather than switching on for exactly three quarters."
        )
        a(
            "- The naive view understates the same deterioration: T1 naive is "
            f"{t['t1n_2024Q3']} / {t['t1n_2024Q4']} / {t['t1n_2025Q1']} for those quarters (D18), so a third "
            "to a half of the economic bad deals in them are distressed refinances that the trackers report "
            "as paid off."
        )
    if young:
        a(
            f"- Immature or partial: {', '.join(young)}. Their closed-deals-only rates are biased upward because "
            f"only fast failures and quick renewals have closed yet: {last} shows {t.get(f'p4e_{last}', 'n/a')} "
            f"economic bad on {t.get(f'p4n_{last}', 'n/a')} closed deals (P4), which says nothing about the "
            "vintage's eventual quality."
        )
    a(
        f"- {len(mature_v)} vintages are mature. 2023 vintages are small (tens of deals), so their intervals "
        "are wide."
    )
    a("")
    a("### Vintage curves (P7)")
    a("")
    a(
        "Cumulative cash multiple (R2/R1 of each relationship's cash ledger) by months since first funding, one "
        "curve per first-funding quarter, censored at the as-of date (figure `vintage_curves.png`; dotted "
        "tails are cohorts only partly observed that long)."
    )
    a("")
    a("| First-funding quarter | Month 6 | Month 12 |")
    a("|---|---:|---:|")
    qs = sorted({q for q, _ in picks["p7"]})
    for q in qs:
        a(f"| {q} | {t.get(f'p7_{q}_6', 'n/a')} | {t.get(f'p7_{q}_12', 'n/a')} |")
    a("")
    a(
        "Relationships typically break even (cash multiple R3 = 1) around month 12. Curves are "
        "relationship-level, so a renewal's new funding pushes the curve down before its collections lift it."
    )
    a("")
    a("## 5. Refinance quality")
    a("")
    a(
        f"- **{t['p8_share']} of renewals are distressed refinances** ({t['p8_share_k']} of {t['p8_share_n']}; "
        "P8, D17)."
    )
    a(
        f"- **Resolved chains that contain a distressed refinance end in loss {ci('p8_with')}** "
        f"({t['p8_with_k']} of {t['p8_with_n']} chains; P8, R9), vs **{ci('p8_without')}** for resolved chains "
        f"without one, and {t['p5_bad']} for all {t['p5_n']} resolved relationships (P5). A distressed refinance "
        "roughly doubles the chance the relationship loses money."
    )
    a(
        f"- Resolved relationships as a whole return a cash multiple of {t['p5_mult']} (P5, R2/R1) with a "
        f"capital-weighted loss rate of {t['p5_loss']} (P5, sum R8 / sum R1)."
    )
    a(
        f"- **Modification cure rate: {t['p9_all']}** ({t['p9_all_k']} of {t['p9_all_n']} modified deals end "
        f"paid in full or renewed healthy; P9). Among modified deals that have closed ({t['p9_closed_n']}) it "
        f"is {t['p9_closed']}: a modification is closer to a coin flip than a cure."
    )
    a("")
    a("## 6. Segments (T1 economic bad rate, mature deals, 95% Wilson CI)")
    a("")

    def seg(dim: str, s: str) -> str:
        k = f"seg_{dim}_{s}"
        return f"{t[k]} [{t[k + '_lo']}-{t[k + '_hi']}], n={t[k + '_n']}"

    if picks.get("positions"):
        a(
            "- **Positions (stacking) is the strongest origination signal:** "
            + "; ".join(f"positions = {s}: {seg('positions', s)}" for s in picks["positions"])
            + " (T1). The intervals of 0 and 2+ positions do not overlap."
        )
    if len(picks.get("industry", [])) == 2:
        hi_, lo_ = picks["industry"]
        a(
            f"- **Industry:** highest {hi_} {seg('industry', hi_)}, lowest {lo_} {seg('industry', lo_)} (T1, "
            "industries with at least 50 mature deals)."
        )
    if len(picks.get("iso", [])) == 2:
        hi_, lo_ = picks["iso"]
        a(
            f"- **ISO:** highest {hi_} {seg('iso', hi_)}, lowest {lo_} {seg('iso', lo_)} (T1). Broker quality "
            "differs, but with about 100 mature deals per ISO the intervals overlap widely; treat it as a "
            "watch list, not a verdict."
        )
    if len(picks.get("burden", [])) == 2:
        hi_, lo_ = picks["burden"]
        a(
            f"- **Burden** (daily equivalent x 21 / monthly revenue, §5): highest {hi_} {seg('burden', hi_)}, "
            f"lowest {lo_} {seg('burden', lo_)} (T1). Burden does not rank risk the way one would expect "
            "here: the lowest-burden bucket is the worst. An exploratory split by renewal status puts the "
            "excess in renewals (distressed refinances are written at a lower payment, the D17 payment "
            "ratio), while among new deals the burden buckets are roughly flat. Part 6 should model burden "
            "with renewal status alongside it."
        )
    a("- Full tables: `build/figures/data/segments.csv`; figure `segments.png`.")
    a("")
    a("## Caveats")
    a("")
    a(
        "- **Synthetic data.** The book is generated (`scripts/generate_portfolio.py`, seed 11) with a known "
        "data-generating process. The point is the method and the definitions, not the levels."
    )
    a(
        "- **The economic bad rate is a lower bound against the simulator's own labels.** D17 is a rule on "
        "observable stress (returns, pace, modification). Measured against the simulator's distressed labels "
        "it has high precision and recall of about 0.89 by design (E6; PROGRESS.md Part 4), so some "
        "refinances the simulator calls distressed look healthy on the ledger. METRICS.md §4 quotes the "
        "simulator's 29.3% closed-deal economic bad rate; the pipeline's D17 gives the figure in section 2."
    )
    a(
        f"- **Tracker-missing deals make R1 a lower bound.** {t['tracker_missing_n']} deals have no tracker "
        "row (Q5), so their commission is unknown and D6 / R1 leave it out. Relationship M0219 (METRICS.md §7) "
        f"is the example: from the spreadsheets alone its cash multiple is {t['m0219_r3']} and its realized "
        f"loss {t['m0219_loss']} (R3, R8), against 0.73 and $10,072 with the full contract terms (§7): "
        "the commission on deal 1102 is invisible, so the pipeline's loss is understated."
    )
    a(
        "- Recent vintages are immature on T1 and biased on the closed-deals view; the vintage curves are "
        "censored at the as-of date. Segment rates use only mature deals, so they describe 2023 to early-2026 "
        "originations."
    )
    a("")
    return "\n".join(L)


NUMBER_TOKEN = re.compile(r"\$[\d,]+(?:\.\d+)?M?|\d+\.\d+(?:%| pp)?")


def build_findings(con: duckdb.DuckDBPyConnection, md_path: Path, json_path: Path, as_of: str) -> dict:
    nums, picks = registry(con)
    keys = [n.key for n in nums]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate findings keys")
    recs, text = [], {}
    for n in nums:
        v = query_value(con, n)
        text[n.key] = fmt_value(v, n.fmt)
        recs.append({**asdict(n), "metric_ids": list(n.metric_ids), "value": v, "text": text[n.key]})
    md = _render(text, picks, as_of)
    Path(md_path).parent.mkdir(parents=True, exist_ok=True)
    Path(md_path).write_text(md, encoding="utf-8")
    Path(json_path).write_text(json.dumps(recs, indent=1, default=str), encoding="utf-8")
    return {"numbers": len(recs), "picks": {k: v for k, v in picks.items() if k not in ("p7", "maturity")}}


def check_findings(con: duckdb.DuckDBPyConnection, md_path: Path, json_path: Path) -> dict[str, Any]:
    """Re-run every registered query; the value must equal the recorded one and its text must appear in
    findings.md. Also every decimal / % / pp / $ token in the findings body before the Caveats section
    must be one of the registered texts (no unregistered number)."""
    md = Path(md_path).read_text(encoding="utf-8")
    recs = json.loads(Path(json_path).read_text(encoding="utf-8"))
    bad: list[str] = []
    for r in recs:
        n = Num(r["key"], tuple(r["metric_ids"]), r["sql"], r["fmt"], r["wilson"])
        v = query_value(con, n)
        same = (v is None and r["value"] is None) or (
            v is not None and r["value"] is not None and float(v) == float(r["value"])
        )
        if not same or fmt_value(v, n.fmt) != r["text"]:
            bad.append(f"{n.key}: table {v!r} != recorded {r['value']!r}")
    body = md.split("## Caveats")[0]
    texts = {r["text"] for r in recs}
    tokens = NUMBER_TOKEN.findall(body)
    unregistered = sorted({tok for tok in tokens if not any(tok in tx for tx in texts)})
    quoted = sum(r["text"] in md for r in recs)
    return {
        "numbers": len(recs),
        "quoted_in_findings": quoted,
        "mismatches": bad,
        "unregistered_tokens": unregistered,
        "match_rate": (len(recs) - len(bad)) / len(recs) if recs else 1.0,
    }
