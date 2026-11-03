#!/usr/bin/env python3
"""
Synthetic MCA portfolio generator (dev-only tool). Deterministic for a given --seed.

Simulates a funder's book from 2023-01 to an as-of date of 2026-09-30:
  merchants -> deal chains (new, healthy renewal, distressed refinance) -> daily/weekly ACH remittances,
  NSF returns, payment modifications, early cash payoffs, defaults, settlements, write-offs.

Writes two trees:
  raw/            what the funder actually has: inconsistent spreadsheets and CSV exports
  ground_truth/   the clean truth + expected metric values (EVAL ONLY -- pipeline code must never read it)

The core trap: when a deal is renewed, its remaining balance is "paid off" out of the NEW advance.
Spreadsheets record that as a PAYOFF, so deal-level numbers say "collected / paid off" even though
no cash came from the merchant. A refinance that lowers the payment on a struggling merchant looks
like a success at the deal level and a loss at the relationship level.

Usage: python3 scripts/generate_portfolio.py --out . [--seed 11] [--merchants 1200]
"""
from __future__ import annotations

import argparse
import calendar
import json
import math
import random
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ASOF = date(2026, 9, 30)
FIRST_ORIG = date(2023, 1, 3)
LAST_NEW_MERCHANT = date(2026, 8, 31)
MAX_CHAIN = 6

# ------------------------------------------------------------------ calendar (Fed ACH holidays, Sunday->Monday)
def _nth(y, m, wd, n):
    d = date(y, m, 1)
    while d.weekday() != wd:
        d += timedelta(days=1)
    return d + timedelta(weeks=n - 1)


def _last(y, m, wd):
    d = date(y, m, calendar.monthrange(y, m)[1])
    while d.weekday() != wd:
        d -= timedelta(days=1)
    return d


def _sun_to_mon(d):
    return d + timedelta(days=1) if d.weekday() == 6 else d


HOLIDAYS: set[date] = set()
for _y in range(2023, 2028):
    HOLIDAYS |= {
        _sun_to_mon(date(_y, 1, 1)), _nth(_y, 1, 0, 3), _nth(_y, 2, 0, 3), _last(_y, 5, 0),
        _sun_to_mon(date(_y, 6, 19)), _sun_to_mon(date(_y, 7, 4)), _nth(_y, 9, 0, 1), _nth(_y, 10, 0, 2),
        _sun_to_mon(date(_y, 11, 11)), _nth(_y, 11, 3, 4), _sun_to_mon(date(_y, 12, 25)),
    }
BDAYS: list[date] = []
_d = date(2022, 12, 1)
while _d <= date(2027, 12, 31):
    if _d.weekday() < 5 and _d not in HOLIDAYS:
        BDAYS.append(_d)
    _d += timedelta(days=1)
BIDX = {d: i for i, d in enumerate(BDAYS)}


def bday_on_or_after(d: date) -> date:
    while d not in BIDX:
        d += timedelta(days=1)
    return d


# ------------------------------------------------------------------ reference lists (all fictional)
INDUSTRIES = {  # name: (weight, risk effect on log-hazard, median monthly revenue, name nouns, free-text variants)
    "Restaurant": (0.19, 0.35, 70000, ["Bistro", "Grill", "Pizza", "Kitchen", "Cafe", "Taqueria"], ["Restaurant", "restaurant/bar", "Food Service", "REST"]),
    "Retail": (0.13, 0.05, 60000, ["Market", "Goods", "Outfitters", "Supply", "Boutique"], ["Retail", "retail store", "RETAIL"]),
    "Auto Repair": (0.10, 0.10, 55000, ["Auto Care", "Collision", "Motors", "Tire & Lube"], ["Auto Repair", "auto", "Automotive Svc"]),
    "Construction": (0.12, 0.45, 95000, ["Builders", "Contracting", "Roofing", "Concrete"], ["Construction", "contractor", "GC/Construction"]),
    "Trucking": (0.10, 0.55, 85000, ["Freight", "Logistics", "Transport", "Hauling"], ["Trucking", "transportation", "Freight/Logistics"]),
    "Medical/Dental": (0.08, -0.45, 110000, ["Dental", "Family Medicine", "Chiropractic", "Physical Therapy"], ["Medical", "dental office", "Healthcare"]),
    "Beauty/Salon": (0.08, 0.15, 35000, ["Salon", "Spa", "Barbershop", "Nail Studio"], ["Salon", "beauty", "Personal Care"]),
    "Landscaping": (0.06, 0.20, 50000, ["Landscaping", "Lawn & Garden", "Tree Service"], ["Landscaping", "lawn care", "Landscape"]),
    "E-commerce": (0.07, 0.25, 75000, ["Online", "Direct", "Commerce", "Shop"], ["Ecommerce", "e-comm", "Online Retail"]),
    "Professional Services": (0.07, -0.30, 65000, ["Consulting", "Accounting", "Staffing", "Marketing"], ["Prof Services", "professional svcs", "Services"]),
}
STATES = (["NY"] * 18 + ["NJ"] * 10 + ["FL"] * 16 + ["TX"] * 14 + ["CA"] * 12 + ["GA"] * 7 + ["PA"] * 7 + ["IL"] * 6 + ["OH"] * 5 + ["NC"] * 5)
NAME_A = ["Harbor", "Summit", "Maple", "Copper", "Granite", "Riverside", "Bluebird", "Ironwood", "Cedar", "Lakeshore", "Northgate", "Willow",
          "Redstone", "Silverline", "Oakmont", "Brightwater", "Fox Run", "Highland", "Juniper", "Keystone", "Lantern", "Meadowbrook", "Pinecrest",
          "Quarry", "Rosewood", "Saltmarsh", "Timberline", "Upland", "Vista", "Westbrook", "Amber", "Birchwood", "Crescent", "Driftwood", "Elmhurst",
          "Fairhaven", "Goldleaf", "Hollow Creek", "Indigo", "Kingsbridge", "Larkspur", "Millstone", "Northfield", "Orchard", "Parkside", "Ridgeview"]
