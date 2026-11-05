# Data-quality report (Parts 2-4: standardize, resolve, ledger + metrics)

Rules from METRICS.md §1 (L1-L3) and §6 (Q1-Q7), plus S* cross-field checks added by standardization.
Rules owned by later parts are listed as pending. Source: DuckDB table `data_quality`.

| Rule | What | Status | Hits | Of | Detail |
|---|---|---|---:|---:|---|
| Q1 | Merchant key (entity resolution) | evaluated | 1,200 | 2,011 | 2011 deals -> 1200 merchants; merges {'exact': 811}; deal attach {'seed': 1200, 'exact': 811}; 9098 aliases; 0 review exceptions |
| Q2 | Renewal link | evaluated | 811 | 2,011 | renewal links by method {'explicit': 634, 'inferred': 133, 'regex': 44}; 0 review exceptions |
| Q3 | Scorecard <-> deal | evaluated | 496 | 496 | 496/496 scorecards linked to a deal (same merchant, app date 0-10 days before funding, closest); 0 review exceptions |
| Q4 | Collections <-> deal | evaluated | 725 | 726 | collections rows linked by {'deal_number': 552, 'latest_deal': 1, 'open_deal': 172, 'unmatched': 1}; 3 review exceptions |
| Q5 | Completeness: every deal with ACH activity has a tracker row | evaluated | 44 | 2,011 | 44 deals with ACH activity and no tracker row reconstructed from ACH (flag tracker_missing; contract fields NULL) |
| Q6 | Stale tracker status vs derived outcome | evaluated | 364 | 1,967 | 364 of 1967 tracker statuses disagree with the §2.1 outcome (as of the metrics as-of date); 194 of 791 renewed deals are typed paid off. Top pairs: paid_off->renewed_healthy: 137, active->paid_in_full: 93, paid_off->refinanced_distressed: 57, active->renewed_healthy: 37, active->written_off: 17, active->refinanced_distressed: 9, active->settled: 5, defaulted->written_off: 2 |
| Q7 | Accounting: every raw row loaded, rejected with a reason, or repaired with a log entry | evaluated | 412 | 171,770 | raw_tracker_2023: 114 raw = 114 loaded + 0 rejected (7 loaded rows repaired/derived/nulled); raw_tracker_2024: 405 raw = 405 loaded + 0 rejected (405 loaded rows repaired/derived/nulled); raw_tracker_2025: 1448 raw = 1448 loaded + 0 rejected (0 loaded rows repaired/derived/nulled); raw_ach: 168581 raw = 152941 loaded + 0 rejected + 15640 L2 duplicates (0 loaded rows repaired/derived/nulled); raw_scorecards: 496 raw = 496 loaded + 0 rejected (0 loaded rows repaired/derived/nulled); raw_collections: 726 raw = 726 loaded + 0 rejected (0 loaded rows repaired/derived/nulled) |
| L1 | Internal vs cash payoff | evaluated | 811 | 836 | PAYOFF rows: {'internal_payoff': 811, 'early_payoff_cash': 25}; by method {'funding_formula': 111, 'inferred_tracker_missing': 22, 'no_successor': 25, 'tracker_link': 678}; description hint disagrees on 0; tracker-missing policy 'infer' |
| L2 | ACH dedupe after date normalization | evaluated | 15,640 | 168,581 | 15640 overlap duplicates dropped; 125 of them pair an MM/DD/YYYY row with a YYYY-MM-DD row (July 2024 export switch) and would survive a dedupe on the raw date text (125 rows would be double counted) |
| L3 | Misposted payments | evaluated | 227 |  | 227 DEBIT rows reassigned from the paid-off prior deal to its active renewal (misposting_repairs); 0 debits after a payoff left as posted (review) |
| S1 | deals_std: RTR != advance x factor (+-1 cent) | evaluated | 0 | 1,967 | consistency check: counted, values unchanged |
| S2 | deals_std: payment != RTR / term installment | evaluated | 0 | 1,967 | consistency check: counted, values unchanged |
| S3 | deals_std: funding date outside the tracker's year | evaluated | 0 | 1,967 | consistency check: counted, values unchanged |
| S4 | deals_std: explicit prior deal unknown or not earlier | evaluated | 17 | 1,967 | consistency check: counted, values unchanged |
| S5 | deals_std: missing contract term field | evaluated | 0 | 1,967 | consistency check: counted, values unchanged |
| S6 | payments_std: amount sign unexpected for its type | evaluated | 0 | 152,941 | consistency check: counted, values unchanged |

