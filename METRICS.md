# MCA Risk Metric Definitions (v1.2)

This is the single source of truth for every number the pipeline produces. Code implements these definitions. When a definition changes, the version and the changelog change with it.

**The problem these definitions address:** when a deal is renewed, the remaining balance is "paid off" out of the *new* advance. Trackers record that as collected or paid off, but no cash came from the merchant. Refinancing a struggling merchant into a lower payment makes the old deal look successful and moves the loss into the new deal. **Deal-level numbers therefore overstate performance. Risk has to be measured on cash, at the merchant-relationship level.**

---

## 0. Conventions

| Item | Rule |
|---|---|
| Units of analysis | **Deal** (one contract); **Relationship** (all deals with one resolved merchant, linked by renewals); **Portfolio** |
| Perspective and sign | Funder's view. Cash *out* is negative and cash *in* is positive. Stored as integer cents. |
| As-of date | Every metric is computed as of a stated date (default `2026-09-30`). Nothing after the as-of date is used. |
| Business days | Federal Reserve ACH calendar: weekdays, excluding federal holidays (Sunday holidays move to Monday). |
| Daily equivalent | Daily payment = `payment` for daily deals and `payment / 5` for weekly deals. |
| Cash vs. non-cash | Only events that move money between the funder and an outside party are **cash**. Internal payoffs, write-offs and modifications are **non-cash** events. |

---

## 1. Event taxonomy (the cash ledger)

Every ACH or log row maps to exactly one ledger event.

| Event | Source | Cash? | Notes |
|---|---|---|---|
| `funding_out` | ACH `FUNDING` | yes (−) | Net of fees and of any internal payoff |
| `commission_out` | tracker commission % × advance | yes (−) | Paid to the ISO through accounts payable, not ACH. Easy to forget, and it changes economics materially. |
| `debit` | ACH `DEBIT` | yes (+) | Each attempted remittance |
| `return` | ACH `RETURN` | yes (−) | Reverses a debit, usually 2 business days later. Codes R01/R08/R09 |
| `early_payoff_cash` | ACH `PAYOFF`, **not** matched to a renewal | yes (+) | The merchant paid off early, often at a discount |
| `internal_payoff` | ACH `PAYOFF`, **matched** to a renewal | **no** | The prior balance was rolled into the new deal |
| `settlement` | ACH `SETTLEMENT` | yes (+) | Post-default instalments |
| `modification` | collections log (Recon / Modification) | no | The payment amount changes from that date |
| `write_off` | collections log (W/O / Charge off) | no | Remaining balance written off |

**Rule L1: telling an internal payoff from a cash payoff.** A `PAYOFF` row on deal *P* dated *t* is an `internal_payoff` if a deal *N* exists that satisfies all of the following:
- *N* belongs to the same resolved merchant;
- *N* funds within 0–2 business days of *t*;
- either the tracker links *N* to *P* (`renewal_of` / `Prior Deal` / notes), **or** the payoff amount is within ±$1 of *N*'s `payoff_amt`, **or** *N*'s funding row satisfies `advance − fee − funding_out ≈ payoff amount` (±$1).
- **or** *N* has no tracker row (Q5 `tracker_missing`), so the three checks above cannot be evaluated. The payoff is still internal when *N* belongs to the same resolved merchant and funds within 0–2 business days of *t*. Such a link is recorded with method `inferred_tracker_missing` and confidence 0.6, and is always routed to `exceptions` for review.

Otherwise the payoff is `early_payoff_cash`. The description text ("RENEWAL PAYOFF" or "PAYOFF") is a hint only. About 40% of internal payoffs are labelled plain "PAYOFF".

**Rule L2: deduplication.** Monthly exports overlap by 3 days. The dedupe key is (normalized date, deal, amount, type, batch ID). Normalize dates **before** deduplicating: the export format changes in July 2024, so the same row appears once as `06/28/2024` and once as `2024-06-28`.

**Rule L3: misposting.** A `DEBIT` posted to deal *P* after *P*'s internal payoff date, while *P*'s renewal *N* is active, is reassigned to *N*. The reassignment is logged as a repair.

---

## 2. Deal-level metrics

| ID | Metric | Definition |
|---|---|---|
| D1 | Advance | Contract purchase price |
| D2 | RTR | Purchased amount (right to receive) = advance × factor |
| D3 | Fee | Origination fees withheld from funding |
| D4 | Net cash to merchant | `−funding_out` = advance − fee − internal payoff of the prior deal |
| D5 | Commission | Advance × ISO commission % |
| D6 | **Funder cash out** | D4 + D5 |
| D7 | **Cash from merchant (CFM)** | Σ debit + Σ return + early_payoff_cash + settlement, through the as-of date |
| D8 | Internal payoff out | This deal's balance rolled into its successor (non-cash) |
| D9 | Reported collected | D7 + D8. *This is what trackers and most MCA reports show.* |
| D10 | Reported collection rate | D9 / D2 |
| D11 | **Renewal-adjusted collection rate** | D7 / D2 |
| D12 | Expected collected to date | min(D2, business days from funding to the as-of date (or close) × original daily equivalent) |
| D13 | **Pace** | D7 / D12. Below 1 means behind schedule. |
| D14 | Return rate (last 20) | Returns among the last 20 debit attempts ÷ 20 (or ÷ number of attempts if fewer) |
| D15 | Days since last good payment | Business days since the most recent debit that was not returned |