SURNAMES = ["Delgado", "Okafor", "Brennan", "Castellano", "Nguyen", "Patel", "Kowalski", "Haddad", "Moreau", "Lindqvist", "Ferreira", "Abernathy",
            "Takahashi", "Osei", "Ramirez", "Sullivan", "Petrov", "Achebe", "Romano", "Whitaker", "Iqbal", "Duarte", "Fitzgerald", "Mbeki"]
SUFFIXES = ["LLC"] * 6 + ["Inc"] * 3 + ["Corp", "Co"]
ISOS = ["Hudson Bridge Capital Advisors", "Gulfstream Merchant Partners", "Lone Oak Funding Group", "Brightpath ISO", "Atlas Row Advisors",
        "Seaboard Business Capital", "Prairie Line Partners", "Keystone Referral Group", "Sunbelt Merchant Services", "Northpoint Brokerage",
        "Direct (house)", "Cobalt Street Capital"]

# ------------------------------------------------------------------ data-generating process (true coefficients; saved to dgp.json)
DGP = {
    "hazard": "daily distress hazard = BASE * exp(lin); lin below. Distress -> (distressed refinance | payment modification | default).",
    "BASE": 0.0016,
    "coef": {
        "latent_quality_z": -0.65, "industry_effect": "INDUSTRIES[ind][1]", "fico_below_600": 0.45, "fico_per_50_below_650": 0.20,
        "log_tib_over_24m": -0.25, "positions_at_funding": 0.30, "nsf_90d": 0.12, "burden_minus_0_15_x4": 4.0,
        "is_distressed_refi": 0.90, "is_healthy_renewal": -0.25, "macro_2024Q3_2025Q1_vintage": 0.35,
    },
    "branch_after_distress": {"distressed_refi": 0.38, "modification": 0.27, "default": 0.35},
    "modification_recovery_prob": 0.45,
    "healthy_renewal_prob": 0.50, "early_cash_payoff_prob": 0.05,
    "settlement_prob": 0.45, "bankruptcy_share_of_defaults": 0.08,
    "nsf_fail_rate": {"healthy": 0.015, "stressed": 0.45, "modified_ok": 0.05, "modified_bad": 0.30},
}


@dataclass
class Merchant:
    merchant_id: str
    legal_name: str
    dba: str
    ein: str
    industry: str
    state: str
    formation_date: date
    fico_base: int
    base_rev: float
    z: float
    first_funding_date: date


@dataclass
class Deal:
    deal_id: int
    merchant_id: str
    seq_in_chain: int
    prior_deal_id: int | None
    renewal_type: str  # new | healthy_renewal | distressed_refi
    funding_date: date
    advance: float
    fee: float
    factor: float
    rtr: float
    frequency: str
    term_bdays: int
    payment: float
    daily_equiv: float
    iso: str
    commission_pct: float
    commission_amt: float
    internal_payoff_of_prior: float
    net_cash_to_merchant: float
    monthly_rev: float
    fico: int
    tib_months: int
    positions_at_funding: int
    nsf_90d: int
    industry: str
    state: str
    burden: float
    hazard_lin: float
    outcome: str = "active"
    outcome_date: date | None = None
    distress_date: date | None = None
    modified_date: date | None = None
    modified_payment: float | None = None
    default_date: date | None = None
    collections_date: date | None = None
    bankruptcy: bool = False
    settlement_amount: float | None = None
    written_off_amount: float = 0.0
    balance_asof: float = 0.0
    rolled_to_deal_id: int | None = None
    internal_payoff_out: float = 0.0
    cash_collected: float = 0.0


def r2(x):
    return float(round(x + 1e-9, 2))


def round_to(x, step):
    return float(max(step, round(x / step) * step))


