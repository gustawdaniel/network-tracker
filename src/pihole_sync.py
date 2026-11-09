#!/usr/bin/env python3
"""
pihole_sync.py — Incremental and historical DNS query synchronizer from Pi-hole to ClickHouse.

Key Architectural Guarantees:
1. Low CPU footprint: Queries in batches of 5000 with throttling pause (default 150ms).
2. Zero data loss: Exact cursor tracking by query_storage.id.
3. Fail Loudly: NO silent fails. Any database or network issue raises critical exceptions and halts.
4. Non-blocking: Connects with immutable/read-only URI to avoid SQLite WAL locking with pihole-FTL.
"""

import argparse
import datetime
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] pihole-sync: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("pihole-sync")

QUERY_TYPES = {
    1: "A",
    2: "AAAA",
    3: "ANY",
    4: "SRV",
    5: "SOA",
    6: "PTR",
    7: "TXT",
    8: "NAPTR",
    9: "MX",
    10: "DS",
    11: "RRSIG",
    12: "DNSKEY",
    13: "NS",
    14: "OTHER",
    15: "SVCB",
    16: "HTTPS",
}

STATUS_NAMES = {
    0: "UNKNOWN",
    1: "BLOCKED_GRAVITY",
    2: "FORWARDED",
    3: "CACHED",
    4: "BLOCKED_REGEX",
    5: "BLOCKED_EXACT",
    6: "BLOCKED_UPSTREAM",
    7: "UPSTREAM_ERROR",
    8: "UPSTREAM_TIMEOUT",
    9: "BLOCKED_GRAVITY_CNAME",
    10: "BLOCKED_REGEX_CNAME",
    11: "BLOCKED_EXACT_CNAME",
    12: "RETRIED",
    13: "RETRIED_IGNORED",
    14: "ALREADY_FORWARDED",
    15: "DATABASE_BUSY",
    16: "SPECIAL_DOMAIN",
    17: "CACHE_EXHAUSTED",
}


