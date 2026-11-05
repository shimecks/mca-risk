# Demo walkthrough (about 15 minutes)

The script from `plan.md` §6 with the pipeline's actual numbers (synthetic book, as of 2026-09-30,
METRICS.md v1.3) and what to show at each step. Start the app with `make app` (or open the Community
Cloud deployment). Every number below is on an app page and in `build/findings.md` / `build/model_card.md`
/ `evals/reports/latest.md`; every metric ID on the pages links to its definition.

Before the demo: `make clean all` (about 30 s, no network), `make eval` (all of E1-E9 pass), `make app-data`.

---

## 1. The problem in their words (1 min): Merchant drill-down

**Show:** *Merchant drill-down*, which opens on relationship M0219 (pipeline key `MK1A8B5FBF55`, Indigo
Spa Corp). Point at the chain timeline: deal 1102, then the internal payoff (purple diamond) rolled into
deal 1150 on 2024-02-14, then returns, the D16 default date and the write-off on 2024-10-26.

**Say:** "A receivable isn't its face value." On the deal view, deal 1102 shows a **100%** reported
collection rate (D10): it was "paid off". But only **44.0%** came back as cash (D11). The rest was paid with
the funder's own money via the renewal. Deal 1150 was written off. As a relationship, it lost money:
cash multiple R3 **0.80**, realized loss R8 **$6,648**.

**Caveat (on screen in the yellow box):** deal 1102 is missing from the tracker (Q5), so its $3,424
commission is invisible in the spreadsheets. Cash out (D6, R1) and the realized loss (R8 $6,648) are
**lower bounds**; the true loss is $10,072 (METRICS.md §7). The cash multiple 0.80 is an **upper bound**
on the true 0.73. That is itself a point about the data. Open the *Lineage* popovers: deal 1150 comes from
`raw/deal_tracker_2024.xlsx`, sheet `2024 Deals`, row 31; deal 1102 exists only as an ACH funding row
(`raw/ach_exports/ach_2023_11.csv`, row 1090); the write-off is `raw/collections_log.xlsx` row 100.

## 2. Definitions (3 min): Definitions

**Show:** *Definitions*. Click any metric ID on another page to land on it (e.g. `Definitions?id=D17`).
- Cash vs non-cash events (METRICS.md §1): internal payoffs, write-offs and modifications move no cash.
- **L1**: a payoff is internal when a same-merchant deal funds within 0-2 business days and the tracker
  link, the payoff amount or the funding formula matches (plus the v1.1 rule for tracker-missing successors,
  which always goes to the exceptions queue).
- **D17** distressed refinance and its measured quality: precision **0.986**, recall **0.889** against the
  simulator's hidden labels (*Evals* page, E6).
- Deal (D) vs relationship (R) metrics: R3 cash multiple is the key performance number.

## 3. The data work (3 min): Data quality

**Show:** *Data quality*: rule-hit table, then the sample-row expanders, the exceptions queue and the run
manifest.
- **227 mispostings** repaired (L3): debits posted to a paid-off deal moved to its live renewal.
- **The date-format dedupe trap** (L2): 15,640 overlap duplicates dropped; **125** of them pair a
  `MM/DD/YYYY` row with a `YYYY-MM-DD` row after the July 2024 export switch and would be double counted by a
  naive dedupe.
- **Renewal links hidden in notes** (Q2): 811 links: 634 explicit, 44 from notes regex, 133 inferred.
- **Stale status** (Q6): **364** of 1,967 tracker statuses disagree with the derived outcome; 194 renewed
  deals are typed "paid off".
- **44** deals have ACH activity but no tracker row (Q5); **25** exceptions are queued for review.
- Everything is accounted for (Q7) and every row carries `src_file, src_sheet, src_row`. *Evals* page: E1-E5
  are all at 1.0 (row accounting, field accuracy, dedupe, entity resolution, linkage).

## 4. Findings (4 min): Portfolio

**Show:** *Portfolio* headline cards, then the tabs.
- **Masking:** reported collection rate **92.2%** vs cash-based **70.9%**: a **21.3-point** gap (P1, P2);
  **20.2%** of "collections" are internal payoffs (P3). Tab *Renewal masking*.
- **Bad rate:** **14.0%** naive vs **27.9%** economic over 1,581 closed deals (P4).
  *Caveat:* the simulator's own label says 29.3% (METRICS.md §4). D17 is a rule on observable stress with
  recall about 0.89, so the pipeline's economic rate is a lower bound on the simulator's.
- **Exposure:** economic **$11.84M** vs contractual **$24.84M** over 430 open relationships (P6). Tab
  *Exposure*.
- **Vintages (T1, fixed 180-business-day horizon):** 2024Q3 **34.1%**, 2024Q4 **35.6%**, 2025Q1 **40.5%**,
  against 19.0% for 2024Q1 and 23.3% / 25.3% for 2025Q3 / 2025Q4 (19-29% around them). Tab *Vintages*:
  the bad-rate-by-vintage chart and table mark 2026Q1 as partial and 2026Q2-Q3 as immature; their
  closed-only rates (e.g. 56.5% for 2026Q3 on 23 closed deals) say nothing about vintage quality yet.
- **Refinance quality:** resolved chains with a distressed refinance end in loss **40.4%** of the time vs
  **18.9%** without (P8); 27.1% of renewals are distressed. Tab *Refinance quality*.

## 5. Model (3 min): Model, then Evals

**Show:** *Model* cards and tabs; then the E8 expander on *Evals*.
- Temporal-test AUC **0.660** against an observable ceiling of **0.681** (the simulator's own coefficients
  on observable inputs; the full true hazard reaches 0.726). All **7/7** main driver signs correct.
- Calibration slope **0.96** after a validation-fitted recalibration (0.69 before).
- *Caveats (both user-approved, shown on the Evals page):*
  - The AUC gate was changed from an absolute 0.70 to **"within 0.03 of the observable ceiling"** because
    0.70 would require beating the DGP's own coefficients on this test window. The absolute 0.70 is not met
    and is reported.
  - The recalibration method was chosen **after the test split had been read**, so 0.96 is not a clean
    out-of-sample number; the level is still high on test (calibration-in-the-large +0.089).
- **Naive vs economic target** (tab *Naive vs economic target*): a model trained on the naive target ranks
  about as well (AUC **0.650** vs 0.660, same recall of distressed refis in the top k) but **under-predicts
  the level of risk**: 17.4% predicted vs 23.8% observed overall, and **23%** vs **39%** (39.8% from the
  economic model) on distressed refinances.
- **What I would not use it for yet:** automated declines or pricing without a human; dollar loss
  estimates; monitoring open deals (features are frozen at funding); absolute probabilities across regimes
  without re-fitting (see the model card).

## 6. First 90 days at their company (1 min)

Inventory their spreadsheets and exports; agree the definitions (METRICS.md) with finance, collections and
underwriting; rebuild the history into a cash ledger with lineage; publish a monthly portfolio pack (the
*Portfolio* page); then start modeling, with the eval harness in place from day one.

---

**Backup facts:** `make clean all` runs in about 30 s from `raw/` with no network; 433 tests; the app runs on
a 6.3 MB read-only DuckDB snapshot (`app/data/`) and needs no API key.
