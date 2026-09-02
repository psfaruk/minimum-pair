import asyncio
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from app.config import DB_PATH

logger = logging.getLogger(__name__)

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
                -- 'confirmed' = passed every quality gate; 'fallback' =
                -- the per-candle-guarantee filler (no quality gate),
                -- honestly tiered so it's never presented as an equal.
                -- Some pre-2026-08-29 rows may carry the old tier name
                -- 'noise' for the same fallback concept.
                tier TEXT NOT NULL DEFAULT 'confirmed',
                -- JSON array of EVERY vote fired on this candle:
                -- [{"source": ..., "direction": "CALL"|"PUT"}, ...].
                -- The evaluator grades all of them — the outvoted minority
                -- included — so the weight layer learns from every source
                -- on every candle, not only from the ones that happened to
                -- agree with the majority.
                all_sources TEXT,
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
        # 2026-09 (confluence v3) — per-confluence-strategy record. A
        # "signature" names an exact strategy the engine composed:
        # direction | regime | agreeing families. Learning THIS — instead
        # of only per-source averages — is what lets the engine fire only
        # the confluences that have actually paid on this pair.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_stats (
                signature TEXT NOT NULL,
                pair TEXT NOT NULL,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (signature, pair)
            )
            """
        )


def _rebuild_pattern_stats(conn: sqlite3.Connection) -> None:
    """Recomputes every per-source tally from the signals table.

    pattern_stats is a running total, so it can't be corrected in place
    once the rules that produce it change — it has to be recounted from
    the graded signals themselves. The recount also drops duplicate
    signals: the feed race used to fire 2-4 signals for the same
    (pair, entry_ts), and each duplicate graded the same sources twice
    for the same trade — inflating their sample counts with copies of
    themselves. Only the first signal per entry minute is kept, because
    it is the one fired at the boundary, off the real candle.

    Since all_sources exists, every vote on a signal is graded — the
    outvoted minority included, each on its OWN direction. Legacy rows
    (written before the column) carry only the contributing sources in
    `source`; they keep the old, majority-side accounting.
    """
    conn.execute("DELETE FROM pattern_stats")
    rows = conn.execute(
        """
        SELECT pair, source, all_sources, direction, result FROM (
            SELECT pair, entry_ts, source, all_sources, direction, result,
                   ROW_NUMBER() OVER (PARTITION BY pair, entry_ts ORDER BY id) AS rn
            FROM signals
            WHERE result IN ('WIN', 'LOSS')
        )
        WHERE rn = 1
        """
    ).fetchall()
    tally: dict[tuple[str, str], list[int]] = {}
    for r in rows:
        # outcome_direction: the direction that actually paid. A WIN on a
        # CALL signal means CALL was right; a LOSS means PUT was.
        if r["result"] == "WIN":
            outcome = r["direction"]
        else:
            outcome = "PUT" if r["direction"] == "CALL" else "CALL"

        votes: list[tuple[str, str]] = []
        if r["all_sources"]:
            try:
                parsed = json.loads(r["all_sources"])
                votes = [(v["source"], v["direction"]) for v in parsed if v.get("source")]
            except (ValueError, TypeError, KeyError):
                votes = []
        if not votes:
            votes = [(s, r["direction"]) for s in (r["source"] or "").split(",") if s]

        for source, direction in votes:
            entry = tally.setdefault((source, r["pair"]), [0, 0])
            entry[0 if direction == outcome else 1] += 1
    conn.executemany(
        "INSERT INTO pattern_stats (pattern, pair, wins, losses) VALUES (?, ?, ?, ?)",
        [(source, pair, w, l) for (source, pair), (w, l) in tally.items()],
    )


def _rebuild_signal_stats(conn: sqlite3.Connection) -> None:
    """Recomputes every per-confluence tally from the signals table.

    Only rows that carry a signature (confluence v3 and later) count;
    legacy rows had no signature and there is no honest way to
    reconstruct one. Draws are excluded (stake refunded — no evidence),
    and the table is deduped by (pair, entry_ts) exactly like
    _rebuild_pattern_stats.
    """
    conn.execute("DELETE FROM signal_stats")
    rows = conn.execute(
        """
        SELECT pair, signature, direction, result FROM (
            SELECT pair, entry_ts, signature, direction, result,
                   ROW_NUMBER() OVER (PARTITION BY pair, entry_ts ORDER BY id) AS rn
            FROM signals
            WHERE result IN ('WIN', 'LOSS') AND signature IS NOT NULL
        )
        WHERE rn = 1
        """
    ).fetchall()
    tally: dict[tuple[str, str], list[int]] = {}
    for r in rows:
        entry = tally.setdefault((r["signature"], r["pair"]), [0, 0])
        entry[0 if r["result"] == "WIN" else 1] += 1
    conn.executemany(
        "INSERT INTO signal_stats (signature, pair, wins, losses) VALUES (?, ?, ?, ?)",
        [(sig, pair, w, l) for (sig, pair), (w, l) in tally.items()],
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
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'signals'").fetchone()
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
                    all_sources TEXT,
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

        # 2026-08 — all_sources: every vote on a signal is persisted and
        # graded (see _rebuild_pattern_stats). Older databases get the
        # column added in place; existing rows keep NULL and are graded
        # the legacy majority-side way.
        signal_cols = {r["name"] for r in conn.execute("PRAGMA table_info(signals)")}
        if signal_cols and "all_sources" not in signal_cols:
            conn.execute("ALTER TABLE signals ADD COLUMN all_sources TEXT")

        # 2026-09 (confluence v3) — each signal carries the stable name of
        # the confluence-strategy that produced it (signature) and the
        # agreeing families (families, JSON list). Legacy rows stay NULL;
        # they simply never contribute to signature learning.
        if signal_cols and "signature" not in signal_cols:
            conn.execute("ALTER TABLE signals ADD COLUMN signature TEXT")
        if signal_cols and "families" not in signal_cols:
            conn.execute("ALTER TABLE signals ADD COLUMN families TEXT")

        # Hard integrity guarantee for history accuracy: one signal per
        # (pair, entry minute), enforced by the database itself — the
        # application-level signal_exists() check has always been racing
        # a finalize/restart replay. Older databases are deduped first
        # (the feed race fired 2-4 copies of the same entry minute),
        # keeping the FIRST row per (pair, entry_ts) — the one fired at
        # the boundary off the real candle — then the unique index is
        # created so a duplicate can never be inserted again.
        deduped_v4 = conn.execute(
            "SELECT 1 FROM app_state WHERE key = 'signals_deduped_v4'"
        ).fetchone()
        if not deduped_v4:
            conn.execute(
                """
                DELETE FROM signals
                WHERE id NOT IN (
                    SELECT MIN(id) FROM signals GROUP BY pair, entry_ts
                )
                """
            )
            conn.execute(
                """
                INSERT INTO app_state (key, value, updated_at)
                VALUES ('signals_deduped_v4', '1', ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at
                """,
                (int(time.time()),),
            )
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_pair_entry_ts ON signals(pair, entry_ts)"
            )
        except sqlite3.IntegrityError:
            logger.warning(
                "Could not create the unique (pair, entry_ts) index — "
                "duplicate signals still present; history accuracy degraded"
            )

        # signal_stats is a running total like pattern_stats: rebuilt once
        # when the rule lands, maintained incrementally by the evaluator
        # afterwards. The rebuild itself runs AFTER the tie/synthetic
        # repairs below (see the pattern_stats rebuild block) so repaired
        # rows are counted with their final grade.

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

        # Repair the leftovers of the duplicate-signal feed race: a
        # WIN/LOSS graded against a synthetic outcome candle is
        # impossible by the evaluator's own rules (a synthetic outcome
        # gets a grace window and then grades DRAW), so these rows were
        # graded against a real candle whose row the race later
        # overwrote. The real outcome is gone; the only honest grade
        # left is DRAW.
        regraded = conn.execute(
            """
            UPDATE signals SET result = 'DRAW', close_price = NULL
            WHERE result IN ('WIN', 'LOSS')
              AND id IN (
                SELECT s.id FROM signals s
                JOIN candles cd ON cd.pair = s.pair AND cd.ts = s.entry_ts
                WHERE cd.synthetic = 1
              )
            """
        ).rowcount

        # pattern_stats has to be rebuilt whenever a grading repair lands,
        # and once per database to drop the duplicate signals the feed
        # race graded twice. The app_state flag keeps the one-time
        # recount from running on every boot — the table is maintained
        # incrementally by the evaluator after that.
        # v3 (2026-08): grading rules changed again — every persisted
        # vote is now graded on its own direction (all_sources), not just
        # the majority side. One recount aligns the table with the new
        # rules; afterwards the evaluator maintains it incrementally.
        deduped = conn.execute(
            "SELECT 1 FROM app_state WHERE key = 'pattern_stats_deduped_v2'"
        ).fetchone()
        allvotes_rebuilt = conn.execute(
            "SELECT 1 FROM app_state WHERE key = 'pattern_stats_allvotes_v3'"
        ).fetchone()
        if repaired or regraded or not deduped or not allvotes_rebuilt:
            _rebuild_pattern_stats(conn)
            now = int(time.time())
            conn.execute(
                """
                INSERT INTO app_state (key, value, updated_at)
                VALUES ('pattern_stats_deduped_v2', '1', ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO app_state (key, value, updated_at)
                VALUES ('pattern_stats_allvotes_v3', '1', ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at
                """,
                (now,),
            )
        if regraded:
            logger.info("Re-graded %d signals whose outcome candle never had real data -> DRAW", regraded)
        if repaired:
            logger.info("Re-graded %d flat-tie signals -> DRAW", repaired)

        # signal_stats rebuild — deliberately AFTER the tie and synthetic
        # repairs above, so every repaired row is tallied with its final
        # grade.
        signal_stats_built = conn.execute(
            "SELECT 1 FROM app_state WHERE key = 'signal_stats_built_v4'"
        ).fetchone()
        if not signal_stats_built:
            _rebuild_signal_stats(conn)
            conn.execute(
                """
                INSERT INTO app_state (key, value, updated_at)
                VALUES ('signal_stats_built_v4', '1', ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at
                """,
                (int(time.time()),),
            )

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
    all_sources: str | None,
    signature: str | None,
    families: str | None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO signals
                (pair, direction, created_at, entry_ts, target_close_ts,
                 confidence, source, entry_price, tier, all_sources,
                 signature, families)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                all_sources,
                signature,
                families,
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
    all_sources: list[dict[str, str]] | None = None,
    signature: str | None = None,
    families: list[str] | None = None,
) -> int:
    """`all_sources` carries EVERY vote fired on the signal candle — both
    the contributing majority AND the outvoted minority, each with its own
    direction. The evaluator grades all of them, so a source that keeps
    voting the wrong side finally accumulates losses and loses weight,
    instead of being invisible to the learning loop whenever the crowd
    outvoted it.

    `signature` names the exact confluence-strategy that produced the
    signal (direction|regime|families) and `families` is the sorted list
    of agreeing families — the evaluator learns both per pair, which is
    what lets the engine fire only proven confluences.
    """
    import json as _json

    payload = _json.dumps(all_sources, ensure_ascii=False) if all_sources else None
    families_payload = _json.dumps(families, ensure_ascii=False) if families else None
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
            payload,
            signature,
            families_payload,
        )


def _signal_exists_sync(pair: str, entry_ts: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM signals WHERE pair = ? AND entry_ts = ? LIMIT 1",
            (pair, entry_ts),
        ).fetchone()
    return row is not None


async def signal_exists(pair: str, entry_ts: int) -> bool:
    """True if a signal already fires for this pair's entry minute. The
    feed checks this before inserting so one pair can never get two
    signals for the same candle (the duplicate-signal race)."""
    return await asyncio.to_thread(_signal_exists_sync, pair, entry_ts)


def _update_pending_entry_price_sync(
    pair: str, entry_ts: int, old_price: float, new_price: float
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE signals SET entry_price = ?
            WHERE pair = ? AND entry_ts = ? AND result = 'PENDING' AND entry_price = ?
            """,
            (new_price, pair, entry_ts, old_price),
        )
        return cur.rowcount


async def update_pending_entry_price(
    pair: str, entry_ts: int, old_price: float, new_price: float
) -> int:
    """Re-prices PENDING signals whose entry minute began with a close
    that a late tick later corrected (see feed._flush_late_updates).
    Only PENDING rows are touched — a graded result stands."""
    async with _write_lock:
        return await asyncio.to_thread(
            _update_pending_entry_price_sync, pair, entry_ts, old_price, new_price
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


def _grade_signal_sync(signal_id: int, close_price: float, result: str) -> bool:
    """Transitions a PENDING signal to its final grade. Returns False if
    the row was already graded (or vanished) — the caller must then NOT
    count its stats again, or a single trade would be tallied twice and
    every win rate it touches would be quietly wrong."""
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE signals SET close_price = ?, result = ?
            WHERE id = ? AND result = 'PENDING'
            """,
            (close_price, result, signal_id),
        )
        return cur.rowcount > 0


async def grade_signal(signal_id: int, close_price: float, result: str) -> bool:
    async with _write_lock:
        return await asyncio.to_thread(_grade_signal_sync, signal_id, close_price, result)


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


def _bump_signal_stat_sync(signature: str, pair: str, won: bool) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO signal_stats (signature, pair, wins, losses)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(signature, pair) DO UPDATE SET
                wins = wins + excluded.wins,
                losses = losses + excluded.losses
            """,
            (signature, pair, 1 if won else 0, 0 if won else 1),
        )


async def bump_signal_stat(signature: str, pair: str, won: bool) -> None:
    async with _write_lock:
        await asyncio.to_thread(_bump_signal_stat_sync, signature, pair, won)


def _signal_stats_sync(signature: str, pair: str) -> tuple[int, int]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT wins, losses FROM signal_stats WHERE signature = ? AND pair = ?",
            (signature, pair),
        ).fetchone()
    return (row["wins"], row["losses"]) if row else (0, 0)


async def signal_stats(signature: str, pair: str) -> tuple[int, int]:
    return await asyncio.to_thread(_signal_stats_sync, signature, pair)


def _all_signal_stats_sync() -> dict[tuple[str, str], tuple[int, int]]:
    with _connect() as conn:
        rows = conn.execute("SELECT signature, pair, wins, losses FROM signal_stats").fetchall()
    return {(r["signature"], r["pair"]): (r["wins"], r["losses"]) for r in rows}


async def all_signal_stats() -> dict[tuple[str, str], tuple[int, int]]:
    """Every confluence-strategy's record, keyed by (signature, pair)."""
    return await asyncio.to_thread(_all_signal_stats_sync)


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


# ---------------------------------------------------------------------------
# 2026-09 — direction/tier-aware win-rate analytics.
#
# The user requirement: "প্রত্যেক পেয়ার ও সিগন্যাল হিস্টোরি উইন রেট আমি
# দেখতে পারবো। Call ও put কোনো সিগন্যাল গুলো কেমন win রেট দিচ্ছে। সেই
# গুলো আলাদা আলাদা করে নিজের মতো করে দেখতে পারবো।" — per pair, per
# direction (CALL/PUT), per tier (confirmed/fallback), over a selectable
# window, all in one query the UI can slice however it wants.
# ---------------------------------------------------------------------------

_WIN_RATE_EXT_SQL = """
    SELECT pair,
           SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) AS wins,
           SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) AS losses,
           SUM(CASE WHEN result = 'DRAW' THEN 1 ELSE 0 END) AS draws,
           SUM(CASE WHEN result = 'PENDING' THEN 1 ELSE 0 END) AS pending,
           SUM(CASE WHEN result = 'WIN' AND direction = 'CALL' THEN 1 ELSE 0 END) AS call_wins,
           SUM(CASE WHEN result = 'LOSS' AND direction = 'CALL' THEN 1 ELSE 0 END) AS call_losses,
           SUM(CASE WHEN result = 'DRAW' AND direction = 'CALL' THEN 1 ELSE 0 END) AS call_draws,
           SUM(CASE WHEN result = 'PENDING' AND direction = 'CALL' THEN 1 ELSE 0 END) AS call_pending,
           SUM(CASE WHEN result = 'WIN' AND direction = 'PUT' THEN 1 ELSE 0 END) AS put_wins,
           SUM(CASE WHEN result = 'LOSS' AND direction = 'PUT' THEN 1 ELSE 0 END) AS put_losses,
           SUM(CASE WHEN result = 'DRAW' AND direction = 'PUT' THEN 1 ELSE 0 END) AS put_draws,
           SUM(CASE WHEN result = 'PENDING' AND direction = 'PUT' THEN 1 ELSE 0 END) AS put_pending,
           SUM(CASE WHEN result = 'WIN' AND tier = 'confirmed' THEN 1 ELSE 0 END) AS confirmed_wins,
           SUM(CASE WHEN result = 'LOSS' AND tier = 'confirmed' THEN 1 ELSE 0 END) AS confirmed_losses,
           SUM(CASE WHEN result = 'PENDING' AND tier = 'confirmed' THEN 1 ELSE 0 END) AS confirmed_pending,
           SUM(CASE WHEN result = 'WIN' AND tier IN ('fallback', 'noise') THEN 1 ELSE 0 END) AS fallback_wins,
           SUM(CASE WHEN result = 'LOSS' AND tier IN ('fallback', 'noise') THEN 1 ELSE 0 END) AS fallback_losses,
           SUM(CASE WHEN result = 'PENDING' AND tier IN ('fallback', 'noise') THEN 1 ELSE 0 END) AS fallback_pending
    FROM signals{where}
    GROUP BY pair
"""


def _rate(wins: int, losses: int) -> float | None:
    total = wins + losses
    return (wins / total) if total else None


def _win_rate_ext_sync(pair: str | None, days: int) -> list[dict[str, Any]]:
    clauses, params = [], []
    if pair:
        clauses.append("pair = ?")
        params.append(pair)
    if days and days > 0:
        clauses.append("created_at >= ?")
        params.append(int(time.time()) - days * 86400)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _connect() as conn:
        rows = conn.execute(_WIN_RATE_EXT_SQL.format(where=where), params).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "pair": r["pair"],
                "wins": r["wins"] or 0,
                "losses": r["losses"] or 0,
                "draws": r["draws"] or 0,
                "pending": r["pending"] or 0,
                "win_rate": _rate(r["wins"] or 0, r["losses"] or 0),
                "call": {
                    "wins": r["call_wins"] or 0,
                    "losses": r["call_losses"] or 0,
                    "draws": r["call_draws"] or 0,
                    "pending": r["call_pending"] or 0,
                    "win_rate": _rate(r["call_wins"] or 0, r["call_losses"] or 0),
                },
                "put": {
                    "wins": r["put_wins"] or 0,
                    "losses": r["put_losses"] or 0,
                    "draws": r["put_draws"] or 0,
                    "pending": r["put_pending"] or 0,
                    "win_rate": _rate(r["put_wins"] or 0, r["put_losses"] or 0),
                },
                "confirmed": {
                    "wins": r["confirmed_wins"] or 0,
                    "losses": r["confirmed_losses"] or 0,
                    "pending": r["confirmed_pending"] or 0,
                    "win_rate": _rate(r["confirmed_wins"] or 0, r["confirmed_losses"] or 0),
                },
                "fallback": {
                    "wins": r["fallback_wins"] or 0,
                    "losses": r["fallback_losses"] or 0,
                    "pending": r["fallback_pending"] or 0,
                    "win_rate": _rate(r["fallback_wins"] or 0, r["fallback_losses"] or 0),
                },
            }
        )
    return out


