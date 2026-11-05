"""Merchant story: one merchant relationship, dollar by dollar (default: METRICS.md §7 example M0219).

Cash out / cash back / net result are R1 / R2 / R4 from ``relationship_metrics``; the per-deal summary
is D6 / D7 / D10 / D11 and the outcome from ``deal_metrics``; events and source rows come from
``ledger``, ``deal_status_events`` and ``deals_resolved``. Nothing is recomputed here.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common as c
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

c.page_setup("Merchant story")
c.require_data()

OUTCOME = {
    "refinanced_distressed": "Rolled into a new advance while struggling",
    "renewed_healthy": "Renewed while paying well",
    "paid_in_full": "Paid in full",
    "paid_in_full_early": "Paid off early in cash",
    "settled": "Settled for less than owed",
    "written_off": "Written off",
    "defaulted": "Defaulted",
    "active": "Still paying",
    "active_modified": "Still paying, on modified terms",
}
EVENT = {
    "funding_out": "Advance paid out",
    "commission_out": "Broker commission paid",
    "debit": "Payment received",
    "return": "Payment bounced",
    "internal_payoff": "“Paid off” with the new advance (no cash)",
    "early_payoff_cash": "Paid off in cash",
    "settlement": "Settlement payment",
    "modification": "Terms modified",
    "write_off": "Written off",
    "status:default": "Collections: default",
    "status:bankruptcy": "Collections: bankruptcy",
    "status:settlement": "Collections: settlement agreed",
}
# colour, symbol, size: blue = cash, grey = non-cash / tracker-only, red = loss events
STYLE = {
    "debit": (c.CASH_LIGHT, "circle", 5),
    "return": (c.TRACKER, "x-thin-open", 7),
    "funding_out": (c.CASH, "triangle-up", 14),
    "early_payoff_cash": (c.CASH, "diamond", 14),
    "settlement": (c.CASH, "square", 11),
    "internal_payoff": (c.TRACKER, "diamond", 15),
    "modification": (c.TRACKER, "star", 13),
    "status:default": (c.LOSS, "circle-open", 14),
    "status:bankruptcy": (c.LOSS, "circle-open", 14),
    "status:settlement": (c.TRACKER, "circle-open", 13),
    "write_off": (c.LOSS, "x", 15),
}
SRC = {"src_file": "Source file", "src_sheet": "Sheet", "src_row": "Row"}

st.title("Merchant story")

rels = c.query(
    "SELECT r.merchant_key, m.canonical_name, r.r10_n_deals FROM relationship_metrics r "
    "JOIN merchants m USING (merchant_key) ORDER BY r.r10_n_deals DESC, m.canonical_name"
)
keys = rels.merchant_key.tolist()
if c.M0219_KEY in keys:
    keys.remove(c.M0219_KEY)
    keys.insert(0, c.M0219_KEY)
names = dict(zip(rels.merchant_key, rels.canonical_name, strict=True))
ndeals = dict(zip(rels.merchant_key, rels.r10_n_deals, strict=True))

default = keys[0]
qp = st.query_params.get("merchant")
deal_qp = st.query_params.get("deal")
if deal_qp and str(deal_qp).isdigit():
    hit = c.query("SELECT merchant_key FROM deal_metrics WHERE deal_id = ?", (int(deal_qp),))
    if not hit.empty:
        qp = hit.merchant_key.iloc[0]
if qp in keys:
    default = qp


def _label(k: str) -> str:
    n = ndeals[k]
    tag = " · the worked example" if k == c.M0219_KEY else ""
    return f"{names[k]} ({n} deal{'s' if n != 1 else ''}){tag}"


mk = st.selectbox("Merchant", keys, index=keys.index(default), format_func=_label, key="merchant")

rel = c.query("SELECT * FROM relationship_metrics WHERE merchant_key = ?", (mk,)).iloc[0]
deals = c.query("SELECT * FROM deal_metrics WHERE merchant_key = ? ORDER BY funding_date, deal_id", (mk,))
dres = c.query("SELECT * FROM deals_resolved WHERE merchant_key = ? ORDER BY funding_date, deal_id", (mk,))
ledger = c.query("SELECT * FROM ledger WHERE merchant_key = ? ORDER BY event_date, event_id", (mk,))
ids = tuple(int(x) for x in deals.deal_id)
marks = ",".join("?" * len(ids))
status = c.query(f"SELECT * FROM deal_status_events WHERE deal_id IN ({marks}) ORDER BY event_date", ids)


# ---------------------------------------------------------------------------- verdict
def verdict() -> str:
    resolved = rel.r5_status == "resolved"
    if resolved and pd.notna(rel.r8_realized_loss_cents) and rel.r8_realized_loss_cents > 0:
        head = f"Lost {c.usd(rel.r8_realized_loss_cents)}"
    elif resolved:
        head = f"Made {c.usd(rel.r4_net_cash_position_cents)}"
    else:
        head = f"Still open: {c.usd(rel.r6_economic_exposure_cents)} of cash not yet recovered"
    why = []
    for d in deals.itertuples():
        if d.outcome == "refinanced_distressed" and pd.notna(d.successor_deal_id):
            why.append(
                f"deal {d.deal_id} looked “paid off” but was rolled into a distressed refinance "
                f"(deal {int(d.successor_deal_id)})"
            )
    last = deals.iloc[-1]
    end = OUTCOME.get(last.outcome, str(last.outcome)).lower()
    if why:
        tail = f", which was {end}" if last.outcome in ("written_off", "settled", "defaulted") else ""
        return f"{head}: {'; '.join(why)}{tail}."
    return f"{head}. Last deal: {end}."


resolved = rel.r5_status == "resolved"
loss = resolved and pd.notna(rel.r8_realized_loss_cents) and rel.r8_realized_loss_cents > 0
(st.error if loss else st.success if resolved else st.info)(c.mdx(f"**{verdict()}**"))

cols = st.columns(3)
with cols[0].container(border=True):
    st.metric("Cash out", c.usd(rel.r1_funder_cash_out_cents), help=c.spec("R1", "D6"))
    st.caption("Advances and broker commissions the funder paid")
with cols[1].container(border=True):
    st.metric("Cash back", c.usd(rel.r2_cash_from_merchant_cents), help=c.spec("R2", "D7"))
    st.caption("Payments that actually came from the merchant")
with cols[2].container(border=True):
    st.metric(
        "Net result",
        c.usd(rel.r4_net_cash_position_cents),
        help=c.spec("R4", "R3", note=f"Cash multiple (cash back / cash out): {c.num(rel.r3_cash_multiple)}."),
    )
    st.caption(
        f"{c.num(rel.r3_cash_multiple)} back per dollar out · " + ("finished" if resolved else "still open")
    )

missing = dres[dres.tracker_missing.fillna(False).astype(bool)]
if mk == c.M0219_KEY:
    note = re.search(r"^\*Data note \(v1\.3\):\*.*$", c.metrics_md(), flags=re.M)
    note = note.group(0) if note else ""
    true_loss = re.search(r"lower bound\*\* on the true (\$[\d,]+)", note)
    true_mult = re.search(r"overstates the true (\d\.\d+)", note)
    st.info(
        c.mdx(
            "**The loss shown is a floor.** Deal 1102 is missing from the deal tracker, so its broker "
            "commission isn't in the spreadsheets: cash out and the loss are understated"
            + (
                f" (the full contract terms put the loss at {true_loss.group(1)} and the cash multiple at "
                f"{true_mult.group(1)})."
                if true_loss and true_mult
                else "."
            )
        ),
        icon=":material/info:",
    )
    with st.expander("METRICS.md data note (exact wording)"):
        st.markdown(c.cite(note.replace("*Data note (v1.3):* ", "")))
elif not missing.empty:
    st.info(
        f"Deal {', '.join(str(d) for d in missing.deal_id)} is missing from the deal tracker, so its "
        "advance terms and commission are unknown: cash out and any loss shown are a floor.",
        icon=":material/info:",
    )

# ---------------------------------------------------------------------------- timeline
st.subheader("What happened, deal by deal")
tl = pd.concat(
    [
        ledger.assign(key=ledger.kind)[["event_date", "key", "deal_id", "amount_cents", *SRC]],
        status.assign(key="status:" + status.event_type, amount_cents=status.agreed_amount_cents)[
            ["event_date", "key", "deal_id", "amount_cents", *SRC]
        ],
    ],
    ignore_index=True,
)
tl["event_date"] = pd.to_datetime(tl.event_date)
tl["row"] = "Deal " + tl.deal_id.astype(int).astype(str)
order = [f"Deal {d}" for d in deals.deal_id]

fig = go.Figure()
for d in deals.itertuples():
    end = d.outcome_date if pd.notna(d.outcome_date) else rel.as_of
    fig.add_scatter(
        x=[pd.Timestamp(d.funding_date), pd.Timestamp(end)],
        y=[f"Deal {d.deal_id}"] * 2,
        mode="lines",
        line={"color": c.MUTED, "width": 6},
        showlegend=False,
        hoverinfo="skip",
    )
for k, (colr, sym, size) in STYLE.items():
    sub = tl[tl.key == k]
    if sub.empty:
        continue
    amt = sub.amount_cents.map(lambda x: c.usd(abs(x), 2) if pd.notna(x) else "")
    src = [
        f"{f} · {s} · row {int(r)}" if pd.notna(r) else ""
        for f, s, r in zip(sub.src_file, sub.src_sheet, sub.src_row, strict=True)
    ]
    fig.add_scatter(
        x=sub.event_date,
        y=sub.row,
        mode="markers",
        name=EVENT[k],
        marker={"color": colr, "symbol": sym, "size": size, "line": {"width": 2, "color": colr}},
        customdata=list(zip(amt, src, strict=True)),
        hovertemplate="%{x|%b %d, %Y}: " + EVENT[k] + " %{customdata[0]}<br>"
        "<span style='color:#888'>%{customdata[1]}</span><extra></extra>",
    )
NOTE = {
    "internal_payoff": "“Paid off” by the new advance",
    "early_payoff_cash": "Paid off in cash",
    "write_off": "Written off",
    "status:default": "Default",
}
for k, text in NOTE.items():
    for r in tl[tl.key == k].itertuples():
        fig.add_annotation(
            x=r.event_date,
            y=r.row,
            text=text,
            showarrow=False,
            yshift=18,
            font={"size": 12, "color": c.LOSS if k in ("write_off", "status:default") else c.INK_2},
        )
c.style(fig, "", height=190 + 55 * len(order))
fig.update_layout(
    title=None,
    margin={"t": 90, "l": 80, "r": 16, "b": 40, "autoexpand": False},
    legend={"orientation": "h", "y": 1.02, "x": 0, "xanchor": "left", "yanchor": "bottom"},
)

fig.update_yaxes(categoryorder="array", categoryarray=order[::-1], showgrid=False)
fig.update_xaxes(showgrid=True, gridcolor=c.GRID)
c.chart(fig, "timeline")
st.caption(
    "Small blue dots are payments received; grey crosses are bounced payments. Hover any point for its source row."
)

summary = pd.DataFrame(
    {
        "Deal": [f"Deal {d}" for d in deals.deal_id],
        "Funded": deals.funding_date.map(c.date),
        "Cash out": deals.d6_funder_cash_out_cents.map(c.usd),
        "Cash back": deals.d7_cash_from_merchant_cents.map(c.usd),
        "Tracker says collected": deals.d10_reported_collection_rate.map(lambda x: c.pct(x, 0)),
        "Cash actually collected": deals.d11_adjusted_collection_rate.map(lambda x: c.pct(x, 0)),
        "What happened": [
            OUTCOME.get(o, str(o)) + (f" ({c.date(dt)})" if pd.notna(dt) else "")
            for o, dt in zip(deals.outcome, deals.outcome_date, strict=True)
        ],
    }
)
st.dataframe(summary, hide_index=True, width="stretch")
st.caption(
    "“Tracker says collected” counts a renewal payoff as collected; “cash actually collected” counts only "
    "money from the merchant. Spec: D6, D7, D10, D11, outcome per METRICS.md §2.1."
)

# ---------------------------------------------------------------------------- detail
D_ROWS = [
    ("D1", "Advance", "d1_advance_cents", c.usd),
    ("D2", "Total owed (RTR)", "d2_rtr_cents", c.usd),
    ("D3", "Fee", "d3_fee_cents", c.usd),
    ("D4", "Net cash to merchant", "d4_net_cash_to_merchant_cents", c.usd),
    ("D5", "Broker commission", "d5_commission_cents", c.usd),
    ("D6", "Funder cash out", "d6_funder_cash_out_cents", c.usd),
    ("D7", "Cash from merchant", "d7_cash_from_merchant_cents", c.usd),
    ("D8", "Paid off by a renewal", "d8_internal_payoff_out_cents", c.usd),
    ("D9", "Reported collected", "d9_reported_collected_cents", c.usd),
    ("D10", "Reported collection rate", "d10_reported_collection_rate", c.pct),
    ("D11", "Cash collection rate", "d11_adjusted_collection_rate", c.pct),
    ("D12", "Expected collected to date", "d12_expected_collected_cents", c.usd),
    ("D13", "Pace", "d13_pace", lambda x: c.num(x, 3)),
    ("D14", "Bounce rate (last 20)", "d14_return_rate_last20", c.pct),
    ("D15", "Business days since last good payment", "d15_bdays_since_good_payment", lambda x: c.num(x, 0)),
    ("D16", "Default date", "d16_default_date", c.date),
    ("D17", "Distressed refinance", "d17_distressed", None),
    ("D18", "Bad (tracker's count)", "d18_bad_naive", None),
    ("D19", "Bad (cash view)", "d19_bad_economic", None),
]


def _cell(d: pd.Series, col: str, fmt) -> str:
    v = d[col]
    if col == "d17_distressed" and pd.isna(v):
        return "n/a (not renewed)"
    if c._na(v):
        return "no" if col == "d16_default_date" else "n/a"
    if fmt is None:
        return "yes" if v is True or v == 1 else "no" if v is False or v == 0 else str(v)
    return fmt(v)


with st.expander("All deal metrics"):
    deal_tbl = pd.DataFrame(
        [
            {"Metric": n, "Spec": i, **{f"Deal {d.deal_id}": _cell(d, col, fmt) for _, d in deals.iterrows()}}
            for i, n, col, fmt in D_ROWS
        ]
    )
    st.dataframe(deal_tbl, hide_index=True, width="stretch")

with st.expander("All relationship metrics"):
    r_rows = [
        ("R1", "Funder cash out", c.usd(rel.r1_funder_cash_out_cents)),
        ("R2", "Cash from merchant", c.usd(rel.r2_cash_from_merchant_cents)),
        ("R3", "Cash multiple (back / out)", c.num(rel.r3_cash_multiple, 3)),
        ("R4", "Net cash position", c.usd(rel.r4_net_cash_position_cents)),
        ("R5", "Status", str(rel.r5_status)),
        ("R6", "Cash at risk (open only)", c.usd(rel.r6_economic_exposure_cents)),
        ("R7", "Balance on the books (open only)", c.usd(rel.r7_contractual_balance_cents)),
        ("R8", "Realized loss (finished only)", c.usd(rel.r8_realized_loss_cents)),
        ("R9", "Lost money (finished and multiple < 1)", "yes" if rel.r9_relationship_bad else "no"),
        ("R10", "Deals", str(rel.r10_n_deals)),
        ("R10", "Distressed refinances", str(rel.r10_n_distressed_refi)),
        (
            "R10",
            "Days to resolution",
            "n/a" if c._na(rel.r10_days_to_resolution) else str(rel.r10_days_to_resolution),
        ),
    ]
    st.dataframe(
        pd.DataFrame(r_rows, columns=["Spec", "Metric", "Value"])[["Metric", "Value", "Spec"]],
        hide_index=True,
        width="stretch",
    )
    st.caption(f"As of {c.date(rel.as_of)}.")

with st.expander("Where these numbers come from (source rows)"):
    st.markdown(
        "**Deal records**: the tracker row for each deal, or the bank (ACH) row when the tracker has none."
    )
    rec = pd.DataFrame(
        {
            "Deal": dres.deal_id,
            "Record": [
                "Bank (ACH) funding row, no tracker row" if t else "Deal tracker row"
                for t in dres.tracker_missing
            ],
            **{v: dres[k] for k, v in SRC.items()},
        }
    )
    st.dataframe(rec, hide_index=True, width="stretch", key="lineage_deals")
    st.markdown("**Every event**: each payment, payoff and collections entry with its source row.")
    ev = tl.sort_values(["event_date", "deal_id"])
    events = pd.DataFrame(
        {
            "Date": ev.event_date.map(c.date),
            "Deal": ev.deal_id.astype(int),
            "Event": ev.key.map(EVENT),
            "Amount": ev.amount_cents.map(lambda x: c.usd(abs(x), 2) if pd.notna(x) else ""),
            **{v: ev[k] for k, v in SRC.items()},
        }
    )
    events["Row"] = events["Row"].astype("Int64")
    st.dataframe(events, hide_index=True, width="stretch", key="lineage_events")
    links = c.query(f"SELECT * FROM deal_links WHERE deal_id IN ({marks}) ORDER BY deal_id", ids)
    if not links.empty:
        st.markdown("**Renewal links**, with how each was found and how sure the match is.")
        st.dataframe(links, hide_index=True, width="stretch")
    pay = c.query(f"SELECT * FROM payoff_classes WHERE deal_id IN ({marks}) ORDER BY txn_date", ids)
    if not pay.empty:
        st.markdown("**Payoff classification**: cash payoff vs paid with the new advance (rule L1).")
        st.dataframe(pay, hide_index=True, width="stretch")

c.footer()
