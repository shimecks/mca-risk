"""Interval statistics for the analytics figures. Statistics on metric counts, not metric definitions."""

from __future__ import annotations

import math

Z95 = 1.959963984540054  # two-sided 95% normal quantile


def wilson_ci(k: int, n: int, z: float = Z95) -> tuple[float | None, float | None]:
    """Wilson score interval for a binomial proportion k / n.

    n = 0 gives (None, None) (no information). k = 0 gives a lower bound of exactly 0 and k = n an
    upper bound of exactly 1. Raises on k < 0, n < 0 or k > n."""
    if n < 0 or k < 0 or k > n:
        raise ValueError(f"need 0 <= k <= n, got k={k}, n={n}")
    if n == 0:
        return None, None
    p = k / n
    z2 = z * z
    den = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / den
    lo = 0.0 if k == 0 else max(0.0, centre - half)
    hi = 1.0 if k == n else min(1.0, centre + half)
    return lo, hi