### 2.1 Deal outcome (precedence top to bottom; evaluated as of the as-of date)

| Outcome | Rule |
|---|---|
| `written_off` | A write-off event exists, or default + 180 calendar days have passed without a completed settlement |
| `settled` | A settlement was agreed and every instalment was received |
| `defaulted` | **D16** has fired and the deal is not yet settled or written off |
| `refinanced_distressed` | Closed by an `internal_payoff` **and** **D17** holds |
| `renewed_healthy` | Closed by an `internal_payoff` and D17 does not hold |
| `paid_in_full_early` | Closed by `early_payoff_cash` |
| `paid_in_full` | CFM ≥ RTR − $0.01 |
| `active_modified` | Open, with a modification event |
| `active` | Otherwise |

**D16: Default event** fires on the earlier of:
- (a) a collections-log `Default` or `Bankruptcy` entry for the deal;
- (b) no good payment for 20 consecutive scheduled payment days (4 for weekly deals) while the balance is above 0.

The default date is the last good payment date plus one business day. The default *recognition* date is when (a) or (b) fires.

**D17: Distressed refinance (v1).** A renewal is distressed when the **prior** deal, as of the renewal date, satisfies:
- **D14 ≥ 3/20** (3 or more returns in its last 20 attempts), **or**
- **D13 < 0.85** (more than 15% behind schedule), **or**
- a modification event.

*Returns at the renewal date (v1.2):* a debit attempted on or before the renewal date counts as returned if its return posts after that date (typically within 2 business days). With fewer than 20 attempts, D14 is returns ÷ attempts, as in D14's definition. Because of this look-ahead of up to 2 business days, D17 values must not be used as model features: Part 6 uses as-of-renewal D13 / D14 computed without look-ahead.

*Severity attribute, not part of the test:* payment ratio = new daily equivalent ÷ prior effective daily equivalent. Values ≤ 0.75 match the "refinance to a lower payment" pattern.
*Measured on the synthetic book:* precision 0.98, recall 0.87 against the true labels. Requiring a lower payment as well would drop recall to 0.64, because chained refinances often can't lower the payment further.

**D18: Deal bad (naive)** = outcome ∈ {defaulted, settled, written_off}.
**D19: Deal bad (economic)** = D18 **or** `refinanced_distressed`.

---

## 3. Relationship-level metrics

A **relationship** is every deal belonging to one resolved merchant (entity resolution, §6), ordered by funding date and linked by renewals.

| ID | Metric | Definition |
|---|---|---|
| R1 | Funder cash out | Σ D6 over the relationship's deals |
| R2 | Cash from merchant | Σ D7 |
| R3 | **Cash multiple** | R2 / R1. This is the key performance metric. |
| R4 | Net cash position | R2 − R1 |
| R5 | Status | `resolved` if the last deal is paid_in_full, paid_in_full_early, settled or written_off; otherwise `open` (including defaulted but not yet written off) |
| R6 | **Economic exposure** (open only) | max(0, −R4). The capital still at risk. |
| R7 | Contractual balance (open only) | Remaining RTR on the open deal. *What a tracker would call "outstanding."* |
| R8 | **Realized loss** (resolved only) | max(0, −R4) |
| R9 | Relationship bad | Resolved and R3 < 1.00 |
| R10 | Chain profile | Number of deals, number of distressed refinances (D17), time from first funding to resolution |

R1 includes commissions. A relationship can collect more than the advances paid out and still lose money once commissions are counted.

---

## 4. Portfolio metrics

| ID | Metric | Definition |
|---|---|---|
| P1 | Reported vs. adjusted collection rate | RTR-weighted D10 and D11 over **closed** deals |
| P2 | **Renewal masking gap** | P1(reported) − P1(adjusted), in percentage points |
| P3 | Internal-payoff share | Σ D8 / Σ D9 over all deals |
| P4 | Bad rates | D18 rate vs. D19 rate over closed deals, by vintage |
| P5 | Relationship loss metrics | Share of resolved relationships with R9 true; capital-weighted loss rate = Σ R8 / Σ R1 over resolved relationships |
| P6 | Exposure | Σ R6 (economic) vs. Σ R7 (contractual) over open relationships |
| P7 | Vintage curves | Cumulative R2/R1 by months since first funding, one curve per first-funding quarter. Censored at the as-of date. |
| P8 | Refinance quality | Distressed share of renewals; R9 rate for chains with at least one distressed refinance vs. chains without |
| P9 | Modification cure rate | Share of modified deals that end paid in full or renewed healthy |

