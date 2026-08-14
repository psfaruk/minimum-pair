"""Small-sample statistics shared by the miner and the decision engine.

Both have to answer the same question — "is this hit rate real, or is it
what a coin flip looks like at this sample size?" — and both were getting
it wrong in opposite directions: the miner had the rigor, the decision
engine used a bare threshold that killed fair sources 30% of the time.
"""
import math

Z_95 = 1.96


def wilson_interval(wins: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """95% Wilson score interval for a hit rate.

    Preferred over the normal approximation because it stays sane at the
    sample sizes this app actually has (n of 10-50) and near 0 or 1.
    """
    if n <= 0:
        return 0.0, 1.0
    phat = wins / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def wilson_lower_bound(wins: int, n: int, z: float = Z_95) -> float:
    return wilson_interval(wins, n, z)[0]


def wilson_upper_bound(wins: int, n: int, z: float = Z_95) -> float:
    return wilson_interval(wins, n, z)[1]
