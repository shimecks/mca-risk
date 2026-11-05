# Findings: cash-based risk on the synthetic MCA book

As of **2026-09-30**, METRICS.md v1.3 (D17 `attempt` basis). Every number below is read from the persisted metric tables (`portfolio_metrics`, `target_metrics`, `relationship_metrics`, `deal_metrics`) and cites its metric IDs; `build/findings_numbers.json` holds the query behind each one. Figures: `build/figures/*.png` (interactive: `*.plotly.json`, data: `build/figures/data/*.csv`).

## 1. Renewal masking: one in five "collected" dollars is the funder paying itself

- On closed deals, RTR-weighted, the **reported collection rate is 92.2%** (P1, D10) but the **cash-based rate is 70.9%** (P1, D11): a **masking gap of 21.3 pp** (P2).
- **20.2%** of everything the trackers call collected is an internal payoff rolled into a renewal, not cash from the merchant (P3 = sum D8 / sum D9).
- The gap is present in every vintage (figure `masking.png`). For the youngest vintages P1 only sees the deals that have closed already, which are mostly renewals, so their reported rate is close to 100% and the gap is widest (e.g. 2026Q3: reported 100.0% vs adjusted 38.9%, P1). Read young vintages with that bias in mind.

## 2. Bad rate doubles once distressed refinances count

- Over closed deals (1,581), the **naive bad rate is 14.0%** (P4, D18: defaulted, settled, written off) and the **economic bad rate is 27.9%** (P4, D19: plus refinanced_distressed, D17).
- On the **fixed 180-business-day horizon** (T1, METRICS.md §5; 1,297 of 2,011 funded deals are old enough), **29.1%** of deals are economically bad within 180 business days vs **16.7%** naive (D18 at the horizon). The fixed horizon is the unbiased comparison across vintages; the closed-deals view is not (section 4).

## 3. Exposure: the tracker's outstanding overstates the capital at risk

- Across 430 open relationships, the **contractual balance is $24.84M** (P6, sum R7: remaining RTR) but the **economic exposure is $11.84M** (P6, sum R6: cash out not yet recovered). Less than half of the "outstanding" is cash the funder can still lose; the rest is unearned margin. The economic figure is the number to reserve against.

## 4. Vintage deterioration (T1 fixed horizon)

T1 economic bad rate (D19 within 180 business days of funding) by funding quarter, with 95% Wilson intervals; naive (D18 at the horizon) and the closed-deals-only P4 rate for contrast. A vintage is *immature* when none of its deals has reached the horizon and *partial* when only some have; their T1 rates are not comparable and are left out of the trend. Figure: `bad_rate_vintage.png`.

| Vintage | Maturity | Mature / funded | T1 economic [95% CI] | T1 naive | P4 economic, closed only (n) |
|---|---|---:|---|---:|---|
| 2023Q1 | mature | 9 / 9 | 33.3% [12.1%-64.6%] | 11.1% | 33.3% (9) |
| 2023Q2 | mature | 21 / 21 | 19.0% [7.7%-40.0%] | 9.5% | 19.0% (21) |
| 2023Q3 | mature | 34 / 34 | 17.6% [8.3%-33.5%] | 5.9% | 17.6% (34) |
| 2023Q4 | mature | 55 / 55 | 29.1% [18.8%-42.1%] | 18.2% | 29.1% (55) |
| 2024Q1 | mature | 58 / 58 | 19.0% [10.9%-30.9%] | 8.6% | 20.7% (58) |
| 2024Q2 | mature | 102 / 102 | 32.4% [24.1%-41.9%] | 18.6% | 32.7% (101) |
| 2024Q3 | mature | 123 / 123 | 34.1% [26.4%-42.9%] | 17.1% | 35.0% (123) |
| 2024Q4 | mature | 132 / 132 | 35.6% [28.0%-44.1%] | 23.5% | 36.2% (130) |
| 2025Q1 | mature | 153 / 153 | 40.5% [33.1%-48.4%] | 23.5% | 40.8% (152) |
| 2025Q2 | mature | 174 / 174 | 29.3% [23.1%-36.5%] | 18.4% | 30.1% (173) |
| 2025Q3 | mature | 193 / 193 | 23.3% [17.9%-29.8%] | 13.0% | 23.4% (192) |
| 2025Q4 | mature | 217 / 217 | 25.3% [20.0%-31.5%] | 14.7% | 23.0% (200) |
| 2026Q1 | partial | 26 / 227 | 7.7% [2.1%-24.1%] | 0.0% | 17.6% (187) |
| 2026Q2 | immature | 0 / 268 | n/a | n/a | 21.1% (123) |
| 2026Q3 | immature | 0 / 245 | n/a | n/a | 56.5% (23) |

- **The 2024-Q3 to 2025-Q1 vintages are the worst mature cohorts:** 34.1% [26.4%-42.9%], 35.6% [28.0%-44.1%] and 40.5% [33.1%-48.4%] economically bad by the horizon, against 19.0% [10.9%-30.9%] for 2024-Q1 and 23.3% [17.9%-29.8%] / 25.3% [20.0%-31.5%] for 2025-Q3 / 2025-Q4 (T1). 2024-Q2 (32.4% [24.1%-41.9%]) already sits close to the deteriorated band, and 2025-Q2 (29.3% [23.1%-36.5%]) is in between: the deterioration ramps in and out rather than switching on for exactly three quarters.
- The naive view understates the same deterioration: T1 naive is 17.1% / 23.5% / 23.5% for those quarters (D18), so a third to a half of the economic bad deals in them are distressed refinances that the trackers report as paid off.
- Immature or partial: 2026Q1, 2026Q2, 2026Q3. Their closed-deals-only rates are biased upward because only fast failures and quick renewals have closed yet: 2026Q3 shows 56.5% economic bad on 23 closed deals (P4), which says nothing about the vintage's eventual quality.
- 12 vintages are mature. 2023 vintages are small (tens of deals), so their intervals are wide.