class Sim:
    def __init__(self, seed: int, n_merchants: int):
        self.rng = random.Random(seed)
        self.n = n_merchants
        self.merchants: list[Merchant] = []
        self.deals: list[Deal] = []
        self.cash: list[dict] = []  # truth cashflow events
        self.next_deal_id = 1001
        self.pending: list[tuple] = []  # (funding_date, merchant, prior_deal, kind, payoff)

    # ---------------------------------------------------------- merchants
    def make_merchants(self):
        rng = self.rng
        used = set()
        inds = list(INDUSTRIES)
        weights = [INDUSTRIES[i][0] for i in inds]
        span = BIDX[bday_on_or_after(LAST_NEW_MERCHANT)] - BIDX[bday_on_or_after(FIRST_ORIG)]
        for i in range(self.n):
            ind = rng.choices(inds, weights)[0]
            while True:
                first = rng.choice(NAME_A) if rng.random() < 0.7 else rng.choice(SURNAMES)
                nm = f"{first} {rng.choice(INDUSTRIES[ind][3])} {rng.choice(SUFFIXES)}"
                if nm not in used:
                    used.add(nm)
                    break
            dba = nm.rsplit(" ", 1)[0] if rng.random() < 0.6 else f"{first} {INDUSTRIES[ind][3][0]}"
            # originations grow over time (triangular toward later dates)
            off = int(span * math.sqrt(rng.random()))
            ffd = BDAYS[BIDX[bday_on_or_after(FIRST_ORIG)] + off]
            tib = int(max(4, rng.lognormvariate(math.log(48), 0.8)))
            self.merchants.append(Merchant(
                merchant_id=f"M{i + 1:04d}", legal_name=nm, dba=dba,
                ein=f"{rng.randint(10, 98)}-{rng.randint(1000000, 9999999)}", industry=ind, state=rng.choice(STATES),
                formation_date=ffd - timedelta(days=int(tib * 30.4)),
                fico_base=int(min(800, max(500, rng.gauss(645, 58)))),
                base_rev=INDUSTRIES[ind][2] * rng.lognormvariate(0, 0.45), z=rng.gauss(0, 1), first_funding_date=ffd,
            ))

    # ---------------------------------------------------------- deal origination
    def originate(self, m: Merchant, fdate: date, prior: Deal | None, kind: str, payoff: float) -> Deal:
        rng = self.rng
        seq = 1 if prior is None else prior.seq_in_chain + 1
        if prior is None:
            rev = m.base_rev * rng.lognormvariate(0, 0.1)
            nsf = min(12, int(rng.expovariate(1 / max(0.3, math.exp(-0.2 - 0.6 * m.z)))))
            pos = min(4, sum(rng.random() < p for p in (0.35 + 0.15 * (m.z < -0.5), 0.18, 0.08, 0.03)))
        elif kind == "healthy_renewal":
            rev = prior.monthly_rev * rng.uniform(1.0, 1.22)
            nsf = max(0, prior.nsf_90d - rng.randint(0, 2))
            pos = max(0, prior.positions_at_funding - (rng.random() < 0.4))
        else:  # distressed refi
            rev = prior.monthly_rev * rng.uniform(0.68, 0.9)
            nsf = prior.nsf_90d + rng.randint(2, 6)
            pos = min(5, prior.positions_at_funding + (rng.random() < 0.5))
        fico = int(min(820, max(480, m.fico_base + rng.gauss(0, 12) - (25 if kind == "distressed_refi" else 0))))
        tib = int((fdate - m.formation_date).days / 30.4)
        freq = "weekly" if rng.random() < 0.2 else "daily"
        risk_px = 0.05 * (fico < 600) + 0.02 * pos + 0.005 * nsf
        if kind == "distressed_refi":
            prev_daily = prior.modified_payment / (5 if prior.frequency == "weekly" else 1) if prior.modified_payment else prior.daily_equiv
            target_daily = prev_daily * rng.uniform(0.42, 0.6)
            advance = round_to(payoff * rng.uniform(1.02, 1.15) + 0, 500)
            factor = round(min(1.42, max(1.18, 1.27 + rng.gauss(0, 0.03))), 2)
            rtr = r2(advance * factor)
            term = int(min(380, max(40, round(rtr / target_daily / 5) * 5)))
        else:
            mult = rng.uniform(0.5, 1.15) if prior is None else (prior.advance / max(prior.monthly_rev, 1)) * rng.uniform(1.0, 1.35)
            advance = round_to(min(250000, max(5000, rev * mult)), 500)
            if kind == "healthy_renewal":
                advance = round_to(max(advance, payoff + 2500), 500)
            factor = round(min(1.49, max(1.18, 1.26 + risk_px + rng.gauss(0, 0.03))), 2)
            rtr = r2(advance * factor)
            term = rng.choice([60, 80, 100, 100, 120, 120, 140, 160, 180])
        daily = rtr / term
        payment = r2(daily * 5) if freq == "weekly" else r2(daily)
        fee = round_to(advance * rng.uniform(0.02, 0.05), 25)
        cpct = {"new": rng.uniform(0.06, 0.12), "healthy_renewal": rng.uniform(0.03, 0.08), "distressed_refi": rng.uniform(0.0, 0.03)}[kind]
        cpct = round(cpct, 3)
        iso = rng.choice(ISOS) if prior is None else prior.iso
        if iso == "Direct (house)":
            cpct = 0.0
        net = r2(advance - fee - payoff)
        if net < 250:  # funder always sends at least a little new cash
            advance = round_to(payoff + fee + 1000, 500)
            rtr = r2(advance * factor)
            daily = rtr / term
            payment = r2(daily * 5) if freq == "weekly" else r2(daily)
            net = r2(advance - fee - payoff)
        burden = daily * 21 / rev
        macro = 0.35 if date(2024, 7, 1) <= fdate <= date(2025, 3, 31) else 0.0
        c = DGP["coef"]
        lin = (c["latent_quality_z"] * m.z + INDUSTRIES[m.industry][1] + c["fico_below_600"] * (fico < 600)
               + c["fico_per_50_below_650"] * (650 - fico) / 50 + c["log_tib_over_24m"] * math.log(max(tib, 3) / 24)
               + c["positions_at_funding"] * pos + c["nsf_90d"] * nsf + c["burden_minus_0_15_x4"] * (burden - 0.15)
               + c["is_distressed_refi"] * (kind == "distressed_refi") + c["is_healthy_renewal"] * (kind == "healthy_renewal") + macro)
        d = Deal(self.next_deal_id, m.merchant_id, seq, prior.deal_id if prior else None, kind, fdate, advance, fee, factor, rtr, freq,
                 term, payment, r2(daily), iso, cpct, r2(advance * cpct), r2(payoff), net, r2(rev), fico, tib, int(pos), int(nsf),
                 m.industry, m.state, round(burden, 4), round(lin, 4))
        self.next_deal_id += 1
        self.deals.append(d)
        self.cash.append({"date": fdate, "deal_id": d.deal_id, "merchant_id": m.merchant_id, "kind": "funding_out", "amount": -net, "is_cash": True})
        if d.commission_amt:
            self.cash.append({"date": fdate, "deal_id": d.deal_id, "merchant_id": m.merchant_id, "kind": "commission_out", "amount": -d.commission_amt, "is_cash": True})
        if payoff:
            self.cash.append({"date": fdate, "deal_id": prior.deal_id, "merchant_id": m.merchant_id, "kind": "internal_payoff", "amount": r2(payoff), "is_cash": False})
        return d

    # ---------------------------------------------------------- servicing simulation of one deal
    def service(self, m: Merchant, d: Deal):
        rng = self.rng
        lam = DGP["BASE"] * math.exp(d.hazard_lin)
        t_distress = rng.expovariate(lam)
        healthy_renew = d.seq_in_chain < MAX_CHAIN and rng.random() < DGP["healthy_renewal_prob"]
        renew_point = rng.uniform(0.5, 0.85)
        early_payoff = rng.random() < DGP["early_cash_payoff_prob"]
        early_point = rng.uniform(0.6, 0.9)
        bal = d.rtr
        pay = d.payment
        state = "healthy"
        stress_t = None
        branch_t = None
        branch = None
        mod_fate = None
        mod_end_t = None
        fail = DGP["nsf_fail_rate"]
        i0 = BIDX[d.funding_date] + 1
        weekday = BDAYS[i0].weekday()
        t = 0
        for idx in range(i0, len(BDAYS)):
            day = BDAYS[idx]
            if day > ASOF:
                break
            t += 1
            if state == "healthy" and t >= t_distress:
                state, stress_t = "stressed", t
                d.distress_date = day
                branch_t = t + rng.randint(5, 25)
                r = rng.random()
                b = DGP["branch_after_distress"]
                branch = "distressed_refi" if (r < b["distressed_refi"] and d.seq_in_chain < MAX_CHAIN) else ("modification" if r < b["distressed_refi"] + b["modification"] else "default")
            # payment day?
            is_payday = d.frequency == "daily" or day.weekday() == weekday or (day.weekday() < weekday and BDAYS[idx + 1].weekday() <= day.weekday())
            if is_payday and bal > 0.004:
                amt = r2(min(pay, bal))
                p_fail = {"healthy": fail["healthy"], "stressed": fail["stressed"], "modified": fail["modified_ok"] if mod_fate == "recover" else fail["modified_bad"]}[state]
                self.cash.append({"date": day, "deal_id": d.deal_id, "merchant_id": m.merchant_id, "kind": "debit", "amount": amt, "is_cash": True})
                if rng.random() < p_fail:
                    rd = BDAYS[idx + 2]
                    code = "R01" if rng.random() < 0.8 else ("R08" if state != "healthy" else "R09")
                    self.cash.append({"date": rd, "deal_id": d.deal_id, "merchant_id": m.merchant_id, "kind": "return", "amount": -amt, "is_cash": True, "return_code": code})
                else:
                    bal = r2(bal - amt)
                    d.cash_collected = r2(d.cash_collected + amt)
            if bal <= 0.004:
                d.outcome, d.outcome_date, d.balance_asof = "paid_in_full", day, 0.0
                return None
            paid_frac = 1 - bal / d.rtr
            if state == "healthy" and early_payoff and paid_frac >= early_point:
                cashpay = r2(bal * (1 - rng.uniform(0.03, 0.10)))
                self.cash.append({"date": day, "deal_id": d.deal_id, "merchant_id": m.merchant_id, "kind": "early_payoff_cash", "amount": cashpay, "is_cash": True})
                d.cash_collected = r2(d.cash_collected + cashpay)
                d.outcome, d.outcome_date, d.balance_asof = "paid_in_full_early", day, 0.0
                return None
            if state == "healthy" and healthy_renew and paid_frac >= renew_point:
                d.outcome, d.outcome_date, d.internal_payoff_out = "renewed_healthy", day, bal
                return ("healthy_renewal", day, bal)
            if state == "stressed" and t >= branch_t:
                if branch == "distressed_refi":
                    d.outcome, d.outcome_date, d.internal_payoff_out = "refinanced_distressed", day, bal
                    return ("distressed_refi", day, bal)
                if branch == "modification":
                    state = "modified"
                    d.modified_date = day
                    pay = r2(pay * rng.uniform(0.3, 0.6))
                    d.modified_payment = pay
                    mod_fate = "recover" if rng.random() < DGP["modification_recovery_prob"] else "default"
                    mod_end_t = t + rng.randint(15, 90)
                    continue
                self._default(m, d, day, bal)
                return None
            if state == "modified" and mod_fate == "default" and t >= mod_end_t:
                self._default(m, d, day, bal)
                return None
        d.balance_asof = r2(bal)
        d.outcome = {"healthy": "active", "stressed": "active_stressed", "modified": "active_modified"}[state]
        return None

    def _default(self, m: Merchant, d: Deal, day: date, bal: float):
        rng = self.rng
        d.outcome, d.outcome_date, d.default_date = "defaulted", day, day
        d.balance_asof = r2(bal)
        cdate = BDAYS[BIDX[day] + rng.randint(10, 30)]
        if cdate <= ASOF:
            d.collections_date = cdate
        if rng.random() < DGP["bankruptcy_share_of_defaults"]:
            d.bankruptcy = True
        elif d.collections_date and rng.random() < DGP["settlement_prob"]:
            amt = round_to(bal * rng.uniform(0.25, 0.6), 100)
            d.settlement_amount = amt
            n = rng.choice([1, 2, 3, 4, 6])
            start = cdate + timedelta(days=rng.randint(20, 60))
            missed, paid_total = 0, 0.0
            for k in range(n):
                pd_ = bday_on_or_after(start + timedelta(days=30 * k))
                if pd_ > ASOF:
                    break
                if rng.random() < 0.85:
                    inst = r2(amt / n)
                    self.cash.append({"date": pd_, "deal_id": d.deal_id, "merchant_id": m.merchant_id, "kind": "settlement", "amount": inst, "is_cash": True,
                                      "note": f"SETTLEMENT PMT {k + 1} OF {n}"})
                    paid_total += inst
                    d.cash_collected = r2(d.cash_collected + inst)
                else:
                    missed += 1
                    if missed >= 2:
                        break
            d.balance_asof = r2(max(0.0, bal - paid_total))
            if missed >= 2:
                d.outcome = "defaulted"  # settlement broken
            elif paid_total >= amt - 0.05:
                d.outcome = "settled"
        wo = day + timedelta(days=180)
        if wo <= ASOF and d.outcome != "settled":
            d.written_off_amount = d.balance_asof
            d.outcome = "written_off" if not d.settlement_amount else d.outcome
            if d.outcome == "defaulted":
                d.outcome = "written_off"
        if d.outcome == "settled":
            d.written_off_amount = d.balance_asof  # forgiven remainder
            d.balance_asof = 0.0

    # ---------------------------------------------------------- run
    def run(self):
        self.make_merchants()
        for m in sorted(self.merchants, key=lambda x: x.first_funding_date):
            prior, kind, fdate, payoff = None, "new", m.first_funding_date, 0.0
            while True:
                d = self.originate(m, fdate, prior, kind, payoff)
                if prior is not None:
                    prior.rolled_to_deal_id = d.deal_id
                nxt = self.service(m, d)
                if nxt is None:
                    break
                kind, fdate, payoff = nxt
                prior = d
        # renumber deals by funding date (spreadsheets are chronological)
        order = sorted(self.deals, key=lambda x: (x.funding_date, x.deal_id))
        remap = {d.deal_id: 1001 + i for i, d in enumerate(order)}
        for d in self.deals:
            d.deal_id = remap[d.deal_id]
            d.prior_deal_id = remap.get(d.prior_deal_id) if d.prior_deal_id else None
            d.rolled_to_deal_id = remap.get(d.rolled_to_deal_id) if d.rolled_to_deal_id else None
        for c in self.cash:
            c["deal_id"] = remap[c["deal_id"]]
        self.deals.sort(key=lambda x: x.deal_id)