**Expected on the synthetic book** (from `ground_truth/portfolio_expected.json`, as of 2026-09-30):
- Reported collection rate **92.1%** vs. adjusted **70.8%**: a **21.2-point masking gap**.
- **20.2%** of "collections" are internal payoffs.
- Bad rate **13.9%** naive vs. **29.3%** economic.
- Open economic exposure **$11.95M** vs. contractual balance **$25.24M**.
- **39.8%** of resolved chains that contain a distressed refinance ended in loss, vs. **21.2%** of all resolved relationships.

---

## 5. Modeling targets (for Part 6)

| ID | Target | Population | Notes |
|---|---|---|---|
| T1 | D19 economic bad within **180 business days** of funding | Deals funded at least 180 business days before the as-of date | Fixed horizon avoids censoring bias. The naive D18 target is reported alongside for contrast. |
| T2 | Time to D19 event | All deals; censored at a good close or at the as-of date | Kaplan–Meier by segment; Cox proportional hazards |
| T3 | Relationship R9 | Relationships resolved within 18 months of first funding | Survivorship caveat: long-lived relationships are under-represented |

**Feature rules:**
- Only use information known at the funding date: the scorecard or tracker fields (FICO, average monthly revenue, NSFs in 90 days, positions, time in business, industry, state), the contract terms, the ISO, and **burden** (daily equivalent × 21 / average monthly revenue).
- For renewals, prior-deal features (D13, D14, payment ratio) are computed **as of the renewal date**.
- Never use tracker `status`, outcomes, or any post-funding payment data. A leakage test enforces this.

---

## 6. Entity resolution and data-quality rules

| Rule | Definition |
|---|---|
| Q1 Merchant key | Normalize the name: casefold, strip punctuation, standardize suffixes (LLC/L.L.C., Inc/Inc.), "&" → "and". Block on the first token. Match on EIN when present; otherwise fuzzy token-set similarity ≥ 92 within a block, plus DBA alias tables. Every merge records its method and score. |
| Q2 Renewal link | In priority order: an explicit field (`renewal_of`, `Prior Deal`); notes regex (`renewal of NNNN`, `paid off #NNNN`, `refi #NNNN`); inference via L1. Each link records its method. |
| Q3 Scorecard ↔ deal | Same resolved merchant, application date 0–10 days before funding. If several match, take the closest. |
| Q4 Collections ↔ deal | Deal number if present; otherwise the resolved merchant's deal that is open on the log date |
| Q5 Completeness | Every deal with ACH activity has a tracker row. Missing rows are reconstructed from ACH (funding amount) and flagged `tracker_missing`. |
| Q6 Stale status | Tracker status is never trusted. Outcomes come from §2.1. Disagreements are counted in the data-quality report. |
| Q7 Accounting | Every raw row is either loaded or rejected with a reason. Row counts reconcile at every stage. |

---

## 7. Worked example: relationship M0219 (from the synthetic book)

| | Deal 1102 (new, 2023-11-30) | Deal 1150 (distressed refi, 2024-02-14) |
|---|---|---|
| Advance / factor / RTR | $32,000 / 1.28 / $40,960 | $26,000 / 1.22 / $31,720 |
| Payment | $409.60 daily, 100 days | **$181.26** daily, 175 days (payment ratio 0.44) |
| Funder cash out (D6) | $30,625 net + $3,424 commission = **$34,049** | $2,537 net + $208 commission = **$2,745** |
| Cash from merchant (D7) | **$18,022** | **$8,700** |
| Internal payoff out (D8) | $22,938 (rolled into 1150) | 0 |
| Reported collection rate (D10) | **100%**: "paid off" | 27.4% |
| Adjusted collection rate (D11) | **44.0%** | 27.4% |
| Outcome | `refinanced_distressed` | `written_off` ($23,020) |

*Data note (v1.2):* deal 1102 has no tracker row in `raw/` (Q5), so the pipeline sees no advance, fee or commission for it. The numbers above use the full contract terms. From the spreadsheets alone the pipeline's D6, R1, R3 and R8 are lower bounds: D6 $30,625, R1 $33,370, R3 **0.80**, realized loss **$6,648**. Every other number in the table reproduces exactly.

**Deal view:** one deal paid in full and one loss. **Relationship view:** cash out was $36,794 and cash in was $26,723, so the cash multiple (R3) is **0.73** and the realized loss is **$10,072**. Deal 1102 was never a success.

---

## Changelog
- **v1.0** — initial definitions. D17 uses prior-deal stress signals; payment ratio is a severity attribute.
- **v1.1** (2026-11-03) — L1 gains a fourth condition for a successor deal *N* with no tracker row (`inferred_tracker_missing`, confidence 0.6, always queued for review). Reason: 22 renewals into tracker-missing deals could not be classified under v1.0. Approved by the user.
- **v1.2** (2026-11-04): D17 counts a debit attempted on or before the renewal date as returned when its return posts afterwards (look-ahead of up to 2 business days), and D14 with fewer than 20 attempts is returns ÷ attempts. D17 values are not model features. §7 gains a note that deal 1102 has no tracker row, so the pipeline's D6 / R1 / R3 / R8 for M0219 are lower bounds. Approved by the user.