### Vintage curves (P7)

Cumulative cash multiple (R2/R1 of each relationship's cash ledger) by months since first funding, one curve per first-funding quarter, censored at the as-of date (figure `vintage_curves.png`; dotted tails are cohorts only partly observed that long).

| First-funding quarter | Month 6 | Month 12 |
|---|---:|---:|
| 2023Q1 | 0.96 | 1.20 |
| 2023Q2 | 0.76 | 1.09 |
| 2023Q3 | 0.88 | 1.12 |
| 2023Q4 | 0.78 | 0.99 |
| 2024Q1 | 0.85 | 1.05 |
| 2024Q2 | 0.81 | 1.04 |
| 2024Q3 | 0.81 | 1.01 |
| 2024Q4 | 0.82 | 1.06 |
| 2025Q1 | 0.86 | 1.06 |
| 2025Q2 | 0.83 | 1.05 |
| 2025Q3 | 0.84 | 1.08 |
| 2025Q4 | 0.84 | n/a |
| 2026Q1 | 0.89 | n/a |

Relationships typically break even (cash multiple R3 = 1) around month 12. Curves are relationship-level, so a renewal's new funding pushes the curve down before its collections lift it.

## 5. Refinance quality

- **27.1% of renewals are distressed refinances** (220 of 811; P8, D17).
- **Resolved chains that contain a distressed refinance end in loss 40.4% (95% CI 30.9%-50.8%)** (36 of 89 chains; P8, R9), vs **18.9% (95% CI 16.2%-22.1%)** for resolved chains without one, and 21.4% for all 770 resolved relationships (P5). A distressed refinance roughly doubles the chance the relationship loses money.
- Resolved relationships as a whole return a cash multiple of 1.14 (P5, R2/R1) with a capital-weighted loss rate of 6.5% (P5, sum R8 / sum R1).
- **Modification cure rate: 32.8%** (58 of 177 modified deals end paid in full or renewed healthy; P9). Among modified deals that have closed (119) it is 48.7%: a modification is closer to a coin flip than a cure.

## 6. Segments (T1 economic bad rate, mature deals, 95% Wilson CI)

- **Positions (stacking) is the strongest origination signal:** positions = 0: 20.5% [17.4%-23.9%], n=591; positions = 1: 30.2% [26.0%-34.8%], n=417; positions = 2: 46.9% [39.1%-55.0%], n=147; positions = 3+: 63.2% [47.3%-76.6%], n=38 (T1). The intervals of 0 and 2+ positions do not overlap.
- **Industry:** highest Construction 38.6% [31.2%-46.5%], n=153, lowest Medical/Dental 16.5% [10.1%-25.8%], n=85 (T1, industries with at least 50 mature deals).
- **ISO:** highest Keystone Referral Group 38.1% [28.4%-48.8%], n=84, lowest Hudson Bridge Capital Advisors 20.9% [14.4%-29.2%], n=115 (T1). Broker quality differs, but with about 100 mature deals per ISO the intervals overlap widely; treat it as a watch list, not a verdict.
- **Burden** (daily equivalent x 21 / monthly revenue, §5): highest <15% 37.2% [32.3%-42.4%], n=344, lowest 20-25% 24.0% [19.0%-29.7%], n=242 (T1). Burden does not rank risk the way one would expect here: the lowest-burden bucket is the worst. An exploratory split by renewal status puts the excess in renewals (distressed refinances are written at a lower payment, the D17 payment ratio), while among new deals the burden buckets are roughly flat. Part 6 should model burden with renewal status alongside it.
- Full tables: `build/figures/data/segments.csv`; figure `segments.png`.

## Caveats

- **Synthetic data.** The book is generated (`scripts/generate_portfolio.py`, seed 11) with a known data-generating process. The point is the method and the definitions, not the levels.
- **The economic bad rate is a lower bound against the simulator's own labels.** D17 is a rule on observable stress (returns, pace, modification). Measured against the simulator's distressed labels it has high precision and recall of about 0.89 by design (E6; PROGRESS.md Part 4), so some refinances the simulator calls distressed look healthy on the ledger. METRICS.md §4 quotes the simulator's 29.3% closed-deal economic bad rate; the pipeline's D17 gives the figure in section 2.
- **Tracker-missing deals make R1 a lower bound.** 44 deals have no tracker row (Q5), so their commission is unknown and D6 / R1 leave it out. Relationship M0219 (METRICS.md §7) is the example: from the spreadsheets alone its cash multiple is 0.80 and its realized loss $6,648 (R3, R8), against 0.73 and $10,072 with the full contract terms (§7): the commission on deal 1102 is invisible, so the pipeline's loss is understated.
- Recent vintages are immature on T1 and biased on the closed-deals view; the vintage curves are censored at the as-of date. Segment rates use only mature deals, so they describe 2023 to early-2026 originations.