def load_cursor(cursor_path: Path) -> int:
    if not cursor_path.exists():
        return 0
    try:
        with open(cursor_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return int(data.get("last_query_id", 0))
    except Exception as e:
        logger.error(f"Failed to read cursor file {cursor_path}: {e}")
        raise RuntimeError(f"Corrupted cursor file at {cursor_path}: {e}") from e


def save_cursor(cursor_path: Path, last_query_id: int) -> None:
    try:
        cursor_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cursor_path.with_suffix(".tmp")
        payload = {
            "last_query_id": last_query_id,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        tmp_path.replace(cursor_path)
    except Exception as e:
        logger.critical(f"Failed to persist cursor to {cursor_path}: {e}")
        raise RuntimeError(f"Failed to persist cursor to {cursor_path}: {e}") from e


def send_batch_to_clickhouse(clickhouse_url: str, records: List[Dict[str, Any]]) -> None:
    if not records:
        return
    url = f"{clickhouse_url.rstrip('/')}/?query=INSERT+INTO+signoz_logs.dns_history+FORMAT+JSONEachRow"
    data_lines = [json.dumps(r, ensure_ascii=False) for r in records]
    payload = ("\n".join(data_lines) + "\n").encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/x-ndjson"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                body = resp.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"ClickHouse returned status {resp.status}: {body}")
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="replace")
        logger.critical(f"ClickHouse HTTP error during DNS history push: {e.code} - {err_msg}")
        raise RuntimeError(f"ClickHouse HTTP error: {e.code} - {err_msg}") from e
    except Exception as e:
        logger.critical(f"Network error sending DNS records to ClickHouse at {url}: {e}")
        raise RuntimeError(f"Network error communicating with ClickHouse: {e}") from e


def sync_pihole(
    db_path: Path,
    clickhouse_url: str,
    cursor_path: Path,
    batch_size: int = 5000,
    batch_pause: float = 0.15,
    max_batches: Optional[int] = None,
    server_name: str = "alarmpi",
) -> int:
    if not db_path.exists():
        raise FileNotFoundError(f"Pi-hole database not found at {db_path}")

    last_query_id = load_cursor(cursor_path)
    logger.info(f"Connecting to Pi-hole DB {db_path} (resuming from query_id={last_query_id})")

    uri = f"file:{db_path.resolve()}?immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    cursor = conn.cursor()

    total_synced = 0
    batches_done = 0

    query = """
        SELECT 
            q.id,
            q.timestamp,
            COALESCE(d.domain, '') AS domain,
            COALESCE(c.ip, '') AS client_ip,
            COALESCE(c.name, '') AS client_name,
            q.type,
            q.status,
            COALESCE(q.reply_time, 0.0) AS reply_time
        FROM query_storage q
        LEFT JOIN domain_by_id d ON q.domain = d.id
        LEFT JOIN client_by_id c ON q.client = c.id
        WHERE q.id > ?
        ORDER BY q.id ASC
        LIMIT ?
    """

    try:
        while True:
            if max_batches is not None and batches_done >= max_batches:
                logger.info(f"Reached max batches limit ({max_batches}). Halting.")
                break

            cursor.execute(query, (last_query_id, batch_size))
            rows = cursor.fetchall()
            if not rows:
                break

            records: List[Dict[str, Any]] = []
            max_id_in_batch = last_query_id

            for row in rows:
                qid, ts_sec, domain, client_ip, client_name, qtype, qstatus, reply_sec = row
                if qid > max_id_in_batch:
                    max_id_in_batch = qid

                # format timestamp to 'YYYY-MM-DD HH:MM:SS.000'
                dt = datetime.datetime.fromtimestamp(ts_sec, tz=datetime.timezone.utc)
                ts_str = dt.strftime("%Y-%m-%d %H:%M:%S.000")

                records.append({
                    "timestamp": ts_str,
                    "query_id": qid,
                    "client_ip": client_ip,
                    "client_name": client_name,
                    "domain": domain,
                    "query_type": QUERY_TYPES.get(qtype, f"TYPE_{qtype}"),
                    "status": STATUS_NAMES.get(qstatus, f"STATUS_{qstatus}"),
                    "reply_time_ms": round(float(reply_sec) * 1000.0, 3),
                    "server": server_name,
                })

            send_batch_to_clickhouse(clickhouse_url, records)
            last_query_id = max_id_in_batch
            save_cursor(cursor_path, last_query_id)

            total_synced += len(records)
            batches_done += 1

            if batches_done % 10 == 0 or len(rows) < batch_size:
                logger.info(
                    f"Progress: batch {batches_done}, synced {total_synced} queries so far (current query_id={last_query_id})"
                )

            if len(rows) < batch_size:
                break

            if batch_pause > 0:
                time.sleep(batch_pause)

    finally:
        conn.close()

    logger.info(f"Pi-hole sync finished. Pushed {total_synced} new DNS query records. Current max id: {last_query_id}")
    return total_synced


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronize Pi-hole DNS query history to ClickHouse.")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("/etc/pihole/pihole-FTL.db"),
        help="Path to pihole-FTL.db",
    )
    parser.add_argument(
        "--clickhouse-url",
        type=str,
        default=os.environ.get("CLICKHOUSE_URL", "http://localhost:8123"),
        help="ClickHouse HTTP endpoint",
    )
    parser.add_argument(
        "--cursor-file",
        type=Path,
        default=Path.home() / ".local/state/brain/pihole_cursor.json",
        help="Path to JSON file tracking synced query ID",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5000,
        help="Number of query records per batch",
    )
    parser.add_argument(
        "--batch-pause",
        type=float,
        default=0.15,
        help="Seconds to pause between batches to keep CPU low",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=None,
        help="Limit number of batches to process in this run",
    )
    parser.add_argument(
        "--server-name",
        type=str,
        default="alarmpi",
        help="Identifier for the Pi-hole instance",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuously in a loop sleeping between checks",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=120,
        help="Seconds between cycles when running with --daemon",
    )

    args = parser.parse_args()

    if args.daemon:
        logger.info(f"Starting Pi-hole sync daemon (interval={args.interval}s)")
        while True:
            try:
                sync_pihole(
                    db_path=args.db,
                    clickhouse_url=args.clickhouse_url,
                    cursor_path=args.cursor_file,
                    batch_size=args.batch_size,
                    batch_pause=args.batch_pause,
                    server_name=args.server_name,
                )
            except Exception as e:
                logger.critical(f"Daemon cycle failed: {e}")
                # Fail loudly! Re-raise in accordance with AGENTS.md rule
                raise
            time.sleep(args.interval)
    else:
        sync_pihole(
            db_path=args.db,
            clickhouse_url=args.clickhouse_url,
            cursor_path=args.cursor_file,
            batch_size=args.batch_size,
            batch_pause=args.batch_pause,
            max_batches=args.max_batches,
            server_name=args.server_name,
        )


if __name__ == "__main__":
    main()
