#!/usr/bin/env python3
"""
browser_sync.py — Incremental Browser History Synchronizer to ClickHouse

Extracts visits from Zen Browser (places.sqlite) and Chrome/Chromium (History),
tracks cursor in ~/.local/state/brain/browser_history_cursor.json,
and streams them in non-blocking batches to ClickHouse signoz_logs.web_history.

Zero performance overhead:
- Read-only SQLite (?immutable=1)
- Micro-batched inserts with backoff sleep to protect CPU and IOPS
- Idempotent deduplication via ClickHouse ReplacingMergeTree
"""

import os
import sys
import time
import json
import socket
import sqlite3
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone
import urllib.request
import urllib.parse

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("browser-sync")

HOSTNAME = socket.gethostname()
CLICKHOUSE_URL = os.getenv("CLICKHOUSE_URL", "http://localhost:8123").rstrip("/")
CH_TABLE = os.getenv("CLICKHOUSE_TABLE", "brain_events.web_history")
CH_USER = os.getenv("CLICKHOUSE_USER", "brain_ingest")
CH_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "")

STATE_DIR = Path.home() / ".local" / "state" / "brain"
CURSOR_FILE = STATE_DIR / "browser_history_cursor.json"

BATCH_SIZE = int(os.getenv("BATCH_SIZE", "1500"))
BATCH_PAUSE_SEC = float(os.getenv("BATCH_PAUSE_SEC", "0.1"))

# Mapping Firefox/Zen visit_type
ZEN_TRANSITIONS = {
    1: "link",
    2: "typed",
    3: "bookmark",
    4: "embed",
    5: "redirect_perm",
    6: "redirect_temp",
    7: "download",
    8: "framed"
}

# Mapping Chromium/Chrome transition bitmask
def parse_chrome_transition(val: int) -> str:
    core = (val or 0) & 0xFF
    mapping = {
        0: "link",
        1: "typed",
        2: "auto_bookmark",
        3: "auto_subframe",
        4: "manual_subframe",
        5: "generated",
        6: "start_page",
        7: "form_submit",
        8: "reload",
        9: "keyword",
        10: "keyword_generated"
    }
    return mapping.get(core, "other")


