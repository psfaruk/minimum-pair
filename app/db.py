import asyncio
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from app.config import DB_PATH

_write_lock = asyncio.Lock()

# DB_PATH can point at a mounted volume (e.g. Railway's /data) whose
# parent directory exists on the filesystem but was never created *by
# this process* — sqlite3.connect() doesn't create missing directories,
# it only fails with the unhelpful "unable to open database file".
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_sync() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS candles (
                pair TEXT NOT NULL,
                ts INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                -- 1 when the feed invented this candle because the minute
                -- passed with no ticks. It is a placeholder, not market
                -- data: nothing may be signalled or graded on it.
                synthetic INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (pair, ts)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair TEXT NOT NULL,
                direction TEXT NOT NULL CHECK (direction IN ('CALL', 'PUT')),
                created_at INTEGER NOT NULL,
                entry_ts INTEGER NOT NULL,
                target_close_ts INTEGER NOT NULL,
                -- NULL until this signal's sources have enough graded
                -- history to state a real number. An invented prior is
                -- worse than no number at all.
                confidence REAL,
                source TEXT NOT NULL,
                entry_price REAL,
                close_price REAL,
                -- 'confirmed' = the gated path; 'noise' = the ALWAYS_SIGNAL
                -- fallback, which passes no quality gate and must never be
                -- presented as an equal.
                tier TEXT NOT NULL DEFAULT 'confirmed',
                -- DRAW: price finished exactly where it started. The broker
                -- refunds these, so scoring them as losses (as this app used
                -- to) both understates the win rate and poisons learning.
                result TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (result IN ('PENDING', 'WIN', 'LOSS', 'DRAW'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_signals_pair ON signals(pair)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_signals_result ON signals(result)"
        )
        # Small key/value store for state that has to outlive the
        # process. The login backoff lives here for a specific reason:
        # Railway restarts this container on every crash and redeploy, so
        # an in-memory backoff counter resets to zero exactly when the
        # app is at its most likely to hammer the Cloudflare-guarded
        # login page. Persisted, a boot inherits the wait it was already
        # serving. The captured session token lives here too, so the
        # token survives a restart even when Railway's API isn't
        # configured (given a mounted volume for DB_PATH).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pattern_stats (
                pattern TEXT NOT NULL,
                pair TEXT NOT NULL,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (pattern, pair)
            )
            """
        )


def _rebuild_pattern_stats(conn: sqlite3.Connection) -> None:
    """Recomputes every per-source tally from the signals table.

    pattern_stats is a running total, so it can't be corrected in place
    once the rules that produced it change — it has to be recounted from
    the graded signals themselves.
    """
    conn.execute("DELETE FROM pattern_stats")
    rows = conn.execute(
        "SELECT pair, source, result FROM signals WHERE result IN ('WIN', 'LOSS')"
    ).fetchall()
    tally: dict[tuple[str, str], list[int]] = {}
    for r in rows:
        for source in (r["source"] or "").split(","):
            if not source:
                continue
            entry = tally.setdefault((source, r["pair"]), [0, 0])
            entry[0 if r["result"] == "WIN" else 1] += 1
    conn.executemany(
        "INSERT INTO pattern_stats (pattern, pair, wins, losses) VALUES (?, ?, ?, ?)",
        [(source, pair, w, l) for (source, pair), (w, l) in tally.items()],
    )


def _migrate_sync() -> None:
    """Brings an existing database up to the current schema.

    Deployments carry a volume, so the old rows are real history worth
    repairing rather than discarding — in particular every signal that
    was scored a loss purely because price finished where it started.
    """
    with _connect() as conn:
        candle_cols = {r["name"] for r in conn.execute("PRAGMA table_info(candles)")}
        if "synthetic" not in candle_cols:
            conn.execute("ALTER TABLE candles ADD COLUMN synthetic INTEGER NOT NULL DEFAULT 0")
            # Flat candles in existing history are almost certainly the
            # feed's own fabrications; a real minute that opens, ranges
            # and closes at one identical price is vanishingly rare.
            conn.execute("UPDATE candles SET synthetic = 1 WHERE high = low")

        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'signals'"
        ).fetchone()
        if schema and "DRAW" not in (schema["sql"] or ""):
            conn.execute("DROP INDEX IF EXISTS idx_signals_pair")
            conn.execute("DROP INDEX IF EXISTS idx_signals_result")
            conn.execute("ALTER TABLE signals RENAME TO signals_legacy")
            conn.execute(
                """
                CREATE TABLE signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK (direction IN ('CALL', 'PUT')),
                    created_at INTEGER NOT NULL,
                    entry_ts INTEGER NOT NULL,
                    target_close_ts INTEGER NOT NULL,
                    confidence REAL,
                    source TEXT NOT NULL,
                    entry_price REAL,
                    close_price REAL,
                    tier TEXT NOT NULL DEFAULT 'confirmed',
                    result TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (result IN ('PENDING', 'WIN', 'LOSS', 'DRAW'))
                )
                """
            )
            conn.execute(
                """
                INSERT INTO signals
                    (id, pair, direction, created_at, entry_ts, target_close_ts,
                     confidence, source, entry_price, close_price, result)
                SELECT id, pair, direction, created_at, entry_ts, target_close_ts,
                       confidence, source, entry_price, close_price, result
                FROM signals_legacy
                """
            )
            conn.execute("DROP TABLE signals_legacy")

        # Repair the ties, whether they came from the legacy table or from
        # a newer database graded before this rule landed.
        repaired = conn.execute(
            """
            UPDATE signals SET result = 'DRAW'
            WHERE result IN ('WIN', 'LOSS')
              AND close_price IS NOT NULL
              AND entry_price IS NOT NULL
              AND close_price = entry_price
            """
        ).rowcount
        if repaired:
            _rebuild_pattern_stats(conn)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_pair ON signals(pair)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_result ON signals(result)")


async def init_db() -> None:
    await asyncio.to_thread(_init_sync)
    await asyncio.to_thread(_migrate_sync)


def _get_state_sync(keys: tuple[str, ...]) -> dict[str, str]:
    with _connect() as conn:
        placeholders = ",".join("?" for _ in keys)
        rows = conn.execute(
            f"SELECT key, value FROM app_state WHERE key IN ({placeholders})", keys
        ).fetchall()
    return {r["key"]: r["value"] for r in rows}


async def get_state(*keys: str) -> dict[str, str]:
    """Reads persisted key/value state. Missing keys are simply absent."""
    if not keys:
        return {}
    return await asyncio.to_thread(_get_state_sync, keys)


def _set_state_sync(values: dict[str, str]) -> None:
    now = int(time.time())
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            [(k, v, now) for k, v in values.items()],
        )


async def set_state(values: dict[str, str]) -> None:
    """Writes persisted key/value state. Written as one transaction so a
    restart never sees half of a backoff update."""
    if not values:
        return
    async with _write_lock:
        await asyncio.to_thread(_set_state_sync, values)


def _insert_candle_sync(
    pair: str, ts: int, o: float, h: float, l: float, c: float, synthetic: bool
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO candles (pair, ts, open, high, low, close, synthetic)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pair, ts) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
                -- real ticks arriving late for a minute the feed had to
                -- invent turn that placeholder into genuine data — open
                -- has to be corrected too, or a backfilled real candle
                -- keeps the fabricated open and reports a fake body/wick
                synthetic=excluded.synthetic
            """,
            (pair, ts, o, h, l, c, 1 if synthetic else 0),
        )


