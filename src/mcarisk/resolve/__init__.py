"""Stage 3: entity resolution and linkage (METRICS.md §1 L1/L3, §6 Q1-Q5).

Modules:
  names        name normalization + pair scoring (Q1)
  merchants    deal clustering into merchants, alias index for name-only records (Q1)
  renewals     renewal links: explicit field -> notes regex -> L1 inference (Q2)
  payoffs      internal vs cash payoff classification (L1)
  misposting   reassignment of debits posted to a paid-off prior deal (L3)
  matching     scorecard <-> deal (Q3), collections <-> deal (Q4)
  completeness tracker-missing deals reconstructed from ACH (Q5)
  stage        orchestration: ``python -m mcarisk.resolve`` (``make resolve``)
"""