def load_cursor() -> dict:
    if CURSOR_FILE.exists():
        try:
            with open(CURSOR_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not read cursor file {CURSOR_FILE}: {e}")
    return {}


def save_cursor(data: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CURSOR_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(CURSOR_FILE)


def send_to_clickhouse(records: list) -> bool:
    if not records:
        return True

    lines = [json.dumps(r) for r in records]
    payload = "\n".join(lines).encode("utf-8")
    query = f"INSERT INTO {CH_TABLE} FORMAT JSONEachRow"
    url = f"{CLICKHOUSE_URL}/?query={urllib.parse.quote(query)}"

    headers = {"Content-Type": "application/json", "User-Agent": "BrainBrowserSync/1.0"}
    if CH_USER:
        headers["X-ClickHouse-User"] = CH_USER
    if CH_PASSWORD:
        headers["X-ClickHouse-Key"] = CH_PASSWORD

    req = urllib.request.Request(
        url,
        data=payload,
        headers=headers,
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            if resp.status == 200:
                return True
            logger.critical(f"ClickHouse returned unexpected status: {resp.status}")
            raise RuntimeError(f"ClickHouse HTTP status {resp.status}")
    except Exception as e:
        logger.critical(f"❌ KRYTYCZNY BŁĄD WYSYŁKI DO CLICKHOUSE ({CLICKHOUSE_URL}): {e}", exc_info=True)
        raise RuntimeError(f"KRYTYCZNA AWARIA: Połączenie z ClickHouse ({CLICKHOUSE_URL}) zerwane: {e}") from e


def find_browser_dbs() -> dict:
    dbs = {}
    home = Path.home()

    # 1. Zen Browser
    zen_dirs = [
        home / "Library" / "Application Support" / "zen" / "Profiles",
        home / ".zen"
    ]
    for zen_dir in zen_dirs:
        if zen_dir.is_dir():
            for p in zen_dir.iterdir():
                if p.is_dir() and (p / "places.sqlite").exists():
                    name = f"zen_{p.name}"
                    # Prefer release or default profile as canonical "zen"
                    if "release" in p.name.lower() or "default" in p.name.lower():
                        name = "zen"
                    dbs[name] = {"type": "zen", "path": p / "places.sqlite"}

    # 2. Google Chrome
    chrome_history_candidates = [
        home / "Library" / "Application Support" / "Google" / "Chrome" / "Default" / "History",
        home / ".config" / "google-chrome" / "Default" / "History"
    ]
    for ch_path in chrome_history_candidates:
        if ch_path.exists():
            dbs["chrome"] = {"type": "chrome", "path": ch_path}
            break

    # 3. Chromium
    chromium_history_candidates = [
        home / "Library" / "Application Support" / "Chromium" / "Default" / "History",
        home / ".config" / "chromium" / "Default" / "History"
    ]
    for cr_path in chromium_history_candidates:
        if cr_path.exists():
            dbs["chromium"] = {"type": "chrome", "path": cr_path}
            break

    return dbs


def sync_zen_history(key: str, db_path: Path, cursor: dict, dry_run: bool = False, max_records: int = None) -> int:
    last_id = cursor.get(key, 0)
    uri = f"file:{db_path}?immutable=1"

    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
    except Exception as e:
        logger.error(f"Cannot open SQLite {db_path}: {e}")
        return 0

    total_synced = 0
    current_cursor = last_id

    while True:
        limit = BATCH_SIZE
        if max_records and (total_synced + limit > max_records):
            limit = max_records - total_synced
        if limit <= 0:
            break

        query = """
            SELECT 
                v.id AS visit_id,
                v.from_visit,
                v.visit_type,
                v.visit_date,
                p.url,
                p.title
            FROM moz_historyvisits v
            JOIN moz_places p ON v.place_id = p.id
            WHERE v.id > ?
            ORDER BY v.id ASC
            LIMIT ?
        """
        rows = conn.execute(query, (current_cursor, limit)).fetchall()
        if not rows:
            break

        batch = []
        max_batch_id = current_cursor

        for r in rows:
            v_id = r["visit_id"]
            if v_id > max_batch_id:
                max_batch_id = v_id

            v_date = r["visit_date"] or 0
            if v_date > 0:
                dt = datetime.fromtimestamp(v_date / 1_000_000, tz=timezone.utc)
                ts_str = dt.strftime("%Y-%m-%d %H:%M:%S.%f")
            else:
                ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

            raw_url = r["url"] or ""
            domain = urllib.parse.urlparse(raw_url).netloc.lower()
            if ":" in domain:
                domain = domain.split(":")[0]

            v_type_str = ZEN_TRANSITIONS.get(r["visit_type"], "other")

            batch.append({
                "timestamp": ts_str,
                "source": "zen",
                "url": raw_url,
                "domain": domain,
                "title": (r["title"] or "").strip(),
                "visit_type": v_type_str,
                "visit_duration_ms": 0,
                "from_visit_id": int(r["from_visit"] or 0),
                "visit_id": int(v_id),
                "host": HOSTNAME
            })

        if not dry_run:
            send_to_clickhouse(batch)
            cursor[key] = max_batch_id
            save_cursor(cursor)

        current_cursor = max_batch_id
        total_synced += len(rows)

        if len(rows) >= BATCH_SIZE and BATCH_PAUSE_SEC > 0:
            time.sleep(BATCH_PAUSE_SEC)

    conn.close()
    return total_synced


def sync_chrome_history(key: str, db_path: Path, cursor: dict, dry_run: bool = False, max_records: int = None) -> int:
    last_id = cursor.get(key, 0)
    uri = f"file:{db_path}?immutable=1"

    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
    except Exception as e:
        logger.error(f"Cannot open SQLite {db_path}: {e}")
        return 0

    total_synced = 0
    current_cursor = last_id

    while True:
        limit = BATCH_SIZE
        if max_records and (total_synced + limit > max_records):
            limit = max_records - total_synced
        if limit <= 0:
            break

        query = """
            SELECT 
                v.id AS visit_id,
                v.from_visit,
                v.visit_time,
                v.visit_duration,
                v.transition,
                u.url,
                u.title
            FROM visits v
            JOIN urls u ON v.url = u.id
            WHERE v.id > ?
            ORDER BY v.id ASC
            LIMIT ?
        """
        rows = conn.execute(query, (current_cursor, limit)).fetchall()
        if not rows:
            break

        batch = []
        max_batch_id = current_cursor

        for r in rows:
            v_id = r["visit_id"]
            if v_id > max_batch_id:
                max_batch_id = v_id

            v_time = r["visit_time"] or 0
            if v_time > 0:
                epoch_sec = (v_time - 11644473600000000) / 1_000_000
                dt = datetime.fromtimestamp(max(0, epoch_sec), tz=timezone.utc)
                ts_str = dt.strftime("%Y-%m-%d %H:%M:%S.%f")
            else:
                ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

            raw_url = r["url"] or ""
            domain = urllib.parse.urlparse(raw_url).netloc.lower()
            if ":" in domain:
                domain = domain.split(":")[0]

            v_type_str = parse_chrome_transition(r["transition"])
            dur_ms = int((r["visit_duration"] or 0) / 1000)

            batch.append({
                "timestamp": ts_str,
                "source": "chrome" if "chrome" in key else "chromium",
                "url": raw_url,
                "domain": domain,
                "title": (r["title"] or "").strip(),
                "visit_type": v_type_str,
                "visit_duration_ms": dur_ms,
                "from_visit_id": int(r["from_visit"] or 0),
                "visit_id": int(v_id),
                "host": HOSTNAME
            })

        if not dry_run:
            send_to_clickhouse(batch)
            cursor[key] = max_batch_id
            save_cursor(cursor)

        current_cursor = max_batch_id
        total_synced += len(rows)

        if len(rows) >= BATCH_SIZE and BATCH_PAUSE_SEC > 0:
            time.sleep(BATCH_PAUSE_SEC)

    conn.close()
    return total_synced


def run_sync_cycle(dry_run: bool = False, max_records: int = None) -> int:
    dbs = find_browser_dbs()
    cursor = load_cursor()
    total_cycle = 0

    for key, info in dbs.items():
        if info["type"] == "zen":
            synced = sync_zen_history(key, info["path"], cursor, dry_run=dry_run, max_records=max_records)
        else:
            synced = sync_chrome_history(key, info["path"], cursor, dry_run=dry_run, max_records=max_records)

        if synced > 0:
            logger.info(f"[{key}] Synced {synced} visit records to ClickHouse.")
            total_cycle += synced

    if total_cycle > 0 and not dry_run:
        cursor["last_sync_utc"] = datetime.now(timezone.utc).isoformat()
        save_cursor(cursor)

    return total_cycle


def main():
    parser = argparse.ArgumentParser(description="Synchronize Browser History (Zen / Chrome) to ClickHouse.")
    parser.add_argument("--daemon", action="store_true", help="Run continuously in background polling every 60s")
    parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds (default: 60)")
    parser.add_argument("--dry-run", action="store_true", help="Scan and parse without inserting to ClickHouse")
    parser.add_argument("--limit", type=int, default=None, help="Maximum records to sync per browser source")
    args = parser.parse_args()

    logger.info(f"Starting Browser History Synchronizer (target: {CLICKHOUSE_URL} -> {CH_TABLE})")

    if args.daemon:
        logger.info(f"Running in daemon mode (interval: {args.interval}s)")
        while True:
            try:
                run_sync_cycle(dry_run=args.dry_run, max_records=args.limit)
            except Exception as e:
                logger.error(f"Cycle failed: {e}")
            time.sleep(args.interval)
    else:
        synced = run_sync_cycle(dry_run=args.dry_run, max_records=args.limit)
        logger.info(f"Sync complete. Total new records pushed: {synced}")


if __name__ == "__main__":
    main()
