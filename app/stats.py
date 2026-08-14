"""Small-sample statistics shared by the miner and the decision engine.

Both have to answer the same question — "is this hit rate real, or is it
what a coin flip looks like at this sample size?" — and both were getting
it wrong in opposite directions: the miner had the rigor, the decision
engine used a bare threshold that killed fair sources 30% of the time.
"""
import math
from statistics import NormalDist

Z_95 = 1.96

# Above this, the exact binomial sum is both slow (thousands of terms of
# big-integer arithmetic) and pointless — the normal approximation with a
# continuity correction agrees to several decimals well before here.
EXACT_BINOMIAL_MAX_N = 1000


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


def binom_p_value_one_sided(wins: int, n: int, p: float = 0.5) -> float:
    """P(X >= wins) under Binomial(n, p) — the chance a coin flip would
    have done at least this well."""
    if n <= 0:
        return 1.0
    if wins <= 0:
        return 1.0
    if n <= EXACT_BINOMIAL_MAX_N:
        return sum(math.comb(n, k) * (p**k) * ((1 - p) ** (n - k)) for k in range(wins, n + 1))
    mean = n * p
    sd = math.sqrt(n * p * (1 - p))
    if sd == 0:
        return 1.0 if wins <= mean else 0.0
    return 1.0 - NormalDist(mean, sd).cdf(wins - 0.5)  # continuity correction


def fdr_pass_mask(p_values: list[float], alpha: float = 0.10) -> list[bool]:
    """Benjamini-Hochberg: pass[i] is True if hypothesis i survives FDR
    correction across everything tested in the same batch.

    Needed wherever many candidates are screened at once. Testing 28
    sources at 95% confidence produces one or two "significant" results
    on pure noise every time; without this correction a screening tool
    reports those as edges and someone trades them.
    """
    n = len(p_values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: p_values[i])
    max_rank = 0
    for rank, idx in enumerate(order, start=1):
        if p_values[idx] <= (rank / n) * alpha:
            max_rank = rank
    passed = [False] * n
    for rank, idx in enumerate(order, start=1):
        passed[idx] = rank <= max_rank
    return passed


def block_signflip_p_value(hits: list[bool], block: int = 20, iterations: int = 2000, seed: int = 7) -> float:
    """How often chance alone would score at least this well, given that
    a source's results come in clusters.

    The binomial p-value assumes every trial is independent. Signal
    sources violate that badly: a source that fires after an up-move
    tends to fire again right after it was right, so wins and losses
    arrive in runs. Treating those as independent understates the p-value
    by orders of magnitude — on data with no edge at all, it happily
    reports p=1e-05.

    Under the null the source's direction carries no information, so
    flipping its prediction is equally likely a priori. Flipping in
    contiguous blocks preserves the clustering while destroying any real
    signal, which gives a null distribution the clustering can't fake.
    """
    n = len(hits)
    if n == 0:
        return 1.0
    observed = sum(hits) / n

    import random as _random

    rng = _random.Random(seed)
    n_blocks = (n + block - 1) // block
    at_least_as_good = 0
    for _ in range(iterations):
        flips = [rng.random() < 0.5 for _ in range(n_blocks)]
        score = sum(hit != flips[i // block] for i, hit in enumerate(hits))
        if score / n >= observed:
            at_least_as_good += 1
    return at_least_as_good / iterations
