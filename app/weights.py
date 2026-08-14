"""Turns a source's measured track record into its vote weight.

Every weight in this app used to be a number somebody typed: indicators
got 0.8-1.2, candlestick patterns got 0.10-0.32 (derived from invented
"prior win rates" like 0.52 or 0.75), and the two scales were then summed
against each other. One SMA crossover therefore outvoted ten doji
reversals by construction, and a source's real hit rate never entered
into it.

Now there is one rule for every source: until it has a track record it
weighs the same as everything else, and after that it weighs what it has
earned. The estimate is shrunk toward 50% by PRIOR_STRENGTH pseudo-trades
(a Beta(k/2, k/2) posterior mean), which is what keeps a 3-for-4 start
from being mistaken for an edge.
"""

# Pseudo-trades of "no edge" mixed into every estimate. At n=30 a source
# is trusted about half as much as its raw rate suggests; by n=200 the
# shrinkage barely matters. Sized so a lucky streak can't promote a
# source on its own.
PRIOR_STRENGTH = 30

# What an unmeasured source weighs — the same as every other unmeasured
# source, which is the whole point.
BASE_WEIGHT = 0.5

# How hard a measured edge moves the weight. At K=6 a solidly measured
# 60% source ends up near 1.0 and a 40% one bottoms out at the floor.
EDGE_SCALE = 6.0

# Never zero: a source with no voice would stop appearing on signals,
# stop being graded, and so could never recover from a bad run. That
# absorbing trap is exactly what the previous suppression rule created.
MIN_WEIGHT = 0.05
MAX_WEIGHT = 2.0


def shrunk_rate(wins: int, losses: int) -> float:
    """Hit rate pulled toward 0.5 by PRIOR_STRENGTH pseudo-trades."""
    n = wins + losses
    return (wins + PRIOR_STRENGTH * 0.5) / (n + PRIOR_STRENGTH)


def weight_for(wins: int, losses: int) -> float:
    """The vote weight a source with this record has earned."""
    rate = shrunk_rate(wins, losses)
    weight = BASE_WEIGHT + (rate - 0.5) * EDGE_SCALE
    return max(MIN_WEIGHT, min(MAX_WEIGHT, weight))