# ====================================================================== messy raw output
def name_variant(rng: random.Random, m: Merchant) -> str:
    r = rng.random()
    nm = m.legal_name
    if r < 0.55:
        return nm
    if r < 0.65:
        return nm.upper()
    if r < 0.73:
        return nm.rsplit(" ", 1)[0]
    if r < 0.79:
        return nm.replace(" LLC", ", L.L.C.").replace(" Inc", ", Inc.")
    if r < 0.85:
        return nm.replace(" & ", " and ")
    if r < 0.92:
        return m.dba
    # one-character typo
    s = list(nm)
    k = rng.randrange(1, len(s) - 1)
    if s[k].isalpha():
        s[k] = s[k + 1] if s[k + 1].isalpha() else s[k]
        s[k + 1] = nm[k]
    return "".join(s)


def money_str(rng, x, style):
    if style == "dollar":
        return f"${x:,.2f}" if rng.random() < 0.85 else f"${x:,.0f}"
    if style == "plain":
        return round(x, 2)
    return f"{x:,.2f}"


def status_as_of(d: Deal, when: date, rng) -> str:
    """What a human would have typed in the tracker, as of the sheet's last update."""
    o = d.outcome if (d.outcome_date and d.outcome_date <= when) else "active"
    if d.default_date and d.default_date <= when and o not in ("settled", "written_off"):
        o = "defaulted"
    m = {
        "active": ["Active", "active", "ACTIVE", "Current"],
        "active_stressed": ["Active", "Active"],
        "active_modified": ["Active", "Modified", "Recon"],
        "paid_in_full": ["Paid Off", "PIF", "paid", "Paid in Full"],
        "paid_in_full_early": ["Paid Off", "PIF - early", "Paid Off"],
        # the masking source: renewals typed as paid off
        "renewed_healthy": ["Renewed", "Paid Off", "RENEWED", "Paid Off - Renewal"],
        "refinanced_distressed": ["Renewed", "Paid Off", "Renewed", "Refi"],
        "defaulted": ["Default", "Collections", "DEFAULT"],
        "settled": ["Settled", "Settlement", "Closed - Settled"],
        "written_off": ["Written Off", "W/O", "Charged Off"],
    }[o]
    return rng.choice(m)