async def save_candle(
    pair: str, ts: int, o: float, h: float, l: float, c: float, synthetic: bool = False
) -> None:
    async with _write_lock:
        await asyncio.to_thread(_insert_candle_sync, pair, ts, o, h, l, c, synthetic)


def _recent_candles_sync(pair: str, limit: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, open, high, low, close, synthetic FROM candles "
            "WHERE pair = ? ORDER BY ts DESC LIMIT ?",
            (pair, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


async def recent_candles(pair: str, limit: int = 300) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_recent_candles_sync, pair, limit)


def _get_candle_sync(pair: str, ts: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT ts, open, high, low, close, synthetic FROM candles WHERE pair = ? AND ts = ?",
            (pair, ts),
        ).fetchone()
    return dict(row) if row else None


async def get_candle(pair: str, ts: int) -> dict[str, Any] | None:
    return await asyncio.to_thread(_get_candle_sync, pair, ts)


def _insert_signal_sync(
    pair: str,
    direction: str,
    entry_ts: int,
    target_close_ts: int,
    confidence: float | None,
    source: str,
    entry_price: float | None,
    tier: str,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO signals
                (pair, direction, created_at, entry_ts, target_close_ts,
                 confidence, source, entry_price, tier)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pair,
                direction,
                int(time.time()),
                entry_ts,
                target_close_ts,
                confidence,
                source,
                entry_price,
                tier,
            ),
        )
        return cur.lastrowid