async def win_rate_ext(pair: str | None = None, days: int = 0) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_win_rate_ext_sync, pair, days)


def _summary_sync(days: int) -> dict[str, Any]:
    clauses, params = [], []
    if days and days > 0:
        clauses.append("created_at >= ?")
        params.append(int(time.time()) - days * 86400)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN result = 'DRAW' THEN 1 ELSE 0 END) AS draws,
                SUM(CASE WHEN result = 'PENDING' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN result = 'WIN' AND direction = 'CALL' THEN 1 ELSE 0 END) AS call_wins,
                SUM(CASE WHEN result = 'LOSS' AND direction = 'CALL' THEN 1 ELSE 0 END) AS call_losses,
                SUM(CASE WHEN result = 'WIN' AND direction = 'PUT' THEN 1 ELSE 0 END) AS put_wins,
                SUM(CASE WHEN result = 'LOSS' AND direction = 'PUT' THEN 1 ELSE 0 END) AS put_losses,
                SUM(CASE WHEN result = 'WIN' AND tier = 'confirmed' THEN 1 ELSE 0 END) AS confirmed_wins,
                SUM(CASE WHEN result = 'LOSS' AND tier = 'confirmed' THEN 1 ELSE 0 END) AS confirmed_losses,
                SUM(CASE WHEN result = 'WIN' AND tier IN ('fallback', 'noise') THEN 1 ELSE 0 END) AS fallback_wins,
                SUM(CASE WHEN result = 'LOSS' AND tier IN ('fallback', 'noise') THEN 1 ELSE 0 END) AS fallback_losses
            FROM signals{w}
            """.format(w=where),
            params,
        ).fetchone()
    wins, losses = row["wins"] or 0, row["losses"] or 0
    return {
        "days": days,
        "wins": wins,
        "losses": losses,
        "draws": row["draws"] or 0,
        "pending": row["pending"] or 0,
        "total": wins + losses + (row["draws"] or 0) + (row["pending"] or 0),
        "win_rate": _rate(wins, losses),
        "call": {"wins": row["call_wins"] or 0, "losses": row["call_losses"] or 0,
                 "win_rate": _rate(row["call_wins"] or 0, row["call_losses"] or 0)},
        "put": {"wins": row["put_wins"] or 0, "losses": row["put_losses"] or 0,
                "win_rate": _rate(row["put_wins"] or 0, row["put_losses"] or 0)},
        "confirmed": {"wins": row["confirmed_wins"] or 0, "losses": row["confirmed_losses"] or 0,
                      "win_rate": _rate(row["confirmed_wins"] or 0, row["confirmed_losses"] or 0)},
        "fallback": {"wins": row["fallback_wins"] or 0, "losses": row["fallback_losses"] or 0,
                     "win_rate": _rate(row["fallback_wins"] or 0, row["fallback_losses"] or 0)},
    }


async def summary(days: int = 0) -> dict[str, Any]:
    return await asyncio.to_thread(_summary_sync, days)


def _history_sync(
    pair: str | None,
    limit: int,
    direction: str | None = None,
    tier: str | None = None,
    result: str | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if pair:
        clauses.append("pair = ?")
        params.append(pair)
    if direction in ("CALL", "PUT"):
        clauses.append("direction = ?")
        params.append(direction)
    if tier:
        if tier in ("fallback", "noise"):
            clauses.append("tier IN ('fallback', 'noise')")
        else:
            clauses.append("tier = ?")
            params.append(tier)
    if result in ("WIN", "LOSS", "DRAW", "PENDING"):
        clauses.append("result = ?")
        params.append(result)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM signals{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, max(0, offset)),
        ).fetchall()
    return [dict(r) for r in rows]


async def history(
    pair: str | None = None,
    limit: int = 200,
    direction: str | None = None,
    tier: str | None = None,
    result: str | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(
        _history_sync, pair, limit, direction, tier, result, offset
    )


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