def write_raw(sim: Sim, out: Path, rng: random.Random) -> list[dict]:
    raw = out / "raw"
    (raw / "ach_exports").mkdir(parents=True, exist_ok=True)
    M = {m.merchant_id: m for m in sim.merchants}
    D = {d.deal_id: d for d in sim.deals}
    anomalies: list[dict] = []

    # ---------- deal trackers (one workbook per funding year, schema drifts) ----------
    missing_tracker = set(d.deal_id for d in sim.deals if rng.random() < 0.025)
    for did in sorted(missing_tracker):
        anomalies.append({"type": "deal_missing_from_tracker", "deal_id": did, "detail": "Deal funded and serviced (see ACH exports) but no tracker row"})
    last_update = {2023: date(2024, 3, 29), 2024: date(2025, 2, 28), 2025: date(2026, 6, 30), 2026: date(2026, 9, 25)}
    by_year: dict[int, list[dict]] = {}
    stale = 0
    for d in sim.deals:
        if d.deal_id in missing_tracker:
            continue
        m = M[d.merchant_id]
        y = d.funding_date.year
        st = status_as_of(d, last_update[y], rng)
        if d.outcome_date and d.outcome_date > last_update[y] and st in ("Active", "active", "ACTIVE", "Current"):
            stale += 1
        nm = name_variant(rng, m)
        link_note = ""
        if d.prior_deal_id:
            link_note = rng.choice([f"renewal of {d.prior_deal_id}", f"RENEWAL - paid off #{d.prior_deal_id}", f"ren. prev deal {d.prior_deal_id}",
                                    "renewal", "rnwl", f"refi #{d.prior_deal_id}" if d.renewal_type == "distressed_refi" else "renewal - upsell", ""])
        else:
            link_note = rng.choice(["new", "New merchant", "", "", "NEW - ISO referral"])
        if d.modified_date and d.modified_date <= last_update[y]:
            link_note = (link_note + "; " if link_note else "") + f"recon approved pmt ${d.modified_payment:,.2f}"
        if y == 2023:
            fd = d.funding_date.strftime("%m/%d/%y")
            row = {"Deal #": d.deal_id, "Merchant": nm, "DBA": m.dba if rng.random() < 0.7 else "", "Fund Date": fd,
                   "Advance Amount": money_str(rng, d.advance, "dollar"), "Factor": d.factor, "RTR": money_str(rng, d.rtr, "dollar"),
                   "Daily Pmt": money_str(rng, d.payment, "dollar") if d.frequency == "daily" else f"{money_str(rng, d.payment, 'dollar')} wkly",
                   "Term (days)": d.term_bdays if d.frequency == "daily" else f"{d.term_bdays // 5} wks", "ISO": d.iso,
                   "Commission %": f"{d.commission_pct * 100:.1f}%", "Orig Fee": money_str(rng, d.fee, "dollar"), "Status": st, "Notes": link_note}
            if d.prior_deal_id:
                anomalies.append({"type": "renewal_link_only_in_notes", "deal_id": d.deal_id, "detail": f"prior={d.prior_deal_id}; note='{link_note}'"})
        elif y == 2024:
            prior = d.prior_deal_id if (d.prior_deal_id and rng.random() < 0.75) else None
            if d.prior_deal_id and prior is None:
                anomalies.append({"type": "renewal_link_missing", "deal_id": d.deal_id, "detail": f"true prior={d.prior_deal_id}; 'Prior Deal' blank"})
            row = {"Deal ID": d.deal_id, "Business Name": nm, "Funded": d.funding_date, "Purchase Price": d.advance,
                   "Factor Rate": f"{d.factor:.2f}x", "Payback": d.rtr, "Pmt Amt": d.payment, "Freq": "Daily" if d.frequency == "daily" else "Weekly",
                   "Broker": d.iso, "Comm": d.commission_pct, "Fees": d.fee, "Prior Deal": prior, "Status": st, "Notes": link_note if rng.random() < 0.5 else ""}
        else:
            prior = d.prior_deal_id if (d.prior_deal_id and rng.random() < 0.85) else None
            if d.prior_deal_id and prior is None:
                anomalies.append({"type": "renewal_link_missing", "deal_id": d.deal_id, "detail": f"true prior={d.prior_deal_id}; 'renewal_of' blank"})
            def maybe(v, p=0.06):
                return None if rng.random() < p else v
            row = {"deal_id": d.deal_id, "legal_name": nm, "ein": m.ein if rng.random() < 0.7 else None, "funding_date": d.funding_date.isoformat(),
                   "advance": d.advance, "factor": d.factor, "rtr": d.rtr, "payment": d.payment, "frequency": d.frequency.upper()[0],
                   "term": d.term_bdays, "iso_name": d.iso, "iso_comm_pct": round(d.commission_pct * 100, 1), "orig_fee": d.fee,
                   "renewal_of": prior, "payoff_amt": d.internal_payoff_of_prior if (prior and rng.random() < 0.9) else None, "status": st,
                   "fico": maybe(d.fico), "avg_monthly_rev": maybe(round(d.monthly_rev, -2)), "tib_months": maybe(d.tib_months),
                   "industry": rng.choice(INDUSTRIES[d.industry][4]), "state": d.state, "positions": maybe(d.positions_at_funding),
                   "nsf_90d": maybe(d.nsf_90d), "notes": link_note if rng.random() < 0.4 else None}
        by_year.setdefault(y, []).append(row)
    for y, rows in by_year.items():
        name = f"deal_tracker_{y}.xlsx" if y < 2026 else "deal_tracker_2026_YTD.xlsx"
        df = pd.DataFrame(rows)
        with pd.ExcelWriter(raw / name, engine="xlsxwriter") as xw:
            # 2023 workbook has a junk title row above the header, like a hand-made sheet
            if y == 2023:
                pd.DataFrame([["FUNDED DEALS 2023 - internal, do not forward"]]).to_excel(xw, sheet_name="Funded", index=False, header=False)
                df.to_excel(xw, sheet_name="Funded", index=False, startrow=2)
            else:
                df.to_excel(xw, sheet_name="Funded" if y != 2024 else "2024 Deals", index=False)
    anomalies.append({"type": "stale_status_rows", "deal_id": None, "detail": f"{stale} tracker rows say Active although the deal had closed by the as-of date"})

    # ---------- underwriting scorecards for 2023-2024 deals (separate workbook, matched by name + date) ----------
    sc = []
    for d in sim.deals:
        if d.funding_date.year > 2024 or rng.random() < 0.06:
            if d.funding_date.year <= 2024:
                anomalies.append({"type": "scorecard_missing", "deal_id": d.deal_id, "detail": "No underwriting scorecard row"})
            continue
        m = M[d.merchant_id]
        sc.append({"Business": name_variant(rng, m), "App Date": (d.funding_date - timedelta(days=rng.randint(1, 7))).strftime("%Y-%m-%d"),
                   "Requested": d.advance if rng.random() < 0.7 else round_to(d.advance * rng.uniform(1.0, 1.4), 500),
                   "FICO": d.fico if rng.random() > 0.04 else None, "Avg Mo Rev": f"${d.monthly_rev:,.0f}",
                   "NSFs (90d)": d.nsf_90d if rng.random() > 0.05 else None, "# Positions": d.positions_at_funding,
                   "TIB (yrs)": round(d.tib_months / 12, 1), "Industry": rng.choice(INDUSTRIES[d.industry][4]), "State": d.state,
                   "UW Notes": rng.choice(["", "", "clean statements", "seasonal dip in Feb", "2 MCA debits on stmt", "owner responsive", "watch NSFs"])})
    pd.DataFrame(sc).sample(frac=1, random_state=3).to_excel(raw / "underwriting_scorecards_2023_2024.xlsx", sheet_name="Scorecards", index=False)

    # ---------- ACH exports (monthly CSVs; each overlaps the prior month's last 3 days) ----------
    rows = []
    batch = {}
    for c in sim.cash:
        if c["kind"] == "commission_out":
            continue  # commissions live only in the tracker (paid via AP, not ACH)
        d = D[c["deal_id"]]
        m = M[c["merchant_id"]]
        kind = c["kind"]
        typ = {"funding_out": "FUNDING", "debit": "DEBIT", "return": "RETURN", "early_payoff_cash": "PAYOFF", "internal_payoff": "PAYOFF", "settlement": "SETTLEMENT"}[kind]
        desc = {"funding_out": "FUNDING NET OF FEES" if not d.internal_payoff_of_prior else rng.choice(["FUNDING NET OF FEES/PAYOFF", "FUNDING NET OF FEES"]),
                "debit": "DAILY REMIT" if d.frequency == "daily" else "WEEKLY REMIT",
                "return": f"RETURNED {c.get('return_code', 'R01')}",
                "early_payoff_cash": rng.choice(["EARLY PAYOFF", "PAYOFF", "PAYOFF - DISC"]),
                "internal_payoff": rng.choice(["RENEWAL PAYOFF", "RENEWAL PAYOFF", "PAYOFF", "PAYOFF"]),
                "settlement": c.get("note", "SETTLEMENT")}[kind]
        bkey = (c["date"], typ)
        batch.setdefault(bkey, f"B{c['date']:%y%m%d}{len(batch) % 97:02d}{'FDRS'[('FUNDING','DEBIT','RETURN','SETTLEMENT').index(typ) if typ in ('FUNDING','DEBIT','RETURN','SETTLEMENT') else 0]}")
        rows.append({"Trans Date": c["date"], "Deal": c["deal_id"], "Merchant Name": name_variant(rng, m) if rng.random() < 0.3 else m.legal_name,
                     "Amount": c["amount"] if typ != "FUNDING" else c["amount"], "Type": typ, "Return Code": c.get("return_code", ""),
                     "Batch ID": batch[bkey], "Description": desc, "_truth_kind": kind})
    ach = pd.DataFrame(rows).sort_values(["Trans Date", "Deal", "Type"]).reset_index(drop=True)
    # misposting: first payments of some renewals posted to the OLD deal id
    misposted = 0
    for d in sim.deals:
        if d.prior_deal_id and rng.random() < 0.15:
            idx = ach.index[(ach["Deal"] == d.deal_id) & (ach["Type"] == "DEBIT")][: rng.randint(1, 3)]
            ach.loc[idx, "Deal"] = d.prior_deal_id
            for i in idx:
                anomalies.append({"type": "payment_posted_to_prior_deal", "deal_id": d.deal_id, "detail": f"ACH row dated {ach.at[i, 'Trans Date']} shows deal {d.prior_deal_id}"})
            misposted += len(idx)
    ach["_month"] = ach["Trans Date"].map(lambda x: (x.year, x.month))
    months = sorted(set(ach["_month"]))
    dup_total = 0
    for (y, mo) in months:
        start = date(y, mo, 1)
        end = date(y, mo, calendar.monthrange(y, mo)[1])
        part = ach[(ach["Trans Date"] >= start - timedelta(days=3)) & (ach["Trans Date"] <= end)].copy()
        dup_total += int((part["Trans Date"] < start).sum())
        fmt = "%m/%d/%Y" if (y, mo) < (2024, 7) else "%Y-%m-%d"  # processor changed export format mid-2024
        part["Trans Date"] = part["Trans Date"].map(lambda x: x.strftime(fmt))
        part.drop(columns=["_truth_kind", "_month"]).to_csv(raw / "ach_exports" / f"ach_{y}_{mo:02d}.csv", index=False)
    anomalies.append({"type": "overlapping_export_duplicates", "deal_id": None, "detail": f"{dup_total} rows appear in two consecutive monthly exports (same Batch ID)"})
    anomalies.append({"type": "export_date_format_change", "deal_id": None, "detail": "ACH exports switch from MM/DD/YYYY to YYYY-MM-DD starting 2024-07"})

    # ---------- collections & servicing log (free-text notes) ----------
    log = []
    for d in sim.deals:
        m = M[d.merchant_id]
        did = d.deal_id if rng.random() > 0.25 else None
        if d.deal_id and did is None and (d.collections_date or d.modified_date):
            anomalies.append({"type": "collections_row_missing_deal_id", "deal_id": d.deal_id, "detail": "log row identifies merchant by name only"})
        if d.modified_date:
            log.append({"Date": d.modified_date.strftime("%m/%d/%Y"), "Merchant": name_variant(rng, m), "Deal #": did, "Event": rng.choice(["Recon", "Reconciliation", "Modification"]),
                        "Balance": None, "Amount": None,
                        "Notes": rng.choice([f"Merchant requested reconciliation, pmt reduced from ${d.payment:,.2f} to ${d.modified_payment:,.2f} eff {d.modified_date:%m/%d}",
                                             f"approved recon - new pmt {d.modified_payment:,.0f}/{'day' if d.frequency == 'daily' else 'wk'} per bank stmts",
                                             f"Owner called, sales down. Lowered to ${d.modified_payment:,.2f}. revisit in 30d"])})
        if d.collections_date:
            ev = "Bankruptcy" if d.bankruptcy else "Default"
            notes = (f"BK filed ch.7, stay in effect. no further collection" if d.bankruptcy else
                     rng.choice(["Payments stopped, merchant unresponsive. sent to outside counsel", "ACH revoked (R08 x3). Default notice sent",
                                 "Account closed per bank. UCC filed", "merchant switched banks, stopped payments"]))
            log.append({"Date": d.collections_date.strftime("%m/%d/%Y"), "Merchant": name_variant(rng, m), "Deal #": did, "Event": ev,
                        "Balance": None if rng.random() < 0.3 else f"${(d.balance_asof + (sum(c['amount'] for c in sim.cash if c['deal_id'] == d.deal_id and c['kind'] == 'settlement'))):,.2f}",
                        "Amount": None, "Notes": notes})
            if d.settlement_amount:
                sd = bday_on_or_after(d.collections_date + timedelta(days=rng.randint(10, 40)))
                if sd <= ASOF:
                    n_inst = len([c for c in sim.cash if c["deal_id"] == d.deal_id and c["kind"] == "settlement"])
                    log.append({"Date": sd.strftime("%m/%d/%Y"), "Merchant": name_variant(rng, m), "Deal #": did, "Event": "Settlement",
                                "Balance": None, "Amount": f"${d.settlement_amount:,.0f}",
                                "Notes": rng.choice([f"Agreed to settle for {d.settlement_amount / 1000:.1f}k in installments",
                                                     f"settlement approved by mgmt ${d.settlement_amount:,.0f}", "Settled - see agreement in drive"])})
            if d.outcome == "written_off" and d.default_date + timedelta(days=180) <= ASOF:
                log.append({"Date": (d.default_date + timedelta(days=180)).strftime("%m/%d/%Y"), "Merchant": name_variant(rng, m), "Deal #": did,
                            "Event": rng.choice(["Write-off", "W/O", "Charge off"]), "Balance": f"${d.written_off_amount:,.2f}", "Amount": None, "Notes": "uncollectible per policy"})
    pd.DataFrame(log).sort_values("Date", key=lambda s: pd.to_datetime(s, format="%m/%d/%Y")).to_excel(raw / "collections_log.xlsx", sheet_name="Log", index=False)
    return anomalies