async def insert_signal(
    pair: str,
    direction: str,
    entry_ts: int,
    target_close_ts: int,
    confidence: float | None,
    source: str,
    entry_price: float | None,
    tier: str = "confirmed",
) -> int:
    async with _write_lock:
        return await asyncio.to_thread(
            _insert_signal_sync,
            pair,
            direction,
            entry_ts,
            target_close_ts,
            confidence,
            source,
            entry_price,
            tier,
        )


def _pending_signals_due_sync(now_ts: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE result = 'PENDING' AND target_close_ts <= ?",
            (now_ts,),
        ).fetchall()
    return [dict(r) for r in rows]


async def pending_signals_due(now_ts: int) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_pending_signals_due_sync, now_ts)


def _grade_signal_sync(signal_id: int, close_price: float, result: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE signals SET close_price = ?, result = ? WHERE id = ?",
            (close_price, result, signal_id),
        )


async def grade_signal(signal_id: int, close_price: float, result: str) -> None:
    async with _write_lock:
        await asyncio.to_thread(_grade_signal_sync, signal_id, close_price, result)


def _bump_pattern_stat_sync(pattern: str, pair: str, won: bool) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO pattern_stats (pattern, pair, wins, losses)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(pattern, pair) DO UPDATE SET
                wins = wins + excluded.wins,
                losses = losses + excluded.losses
            """,
            (pattern, pair, 1 if won else 0, 0 if won else 1),
        )


async def bump_pattern_stat(pattern: str, pair: str, won: bool) -> None:
    async with _write_lock:
        await asyncio.to_thread(_bump_pattern_stat_sync, pattern, pair, won)


def _pattern_stats_sync(pattern: str, pair: str) -> tuple[int, int]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT wins, losses FROM pattern_stats WHERE pattern = ? AND pair = ?",
            (pattern, pair),
        ).fetchone()
    return (row["wins"], row["losses"]) if row else (0, 0)


async def pattern_stats(pattern: str, pair: str) -> tuple[int, int]:
    return await asyncio.to_thread(_pattern_stats_sync, pattern, pair)


def _all_pattern_stats_sync() -> dict[tuple[str, str], tuple[int, int]]:
    with _connect() as conn:
        rows = conn.execute("SELECT pattern, pair, wins, losses FROM pattern_stats").fetchall()
    return {(r["pattern"], r["pair"]): (r["wins"], r["losses"]) for r in rows}


async def all_pattern_stats() -> dict[tuple[str, str], tuple[int, int]]:
    """Every source's record, keyed by (source, pair). One query, because
    the decision engine needs the whole table on every candle to weight
    its votes."""
    return await asyncio.to_thread(_all_pattern_stats_sync)


def _pattern_performance_sync(pair: str | None) -> list[dict[str, Any]]:
    # pattern_stats only updates when a signal is *graded*, so on its own
    # it undercounts "how many signals has this source fired" while any
    # are still PENDING. Scan the signals table directly (source is a
    # comma-joined string per row) to get an accurate fired-count
    # including pending ones, and use that for wins/losses/total too so
    # the numbers are self-consistent.
    with _connect() as conn:
        if pair:
            rows = conn.execute("SELECT source, result FROM signals WHERE pair = ?", (pair,)).fetchall()
        else:
            rows = conn.execute("SELECT source, result FROM signals").fetchall()

    counts: dict[str, dict[str, int]] = {}
    for r in rows:
        for pattern in (r["source"] or "").split(","):
            if not pattern:
                continue
            c = counts.setdefault(pattern, {"wins": 0, "losses": 0, "draws": 0, "pending": 0})
            if r["result"] == "WIN":
                c["wins"] += 1
            elif r["result"] == "LOSS":
                c["losses"] += 1
            elif r["result"] == "DRAW":
                c["draws"] += 1
            else:
                c["pending"] += 1

    result = []
    for pattern, c in counts.items():
        # Draws are excluded from the denominator: the stake comes back,
        # so they are neither a hit nor a miss for this source.
        graded = c["wins"] + c["losses"]
        result.append(
            {
                "pattern": pattern,
                "wins": c["wins"],
                "losses": c["losses"],
                "draws": c["draws"],
                "pending": c["pending"],
                "total": graded + c["draws"] + c["pending"],
                "win_rate": (c["wins"] / graded) if graded else None,
            }
        )
    result.sort(key=lambda r: (r["win_rate"] is None, -(r["win_rate"] or 0)))
    return result


async def pattern_performance(pair: str | None = None) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_pattern_performance_sync, pair)


def _win_rate_sync(pair: str | None) -> list[dict[str, Any]]:
    with _connect() as conn:
        if pair:
            rows = conn.execute(
                """
                SELECT pair,
                       SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) AS losses,
                       SUM(CASE WHEN result = 'DRAW' THEN 1 ELSE 0 END) AS draws,
                       SUM(CASE WHEN result = 'PENDING' THEN 1 ELSE 0 END) AS pending
                FROM signals WHERE pair = ? GROUP BY pair
                """,
                (pair,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT pair,
                       SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) AS losses,
                       SUM(CASE WHEN result = 'DRAW' THEN 1 ELSE 0 END) AS draws,
                       SUM(CASE WHEN result = 'PENDING' THEN 1 ELSE 0 END) AS pending
                FROM signals GROUP BY pair
                """
            ).fetchall()
    result = []
    for r in rows:
        wins, losses = r["wins"] or 0, r["losses"] or 0
        total = wins + losses  # draws excluded: the stake is refunded, not lost
        result.append(
            {
                "pair": r["pair"],
                "wins": wins,
                "losses": losses,
                "draws": r["draws"] or 0,
                "pending": r["pending"] or 0,
                "win_rate": (wins / total) if total else None,
            }
        )
    return result


