import asyncio
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from app.config import DB_PATH

_write_lock = asyncio.Lock()


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
                confidence REAL NOT NULL,
                source TEXT NOT NULL,
                entry_price REAL,
                close_price REAL,
                result TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (result IN ('PENDING', 'WIN', 'LOSS'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_signals_pair ON signals(pair)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_signals_result ON signals(result)"
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


async def init_db() -> None:
    await asyncio.to_thread(_init_sync)


def _insert_candle_sync(pair: str, ts: int, o: float, h: float, l: float, c: float) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO candles (pair, ts, open, high, low, close)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(pair, ts) DO UPDATE SET
                high=excluded.high, low=excluded.low, close=excluded.close
            """,
            (pair, ts, o, h, l, c),
        )


async def save_candle(pair: str, ts: int, o: float, h: float, l: float, c: float) -> None:
    async with _write_lock:
        await asyncio.to_thread(_insert_candle_sync, pair, ts, o, h, l, c)


def _recent_candles_sync(pair: str, limit: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, open, high, low, close FROM candles "
            "WHERE pair = ? ORDER BY ts DESC LIMIT ?",
            (pair, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


async def recent_candles(pair: str, limit: int = 300) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_recent_candles_sync, pair, limit)


def _get_candle_sync(pair: str, ts: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT ts, open, high, low, close FROM candles WHERE pair = ? AND ts = ?",
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
    confidence: float,
    source: str,
    entry_price: float | None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO signals
                (pair, direction, created_at, entry_ts, target_close_ts,
                 confidence, source, entry_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        return cur.lastrowid


async def insert_signal(
    pair: str,
    direction: str,
    entry_ts: int,
    target_close_ts: int,
    confidence: float,
    source: str,
    entry_price: float | None,
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
            c = counts.setdefault(pattern, {"wins": 0, "losses": 0, "pending": 0})
            if r["result"] == "WIN":
                c["wins"] += 1
            elif r["result"] == "LOSS":
                c["losses"] += 1
            else:
                c["pending"] += 1

    result = []
    for pattern, c in counts.items():
        graded = c["wins"] + c["losses"]
        result.append(
            {
                "pattern": pattern,
                "wins": c["wins"],
                "losses": c["losses"],
                "pending": c["pending"],
                "total": graded + c["pending"],
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
                       SUM(CASE WHEN result = 'PENDING' THEN 1 ELSE 0 END) AS pending
                FROM signals GROUP BY pair
                """
            ).fetchall()
    result = []
    for r in rows:
        wins, losses = r["wins"] or 0, r["losses"] or 0
        total = wins + losses
        result.append(
            {
                "pair": r["pair"],
                "wins": wins,
                "losses": losses,
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