# ====================================================================== ground truth + expected metrics
def ground_truth(sim: Sim, out: Path, anomalies: list[dict]) -> dict:
    gt = out / "ground_truth"
    gt.mkdir(parents=True, exist_ok=True)
    merchants = pd.DataFrame([asdict(m) for m in sim.merchants])
    deals = pd.DataFrame([asdict(d) for d in sim.deals])
    cash = pd.DataFrame(sim.cash)
    merchants.to_csv(gt / "merchants.csv", index=False)
    cash.sort_values(["date", "deal_id", "kind"]).to_csv(gt / "cashflows.csv", index=False)
    pd.DataFrame(anomalies).to_csv(gt / "anomalies.csv", index=False)

    # ---- deal-level metrics (definitions in METRICS.md) ----
    cin = cash[cash.is_cash & cash.kind.isin(["debit", "return", "early_payoff_cash", "settlement"])].groupby("deal_id").amount.sum()
    ipo = cash[cash.kind == "internal_payoff"].groupby("deal_id").amount.sum()
    deals["cash_from_merchant"] = deals.deal_id.map(cin).fillna(0).round(2)
    deals["internal_payoff_out"] = deals.deal_id.map(ipo).fillna(0).round(2)
    deals["funder_cash_out"] = (deals.net_cash_to_merchant + deals.commission_amt).round(2)
    deals["reported_collected"] = (deals.cash_from_merchant + deals.internal_payoff_out).round(2)
    deals["reported_collection_rate"] = (deals.reported_collected / deals.rtr).round(4)
    deals["renewal_adjusted_collection_rate"] = (deals.cash_from_merchant / deals.rtr).round(4)
    closed = {"paid_in_full", "paid_in_full_early", "renewed_healthy", "refinanced_distressed", "settled", "written_off"}
    deals["is_closed"] = deals.outcome.isin(closed)
    deals["deal_bad_naive"] = deals.outcome.isin(["defaulted", "settled", "written_off"])
    deals["deal_bad_economic"] = deals.deal_bad_naive | (deals.outcome == "refinanced_distressed")
    # expected collections to date vs actual (pace) for active deals
    def expected_to_date(r):
        if r.outcome.startswith("active"):
            n = BIDX[bday_on_or_after(ASOF)] - BIDX[r.funding_date]
            per = r.daily_equiv
            return round(min(r.rtr, n * per), 2)
        return None
    deals["expected_collected_asof"] = deals.apply(expected_to_date, axis=1)
    deals["pace_asof"] = (deals.cash_from_merchant / deals.expected_collected_asof).round(4)
    for c in ("funding_date", "outcome_date", "distress_date", "modified_date", "default_date", "collections_date"):
        deals[c] = deals[c].astype("string")
    deals.to_csv(gt / "deals.csv", index=False)

    # ---- relationship-level metrics ----
    g = deals.groupby("merchant_id")
    rel = pd.DataFrame({
        "n_deals": g.deal_id.count(),
        "n_distressed_refi": g.renewal_type.apply(lambda s: int((s == "distressed_refi").sum())),
        "first_funding_date": g.funding_date.min(),
        "total_advanced_contract": g.advance.sum().round(2),
        "total_rtr_contract": g.rtr.sum().round(2),
        "net_new_cash_out": g.net_cash_to_merchant.sum().round(2),
        "commissions_out": g.commission_amt.sum().round(2),
        "funder_cash_out": g.funder_cash_out.sum().round(2),
        "cash_from_merchant": g.cash_from_merchant.sum().round(2),
        "reported_collected": g.reported_collected.sum().round(2),
    })
    last = deals.sort_values("deal_id").groupby("merchant_id").tail(1).set_index("merchant_id")
    rel["final_outcome"] = last.outcome
    rel["status"] = last.outcome.map(lambda o: "open" if o.startswith("active") or o == "defaulted" else "resolved")
    rel["cash_multiple"] = (rel.cash_from_merchant / rel.funder_cash_out).round(4)
    rel["net_cash_position"] = (rel.cash_from_merchant - rel.funder_cash_out).round(2)
    rel["economic_exposure"] = rel.net_cash_position.map(lambda x: round(max(0.0, -x), 2)).where(rel.status == "open", 0.0)
    rel["contractual_balance_asof"] = last.balance_asof.where(last.outcome.str.startswith("active") | (last.outcome == "defaulted"), 0.0).round(2)
    rel["realized_loss"] = rel.net_cash_position.map(lambda x: round(max(0.0, -x), 2)).where(rel.status == "resolved", 0.0)
    rel["relationship_bad"] = (rel.status == "resolved") & (rel.cash_multiple < 1.0)
    rel.reset_index().to_csv(gt / "relationships.csv", index=False)

    # ---- portfolio headline numbers (what the demo should surface) ----
    cl = deals[deals.is_closed]
    renewed = deals[deals.outcome.isin(["renewed_healthy", "refinanced_distressed"])]
    res = rel[rel.status == "resolved"]
    exp = {
        "as_of": ASOF.isoformat(), "n_merchants": int(len(rel)), "n_deals": int(len(deals)),
        "deals_by_type": deals.renewal_type.value_counts().to_dict(), "deals_by_outcome": deals.outcome.value_counts().to_dict(),
        "closed_deals_reported_collection_rate_rtr_weighted": round(cl.reported_collected.sum() / cl.rtr.sum(), 4),
        "closed_deals_renewal_adjusted_collection_rate_rtr_weighted": round(cl.cash_from_merchant.sum() / cl.rtr.sum(), 4),
        "share_of_reported_collections_that_are_internal_payoffs": round(deals.internal_payoff_out.sum() / deals.reported_collected.sum(), 4),
        "renewed_deals_with_distressed_refi": int((renewed.outcome == "refinanced_distressed").sum()),
        "renewed_deals_total": int(len(renewed)),
        "deal_level_bad_rate_naive_closed": round(cl.deal_bad_naive.mean(), 4),
        "deal_level_bad_rate_economic_closed": round(cl.deal_bad_economic.mean(), 4),
        "resolved_relationships": int(len(res)),
        "resolved_relationship_cash_multiple": round(res.cash_from_merchant.sum() / res.funder_cash_out.sum(), 4),
        "resolved_relationships_with_loss_share": round(res.relationship_bad.mean(), 4),
        "open_economic_exposure_total": round(rel.economic_exposure.sum(), 2),
        "open_contractual_balance_total": round(rel.contractual_balance_asof.sum(), 2),
        "distressed_refi_chains_ending_in_loss_share": round(res[res.n_distressed_refi > 0].relationship_bad.mean(), 4) if (res.n_distressed_refi > 0).any() else None,
        "anomaly_counts": pd.DataFrame(anomalies).type.value_counts().to_dict(),
    }
    (gt / "portfolio_expected.json").write_text(json.dumps(exp, indent=2) + "\n")
    (gt / "dgp.json").write_text(json.dumps(DGP, indent=2) + "\n")
    return exp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--merchants", type=int, default=1200)
    a = ap.parse_args()
    out = Path(a.out)
    sim = Sim(a.seed, a.merchants)
    sim.run()
    anomalies = write_raw(sim, out, random.Random(a.seed + 1))
    exp = ground_truth(sim, out, anomalies)
    print(json.dumps(exp, indent=1))


if __name__ == "__main__":
    main()