## Row accounting (Q7)

| Source | Target | Raw rows | Loaded | Rejected | L2 duplicates | Loaded rows with a log entry |
|---|---|---:|---:|---:|---:|---:|
| `raw_tracker_2023` | `deals_std` | 114 | 114 | 0 | 0 | 7 |
| `raw_tracker_2024` | `deals_std` | 405 | 405 | 0 | 0 | 405 |
| `raw_tracker_2025` | `deals_std` | 1,448 | 1,448 | 0 | 0 | 0 |
| `raw_ach` | `payments_std` | 168,581 | 152,941 | 0 | 15,640 | 0 |
| `raw_scorecards` | `scorecards_std` | 496 | 496 | 0 | 0 | 0 |
| `raw_collections` | `collections_std` | 726 | 726 | 0 | 0 | 0 |

## Field events (`std_log`)

| Table | Field | Action | Reason | Rows |
|---|---|---|---|---:|
| `deals_std` | payment_cents | repaired | payment_whole_dollar_from_rtr_term | 7 |
| `deals_std` | term_bdays | derived | term_bdays_derived_rtr_over_payment | 405 |

## Sample rows

- **Q4**: {"src_file": "raw/collections_log.xlsx", "src_sheet": "Log", "src_row": 227, "reason": "collection_unmatched", "entity_id": "226"}; {"src_file": "raw/collections_log.xlsx", "src_sheet": "Log", "src_row": 573, "reason": "no_open_deal_on_log_date", "entity_id": "572"}; {"src_file": "raw/collections_log.xlsx", "src_sheet": "Log", "src_row": 684, "reason": "several_open_deals", "entity_id": "683"}
- **Q5**: {"src_file": "raw/ach_exports/ach_2023_03.csv", "src_sheet": "(csv)", "src_row": 11, "deal_id": 1005, "funding_date": "2023-03-01"}; {"src_file": "raw/ach_exports/ach_2023_06.csv", "src_sheet": "(csv)", "src_row": 208, "deal_id": 1024, "funding_date": "2023-06-15"}; {"src_file": "raw/ach_exports/ach_2023_11.csv", "src_sheet": "(csv)", "src_row": 634, "deal_id": 1091, "funding_date": "2023-11-16"}; {"src_file": "raw/ach_exports/ach_2023_11.csv", "src_sheet": "(csv)", "src_row": 832, "deal_id": 1096, "funding_date": "2023-11-22"}; {"src_file": "raw/ach_exports/ach_2023_11.csv", "src_sheet": "(csv)", "src_row": 1090, "deal_id": 1102, "funding_date": "2023-11-30"}
- **Q6**: {"src_file": "raw/deal_tracker_2023.xlsx", "src_sheet": "Funded", "src_row": 6, "deal_id": 1003, "status_raw": "Paid Off", "outcome": "renewed_healthy"}; {"src_file": "raw/deal_tracker_2023.xlsx", "src_sheet": "Funded", "src_row": 12, "deal_id": 1010, "status_raw": "Paid Off", "outcome": "renewed_healthy"}; {"src_file": "raw/deal_tracker_2023.xlsx", "src_sheet": "Funded", "src_row": 20, "deal_id": 1018, "status_raw": "Paid Off", "outcome": "renewed_healthy"}; {"src_file": "raw/deal_tracker_2023.xlsx", "src_sheet": "Funded", "src_row": 22, "deal_id": 1020, "status_raw": "Paid Off", "outcome": "renewed_healthy"}; {"src_file": "raw/deal_tracker_2023.xlsx", "src_sheet": "Funded", "src_row": 31, "deal_id": 1030, "status_raw": "active", "outcome": "paid_in_full"}
- **Q7**: {"src_file": "raw/deal_tracker_2023.xlsx", "src_sheet": "Funded", "src_row": 9, "field": "payment_cents", "action": "repaired", "reason": "payment_whole_dollar_from_rtr_term"}; {"src_file": "raw/deal_tracker_2023.xlsx", "src_sheet": "Funded", "src_row": 18, "field": "payment_cents", "action": "repaired", "reason": "payment_whole_dollar_from_rtr_term"}; {"src_file": "raw/deal_tracker_2023.xlsx", "src_sheet": "Funded", "src_row": 33, "field": "payment_cents", "action": "repaired", "reason": "payment_whole_dollar_from_rtr_term"}; {"src_file": "raw/deal_tracker_2023.xlsx", "src_sheet": "Funded", "src_row": 36, "field": "payment_cents", "action": "repaired", "reason": "payment_whole_dollar_from_rtr_term"}; {"src_file": "raw/deal_tracker_2023.xlsx", "src_sheet": "Funded", "src_row": 41, "field": "payment_cents", "action": "repaired", "reason": "payment_whole_dollar_from_rtr_term"}
- **L1**: {"src_file": "raw/ach_exports/ach_2024_02.csv", "src_sheet": "(csv)", "src_row": 780, "reason": "l1_successor_tracker_missing_inferred_internal", "entity_id": "7443"}; {"src_file": "raw/ach_exports/ach_2024_06.csv", "src_sheet": "(csv)", "src_row": 1544, "reason": "l1_successor_tracker_missing_inferred_internal", "entity_id": "15000"}; {"src_file": "raw/ach_exports/ach_2024_06.csv", "src_sheet": "(csv)", "src_row": 1699, "reason": "l1_successor_tracker_missing_inferred_internal", "entity_id": "15156"}; {"src_file": "raw/ach_exports/ach_2024_08.csv", "src_sheet": "(csv)", "src_row": 1649, "reason": "l1_successor_tracker_missing_inferred_internal", "entity_id": "19945"}; {"src_file": "raw/ach_exports/ach_2024_10.csv", "src_sheet": "(csv)", "src_row": 1299, "reason": "l1_successor_tracker_missing_inferred_internal", "entity_id": "25474"}
- **L2**: {"src_file": "raw/ach_exports/ach_2024_07.csv", "src_sheet": "(csv)", "src_row": 2, "kept_src_file": "raw/ach_exports/ach_2024_06.csv", "kept_src_row": 2330, "date_text_raw": "2024-06-28"}; {"src_file": "raw/ach_exports/ach_2024_07.csv", "src_sheet": "(csv)", "src_row": 3, "kept_src_file": "raw/ach_exports/ach_2024_06.csv", "kept_src_row": 2331, "date_text_raw": "2024-06-28"}; {"src_file": "raw/ach_exports/ach_2024_07.csv", "src_sheet": "(csv)", "src_row": 4, "kept_src_file": "raw/ach_exports/ach_2024_06.csv", "kept_src_row": 2332, "date_text_raw": "2024-06-28"}; {"src_file": "raw/ach_exports/ach_2024_07.csv", "src_sheet": "(csv)", "src_row": 5, "kept_src_file": "raw/ach_exports/ach_2024_06.csv", "kept_src_row": 2333, "date_text_raw": "2024-06-28"}; {"src_file": "raw/ach_exports/ach_2024_07.csv", "src_sheet": "(csv)", "src_row": 6, "kept_src_file": "raw/ach_exports/ach_2024_06.csv", "kept_src_row": 2334, "date_text_raw": "2024-06-28"}
- **L3**: {"src_file": "raw/ach_exports/ach_2023_05.csv", "src_sheet": "(csv)", "src_row": 82, "payment_id": 424, "from": 1002, "to": 1017}; {"src_file": "raw/ach_exports/ach_2023_07.csv", "src_sheet": "(csv)", "src_row": 436, "payment_id": 1393, "from": 1028, "to": 1036}; {"src_file": "raw/ach_exports/ach_2023_07.csv", "src_sheet": "(csv)", "src_row": 459, "payment_id": 1414, "from": 1028, "to": 1036}; {"src_file": "raw/ach_exports/ach_2023_07.csv", "src_sheet": "(csv)", "src_row": 480, "payment_id": 1436, "from": 1028, "to": 1036}; {"src_file": "raw/ach_exports/ach_2023_08.csv", "src_sheet": "(csv)", "src_row": 368, "payment_id": 1766, "from": 1011, "to": 1045}
- **S4**: {"src_file": "raw/deal_tracker_2024.xlsx", "src_sheet": "2024 Deals", "src_row": 31, "detail": "deal 1150: prior deal 1102 has no tracker row"}; {"src_file": "raw/deal_tracker_2024.xlsx", "src_sheet": "2024 Deals", "src_row": 73, "detail": "deal 1193: prior deal 1091 has no tracker row"}; {"src_file": "raw/deal_tracker_2024.xlsx", "src_sheet": "2024 Deals", "src_row": 199, "detail": "deal 1322: prior deal 1188 has no tracker row"}; {"src_file": "raw/deal_tracker_2024.xlsx", "src_sheet": "2024 Deals", "src_row": 209, "detail": "deal 1332: prior deal 1265 has no tracker row"}; {"src_file": "raw/deal_tracker_2024.xlsx", "src_sheet": "2024 Deals", "src_row": 330, "detail": "deal 1455: prior deal 1307 has no tracker row"}

