"""Merchant drill-down: one relationship's chain of deals, its cash events with lineage, D- and R-metrics."""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common as c
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

c.page_setup("Merchant drill-down")
c.require_data()

LINEAGE = ["src_file", "src_sheet", "src_row"]

# Display labels for METRICS.md §1 ledger kinds and collections-log status events.
KIND_LABEL = {
    "funding_out": "funding (cash out)",
    "commission_out": "commission (cash out)",
    "debit": "debit",
    "return": "return",
    "internal_payoff": "payoff: internal (non-cash, L1)",
    "early_payoff_cash": "payoff: cash",
    "settlement": "settlement payment",
    "modification": "modification",
    "write_off": "write-off",
    "status:default": "collections log: default",
    "status:bankruptcy": "collections log: bankruptcy",
    "status:settlement": "collections log: settlement agreed",
    "derived:d16": "D16 default date (derived)",
    "derived:outcome": "outcome (§2.1)",
}
KIND_STYLE = {  # colour, marker symbol
    "funding_out": ("#2a78d6", "triangle-down"),
    "commission_out": ("#0d366b", "triangle-down"),
    "debit": ("#1baf7a", "circle"),
    "return": ("#eb6834", "x"),
    "internal_payoff": ("#9b59b6", "diamond"),
    "early_payoff_cash": ("#1baf7a", "diamond"),
    "settlement": ("#1baf7a", "square"),
    "modification": ("#d4a017", "star"),
    "write_off": ("#c0392b", "square-x"),
    "status:default": ("#c0392b", "hexagon-open"),
    "status:bankruptcy": ("#c0392b", "hexagon"),
    "status:settlement": ("#d4a017", "hexagon-open"),
    "derived:d16": ("#c0392b", "line-ns-open"),
    "derived:outcome": ("#0b0b0b", "star-diamond-open"),
}

st.title("Merchant drill-down")

rels = c.query(
    "SELECT r.merchant_key, m.canonical_name, r.r10_n_deals, r.r5_status FROM relationship_metrics r "
    "JOIN merchants m USING (merchant_key) ORDER BY r.r10_n_deals DESC, r.merchant_key"
)
keys = rels.merchant_key.tolist()
names = dict(zip(rels.merchant_key, rels.canonical_name, strict=True))
ndeals = dict(zip(rels.merchant_key, rels.r10_n_deals, strict=True))

default = c.M0219_KEY if c.M0219_KEY in keys else keys[0]
qp = st.query_params.get("merchant")
deal_qp = st.query_params.get("deal")
if deal_qp and str(deal_qp).isdigit():
    hit = c.query("SELECT merchant_key FROM deal_metrics WHERE deal_id = ?", (int(deal_qp),))
    if not hit.empty:
        qp = hit.merchant_key.iloc[0]
if qp in keys:
    default = qp


def _label(k: str) -> str:
    tag = " · M0219, METRICS.md §7 worked example" if k == c.M0219_KEY else ""
    return f"{k} · {names[k]} ({ndeals[k]} deal{'s' if ndeals[k] != 1 else ''}){tag}"


mk = st.selectbox(
    "Relationship (resolved merchant)", keys, index=keys.index(default), format_func=_label, key="merchant"
)
st.caption(
    f"Merchant keys come from entity resolution ({c.link('Q1')}). Add `?deal=1150` to the URL to jump to a deal."
)

rel = c.query("SELECT * FROM relationship_metrics WHERE merchant_key = ?", (mk,)).iloc[0]
deals = c.query("SELECT * FROM deal_metrics WHERE merchant_key = ? ORDER BY funding_date, deal_id", (mk,))
dres = c.query("SELECT * FROM deals_resolved WHERE merchant_key = ? ORDER BY funding_date, deal_id", (mk,))
ledger = c.query("SELECT * FROM ledger WHERE merchant_key = ? ORDER BY event_date, event_id", (mk,))
ids = tuple(int(x) for x in deals.deal_id)
status = c.query(
    f"SELECT * FROM deal_status_events WHERE deal_id IN ({','.join('?' * len(ids))}) ORDER BY event_date", ids
)