async def win_rate(pair: str | None = None) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_win_rate_sync, pair)


def _history_sync(pair: str | None, limit: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        if pair:
            rows = conn.execute(
                "SELECT * FROM signals WHERE pair = ? ORDER BY created_at DESC LIMIT ?",
                (pair, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


async def history(pair: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_history_sync, pair, limit)


def _latest_signals_sync() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT s.* FROM signals s
            INNER JOIN (
                SELECT pair, MAX(id) AS latest_id FROM signals GROUP BY pair
            ) latest ON s.pair = latest.pair AND s.id = latest.latest_id
            ORDER BY s.pair
            """
        ).fetchall()
    return [dict(r) for r in rows]


async def latest_signals() -> list[dict[str, Any]]:
    return await asyncio.to_thread(_latest_signals_sync)


def _prune_old_data_sync(candle_cutoff_ts: int, signal_cutoff_ts: int) -> tuple[int, int]:
    with _connect() as conn:
        candles_deleted = conn.execute(
            "DELETE FROM candles WHERE ts < ?", (candle_cutoff_ts,)
        ).rowcount
        # PENDING is deliberately excluded from the cutoff: a signal must
        # be graded before it's eligible for pruning, no matter its age.
        signals_deleted = conn.execute(
            """
            DELETE FROM signals
            WHERE created_at < ? AND result IN ('WIN', 'LOSS', 'DRAW')
            """,
            (signal_cutoff_ts,),
        ).rowcount
    return candles_deleted, signals_deleted


async def prune_old_data(candle_retention_days: int, signal_retention_days: int) -> tuple[int, int]:
    """Deletes candle/signal rows older than their retention window.

    pattern_stats (the aggregate weights.py learns from) is untouched —
    only the raw per-candle and per-signal log rows are pruned, so this
    never erases what a source has measurably earned."""
    now = int(time.time())
    candle_cutoff = now - candle_retention_days * 86400
    signal_cutoff = now - signal_retention_days * 86400
    async with _write_lock:
        return await asyncio.to_thread(_prune_old_data_sync, candle_cutoff, signal_cutoff)