## Resolution, ledger and metrics tables (Parts 3-4)

| Table | Rows |
|---|---:|
| `deals_resolved` | 2,011 |
| `merchants` | 1,200 |
| `merchant_aliases` | 9,098 |
| `merchant_merges` | 811 |
| `deal_links` | 811 |
| `payoff_classes` | 836 |
| `payments_resolved` | 152,941 |
| `misposting_repairs` | 227 |
| `scorecard_links` | 496 |
| `collections_links` | 726 |
| `notes_extractions` | 1,452 |
| `collections_terms` | 726 |
| `exceptions` | 25 |
| `ledger` | 155,067 |
| `deal_status_events` | 391 |
| `ledger_rejects` | 1 |
| `deal_metrics` | 2,011 |
| `relationship_metrics` | 1,200 |
| `portfolio_metrics` | 466 |
| `deal_attributes` | 2,011 |
| `deal_targets` | 2,011 |
| `target_metrics` | 102 |

## Ledger accounting (Part 4)

| Source | Rows | Ledger | Status events | Rejected |
|---|---:|---:|---:|---:|
| `payments_resolved` | 152,941 | 152,941 | 0 | 0 |
| `deals_resolved (commission_out)` | 2,011 | 1,792 | 0 | 0 |
| `collections_terms` | 726 | 334 | 391 | 1 |

## Derived outcomes (§2.1)

| Outcome | Deals |
|---|---:|
| renewed_healthy | 591 |
| paid_in_full | 524 |
| active | 325 |
| refinanced_distressed | 220 |
| written_off | 160 |
| defaulted | 66 |
| settled | 61 |
| active_modified | 39 |
| paid_in_full_early | 25 |

## Review queue (`exceptions`)

| Rule | Reason | Rows |
|---|---|---:|
| L1 | l1_successor_tracker_missing_inferred_internal | 22 |
| Q4 | collection_unmatched | 1 |
| Q4 | no_open_deal_on_log_date | 1 |
| Q4 | several_open_deals | 1 |

Notes extraction (collections_terms by extractor): llm: 726