# ---- data notes
missing = dres[dres.tracker_missing.fillna(False).astype(bool)]
if mk == c.M0219_KEY:
    md = c.metrics_md()
    m = re.search(r"^\*Data note \(v1\.3\):\*.*$", md, flags=re.M)
    note = m.group(0) if m else ""
    st.warning(
        "**METRICS.md §7 data note (v1.3).** Deal 1102 is missing from the tracker, so cash out and the "
        f"realized loss shown here are **lower bounds** ({c.links(['D6', 'R1', 'R8'])}) and the cash "
        f"multiple ({c.link('R3')}) is an **upper bound**.\n\n> "
        + c.mdx(note.replace("*Data note (v1.3):* ", ""))
    )
elif not missing.empty:
    st.warning(
        f"Deal(s) {', '.join(str(d) for d in missing.deal_id)} have no tracker row ({c.link('Q5')}): advance, fee "
        f"and commission are unknown, so {c.links(['D6', 'R1', 'R8'])} are lower bounds and {c.link('R3')} is "
        "an upper bound."
    )

# ---- relationship metrics
st.subheader(f"Relationship: {names[mk]}")
cols = st.columns(5)
cols[0].metric("R1 funder cash out", c.usd(rel.r1_funder_cash_out_cents))
cols[1].metric("R2 cash from merchant", c.usd(rel.r2_cash_from_merchant_cents))
cols[2].metric("R3 cash multiple", c.num(rel.r3_cash_multiple))
cols[3].metric("R4 net cash position", c.usd(rel.r4_net_cash_position_cents))
cols[4].metric("R5 status", str(rel.r5_status))
r_rows = [
    ("R1", "Funder cash out (sum D6)", c.usd(rel.r1_funder_cash_out_cents)),
    ("R2", "Cash from merchant (sum D7)", c.usd(rel.r2_cash_from_merchant_cents)),
    ("R3", "Cash multiple R2 / R1", c.num(rel.r3_cash_multiple, 3)),
    ("R4", "Net cash position R2 - R1", c.usd(rel.r4_net_cash_position_cents)),
    ("R5", "Status", str(rel.r5_status)),
    ("R6", "Economic exposure (open only)", c.usd(rel.r6_economic_exposure_cents)),
    ("R7", "Contractual balance (open only)", c.usd(rel.r7_contractual_balance_cents)),
    ("R8", "Realized loss (resolved only)", c.usd(rel.r8_realized_loss_cents)),
    ("R9", "Relationship bad (resolved and R3 < 1)", str(rel.r9_relationship_bad)),
    (
        "R10",
        "Deals / distressed refis / days to resolution",
        f"{rel.r10_n_deals} / {rel.r10_n_distressed_refi} / {rel.r10_days_to_resolution}",
    ),
]
with st.expander("All R-metrics", expanded=True):
    st.markdown(
        "| ID | Metric | Value |\n|---|---|---|\n"
        + "\n".join(f"| {c.link(i)} | {n} | {c.mdx(v)} |" for i, n, v in r_rows)
        + f"\n\nAs of {str(rel.as_of)[:10]}; "
        f"{int(rel.n_commission_unknown)} deal(s) with unknown commission."
    )

# ---- chain timeline
st.subheader("Chain timeline")
ev = ledger.assign(kind_key=ledger.kind, deal=ledger.deal_id)
st_ev = status.assign(
    kind_key="status:" + status.event_type,
    deal=status.deal_id,
    amount_cents=status.agreed_amount_cents,
    is_cash=False,
)
derived = []
for d in deals.itertuples():
    if pd.notna(d.d16_default_date):
        derived.append(
            {
                "event_date": d.d16_default_date,
                "kind_key": "derived:d16",
                "deal": d.deal_id,
                "detail": f"D16 trigger: {d.d16_trigger}",
            }
        )
    if pd.notna(d.outcome_date):
        derived.append(
            {
                "event_date": d.outcome_date,
                "kind_key": "derived:outcome",
                "deal": d.deal_id,
                "detail": f"outcome {d.outcome}",
            }
        )
parts = [f.dropna(axis=1, how="all") for f in (ev, st_ev, pd.DataFrame(derived)) if not f.empty]
tl = pd.concat(parts, ignore_index=True)
for col in ("amount_cents", "src_file", "src_sheet", "src_row"):
    if col not in tl:
        tl[col] = None
tl["event_date"] = pd.to_datetime(tl.event_date)
deal_order = [f"deal {d}" for d in deals.deal_id]
tl["deal_label"] = "deal " + tl.deal.astype("Int64").astype(str)

fig = go.Figure()
for k, (colr, sym) in KIND_STYLE.items():
    sub = tl[tl.kind_key == k]
    if sub.empty:
        continue
    amt = sub.amount_cents.map(lambda x: c.usd(x, 2) if pd.notna(x) else "")
    lin = sub.apply(
        lambda r: (
            f"{r.src_file} · {r.src_sheet} · row {int(r.src_row)}"
            if pd.notna(r.get("src_row"))
            else "derived"
        ),
        axis=1,
    )
    fig.add_scatter(
        x=sub.event_date,
        y=sub.deal_label,
        mode="markers",
        name=KIND_LABEL[k],
        marker={
            "color": colr,
            "symbol": sym,
            "size": 7 if k in ("debit", "return") else 13,
            "line": {"width": 1.5, "color": colr},
        },
        customdata=list(zip(amt, lin, strict=True)),
        hovertemplate="%{x|%Y-%m-%d} "
        + KIND_LABEL[k]
        + " %{customdata[0]}<br>%{customdata[1]}<extra></extra>",
    )
fig.update_layout(
    height=200 + 60 * len(deal_order),
    yaxis={"categoryorder": "array", "categoryarray": deal_order[::-1], "title": None},
    xaxis={"title": None},
    legend={"orientation": "h", "y": -0.25},
    margin={"t": 10, "l": 10, "r": 10},
)
st.plotly_chart(fig, width="stretch", key="timeline")
st.caption(
    f"Ledger events (METRICS.md §1; {c.links(['L1', 'L3'])}), collections-log status events and the derived "
    f"{c.link('D16')} default date and outcome. Hover a point for its source file and row."
)

# ---- deals and D-metrics
st.subheader("Deals and D-metrics")
D_ROWS = [
    ("D1", "Advance", "d1_advance_cents", c.usd),
    ("D2", "RTR", "d2_rtr_cents", c.usd),
    ("D3", "Fee", "d3_fee_cents", c.usd),
    ("D4", "Net cash to merchant", "d4_net_cash_to_merchant_cents", c.usd),
    ("D5", "Commission", "d5_commission_cents", c.usd),
    ("D6", "Funder cash out", "d6_funder_cash_out_cents", c.usd),
    ("D7", "Cash from merchant", "d7_cash_from_merchant_cents", c.usd),
    ("D8", "Internal payoff out", "d8_internal_payoff_out_cents", c.usd),
    ("D9", "Reported collected", "d9_reported_collected_cents", c.usd),
    ("D10", "Reported collection rate", "d10_reported_collection_rate", c.pct),
    ("D11", "Renewal-adjusted collection rate", "d11_adjusted_collection_rate", c.pct),
    ("D12", "Expected collected to date", "d12_expected_collected_cents", c.usd),
    ("D13", "Pace", "d13_pace", lambda x: c.num(x, 3)),
    ("D14", "Return rate (last 20)", "d14_return_rate_last20", c.pct),
    ("D15", "Business days since last good payment", "d15_bdays_since_good_payment", lambda x: c.num(x, 0)),
    ("D16", "Default date (recognition date)", "d16_default_date", None),
    ("D17", "Distressed refinance", "d17_distressed", None),
    ("D18", "Bad (naive)", "d18_bad_naive", None),
    ("D19", "Bad (economic)", "d19_bad_economic", None),
    ("§2.1", "Outcome (date)", "outcome", None),
]


def _cell(d: pd.Series, col: str, fmt) -> str:
    v = d[col]
    if col == "d16_default_date":
        return "no" if pd.isna(v) else f"{str(v)[:10]} ({str(d.d16_recognition_date)[:10]}, {d.d16_trigger})"
    if col == "outcome":
        return f"{v} ({str(d.outcome_date)[:10]})" if pd.notna(d.outcome_date) else str(v)
    if col == "d17_distressed" and pd.isna(v):
        return "n/a (not renewed)"
    if fmt is None:
        return "n/a" if pd.isna(v) else str(v)
    return fmt(v)


header = "| ID | Metric | " + " | ".join(f"deal {d}" for d in deals.deal_id) + " |"
lines = [header, "|---|---|" + "---|" * len(deals)]
extra = [
    ("", "Funding date", [str(x)[:10] for x in deals.funding_date]),
    ("", "Tracker row", ["missing (Q5)" if t else "yes" for t in deals.tracker_missing]),
    ("", "Successor deal", [("" if pd.isna(s) else str(int(s))) for s in deals.successor_deal_id]),
]
for i, n, vals in extra:
    lines.append(f"| {c.cite(i)} | {c.cite(n)} | " + " | ".join(vals) + " |")
for i, n, col, fmt in D_ROWS:
    vals = [_cell(d, col, fmt) for _, d in deals.iterrows()]
    lines.append(f"| {c.link(i) if i[0] == 'D' else i} | {n} | " + " | ".join(vals) + " |")
st.markdown(c.mdx("\n".join(lines)))
st.caption("Values are read from `deal_metrics` as of the metrics as-of date (amounts in USD).")

# ---- lineage
st.subheader("Lineage")
st.markdown(
    "Every event keeps the source row it came from (`src_file`, `src_sheet`, `src_row`, 1-based physical "
    "row; CSV header = row 1). Open a popover for an event's source."
)
key_kinds = {
    "funding_out",
    "commission_out",
    "internal_payoff",
    "early_payoff_cash",
    "settlement",
    "modification",
    "write_off",
}
for d, drow in zip(deals.deal_id, dres.itertuples(), strict=True):
    sub_l = ledger[ledger.deal_id == d]
    sub_s = status[status.deal_id == d]
    st.markdown(f"**Deal {d}** · {len(sub_l)} ledger events, {len(sub_s)} collections status events")
    key_ev = sub_l[sub_l.kind.isin(key_kinds)]
    items = [
        (f"{str(r.event_date)[:10]} · {KIND_LABEL[r.kind]} · {c.usd(r.amount_cents, 2)}", r)
        for r in key_ev.itertuples()
    ] + [
        (f"{str(r.event_date)[:10]} · {KIND_LABEL['status:' + r.event_type]}", r) for r in sub_s.itertuples()
    ]
    pcols = st.columns(4)
    with pcols[0].popover(f"Lineage: deal {d} tracker row", width="stretch"):
        src = "ACH funding row (no tracker row, Q5)" if drow.tracker_missing else "tracker row"
        st.markdown(f"**{src}**")
        st.dataframe(pd.DataFrame([{k: getattr(drow, k) for k in LINEAGE}]), hide_index=True, width="stretch")
        st.caption(
            f"source family `{drow.source_family}`; status in tracker: `{drow.status_raw}`; notes: {drow.notes_raw}"
        )
    for j, (label, r) in enumerate(items):
        with pcols[(j + 1) % 4].popover(c.mdx(label), width="stretch"):
            st.markdown(f"**Lineage** for {c.mdx(label)}")
            st.dataframe(
                pd.DataFrame([{k: getattr(r, k) for k in LINEAGE}]), hide_index=True, width="stretch"
            )
    with st.popover(f"Lineage: all {len(sub_l) + len(sub_s)} events of deal {d}", width="stretch"):
        tbl = pd.concat(
            [
                sub_l.assign(event=sub_l.kind.map(KIND_LABEL))[
                    [
                        "event_date",
                        "event",
                        "amount_cents",
                        "is_cash",
                        "return_code",
                        "deal_id_posted",
                        *LINEAGE,
                    ]
                ],
                sub_s.assign(
                    event=("status:" + sub_s.event_type).map(KIND_LABEL),
                    amount_cents=sub_s.agreed_amount_cents,
                )[["event_date", "event", "amount_cents", *LINEAGE]],
            ],
            ignore_index=True,
        ).sort_values(["event_date", "event"])
        tbl["amount"] = tbl.pop("amount_cents").map(lambda x: c.usd(x, 2) if pd.notna(x) else "")
        st.dataframe(tbl, hide_index=True, width="stretch")
        rep = (
            tbl[tbl.deal_id_posted.notna() & (tbl.deal_id_posted != d)]
            if "deal_id_posted" in tbl
            else tbl.iloc[0:0]
        )
        if not rep.empty:
            st.caption(
                f"{len(rep)} debit(s) were posted to another deal and reassigned here ({c.link('L3')})."
            )

links = c.query(
    f"SELECT * FROM deal_links WHERE deal_id IN ({','.join('?' * len(ids))}) ORDER BY deal_id", ids
)
if not links.empty:
    with st.expander("Renewal links (Q2) with method and confidence"):
        st.dataframe(links, hide_index=True, width="stretch")
pay = c.query(
    f"SELECT * FROM payoff_classes WHERE deal_id IN ({','.join('?' * len(ids))}) ORDER BY txn_date", ids
)
if not pay.empty:
    with st.expander("Payoff classification (L1): internal vs cash, with method and confidence"):
        st.dataframe(pay, hide_index=True, width="stretch")
