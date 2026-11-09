#!/usr/bin/env python3
"""
server.py — Ultra-fast, lightweight web server and API for Network Tracker (Browser History Telemetry).
Serves VictoriaLogs-inspired web interface with interactive time-series histogram and search stream.
"""

import argparse
import calendar
import datetime
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("network-tracker-web")

CLICKHOUSE_URL = os.environ.get("CLICKHOUSE_URL", "http://localhost:8123")
CLICKHOUSE_USER = os.environ.get("CLICKHOUSE_USER", "")
CLICKHOUSE_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
CLICKHOUSE_DB = os.environ.get("CLICKHOUSE_DB", "brain_events")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
INDEX_FILE = WEB_DIR / "index.html"


def escape_sql(val: str) -> str:
    """Escapes single quotes and backslashes for ClickHouse string literals."""
    return val.replace("\\", "\\\\").replace("'", "\\'")


def run_clickhouse_query(sql: str) -> Dict[str, Any]:
    """Executes SQL against ClickHouse and returns parsed JSON result with execution latency."""
    t0 = time.time()
    url = f"{CLICKHOUSE_URL.rstrip('/')}/?query=" + urllib.parse.quote(sql.strip() + " FORMAT JSON")
    req = urllib.request.Request(url, headers={"User-Agent": "BrainNetworkTracker/1.0"})
    if CLICKHOUSE_USER:
        req.add_header("X-ClickHouse-User", CLICKHOUSE_USER)
    if CLICKHOUSE_PASSWORD:
        req.add_header("X-ClickHouse-Key", CLICKHOUSE_PASSWORD)
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            took_ms = round((time.time() - t0) * 1000, 2)
            data["took_ms"] = took_ms
            return data
    except Exception as e:
        logger.error(f"ClickHouse query error: {e} | SQL: {sql[:150]}")
        raise RuntimeError(f"ClickHouse error: {e}") from e


def build_where_clause(
    q: Optional[str] = None,
    day: Optional[str] = None,
    month: Optional[str] = None,
    hour: Optional[str] = None,
    time_range: Optional[str] = None,
    source: Optional[str] = None,
    domain: Optional[str] = None,
) -> str:
    clauses = []
    today = datetime.date.today()

    if day and re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        clauses.append(f"toDate(timestamp) = '{day}'")
    elif month and re.match(r"^\d{4}-\d{2}$", month):
        clauses.append(f"toStartOfMonth(timestamp) = '{month}-01'")
    elif time_range:
        tr = time_range.lower().strip()
        if tr == "today":
            clauses.append(f"toDate(timestamp) = '{today.strftime('%Y-%m-%d')}'")
        elif tr == "7d":
            start_d = today - datetime.timedelta(days=6)
            clauses.append(f"toDate(timestamp) >= '{start_d.strftime('%Y-%m-%d')}'")
        elif tr == "30d":
            start_d = today - datetime.timedelta(days=29)
            clauses.append(f"toDate(timestamp) >= '{start_d.strftime('%Y-%m-%d')}'")
        elif tr == "90d":
            start_d = today - datetime.timedelta(days=89)
            clauses.append(f"toDate(timestamp) >= '{start_d.strftime('%Y-%m-%d')}'")
        elif tr == "1y":
            start_d = today - datetime.timedelta(days=365)
            clauses.append(f"toDate(timestamp) >= '{start_d.strftime('%Y-%m-%d')}'")
        # 'all' has no date bound

    if hour is not None and str(hour).strip() != "":
        h_val = str(hour).split(":")[0].strip()
        if h_val.isdigit() and 0 <= int(h_val) <= 23:
            clauses.append(f"toHour(timestamp) = {int(h_val)}")

    if source and source not in ("all", "*", ""):
        safe_src = escape_sql(source)
        if safe_src == "chrome":
            clauses.append("(source = 'chrome' OR source = 'google-chrome')")
        else:
            clauses.append(f"source = '{safe_src}'")

    if domain and domain.strip():
        safe_dom = escape_sql(domain.strip())
        clauses.append(f"domain = '{safe_dom}'")

    if q and q.strip():
        raw_q = q.strip()
        is_quoted = (raw_q.startswith('"') and raw_q.endswith('"')) or (raw_q.startswith("'") and raw_q.endswith("'"))
        clean_q = raw_q.strip('"\'').strip()
        terms = [t.strip() for t in clean_q.split() if t.strip()]

        if is_quoted or len(terms) > 1:
            # Multi-word or quoted: match whole phrase in title, domain, or URL (with space, +, %20, -)
            safe_phrase = escape_sql(clean_q)
            safe_plus = escape_sql("+".join(terms))
            safe_enc = escape_sql("%20".join(terms))
            safe_dash = escape_sql("-".join(terms))
            phrase_conds = [
                f"ilike(title, '%{safe_phrase}%')",
                f"ilike(url, '%{safe_phrase}%')",
                f"ilike(url, '%{safe_plus}%')",
                f"ilike(url, '%{safe_enc}%')",
                f"ilike(url, '%{safe_dash}%')",
                f"ilike(domain, '%{safe_phrase}%')"
            ]

            if not is_quoted:
                # Also allow all terms if found in clean parts (title, domain, or cutQueryStringAndFragment(url))
                term_conds = []
                for term in terms:
                    safe_term = escape_sql(term)
                    term_conds.append(
                        f"(ilike(title, '%{safe_term}%') OR ilike(domain, '%{safe_term}%') OR ilike(cutQueryStringAndFragment(url), '%{safe_term}%'))"
                    )
                phrase_conds.append(f"({' AND '.join(term_conds)})")

            clauses.append(f"({' OR '.join(phrase_conds)})")
        elif len(terms) == 1:
            # Single term
            safe_term = escape_sql(clean_q)
            if len(clean_q) <= 3:
                # Short term (<=3 chars like 'to', 'btc', 'usd', 'api'):
                # Avoid matching inside base64 hash params in url! Match in title, domain, clean path or as exact param
                clauses.append(
                    f"(ilike(title, '%{safe_term}%') OR ilike(domain, '%{safe_term}%') OR ilike(cutQueryStringAndFragment(url), '%{safe_term}%') OR match(url, '(?i)[?&=+/_.-]{safe_term}[?&=+/_.-]'))"
                )
            else:
                clauses.append(
                    f"(ilike(url, '%{safe_term}%') OR ilike(title, '%{safe_term}%') OR ilike(domain, '%{safe_term}%'))"
                )

    return ("WHERE " + " AND ".join(clauses)) if clauses else ""


class NetworkTrackerHandler(BaseHTTPRequestHandler):
    # Drops connections that open and go silent (handshake included).
    timeout = 30

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def send_json(self, status_code: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        # Support optional /network prefix if proxy didn't strip it
        if path.startswith("/network"):
            path = path[len("/network"):] or "/"

        if path in ("/", "/index.html"):
            self.serve_index()
        elif path == "/api/history":
            self.handle_history(params)
        elif path == "/api/histogram":
            self.handle_histogram(params)
        elif path == "/api/days":
            self.handle_days(params)
        elif path == "/api/stats":
            self.handle_stats()
        elif path == "/healthz":
            self.send_json(200, {"status": "ok", "service": "network-tracker-web"})
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404 Not Found")

    def serve_index(self) -> None:
        if not INDEX_FILE.exists():
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"500 Internal Error: index.html not found.")
            return

        with open(INDEX_FILE, "rb") as f:
            content = f.read()

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content)

    def handle_history(self, params: Dict[str, List[str]]) -> None:
        q = params.get("q", [""])[0]
        day = params.get("day", [""])[0]
        month = params.get("month", [""])[0]
        hour = params.get("hour", [""])[0]
        range_val = params.get("range", ["all"])[0]
        source = params.get("source", ["all"])[0]
        domain = params.get("domain", [""])[0]

        try:
            limit = min(max(int(params.get("limit", [100])[0]), 1), 300)
        except ValueError:
            limit = 100

        try:
            offset = max(int(params.get("offset", [0])[0]), 0)
        except ValueError:
            offset = 0

        where = build_where_clause(
            q=q,
            day=day,
            month=month,
            hour=hour,
            time_range=range_val,
            source=source,
            domain=domain,
        )

        # Count total matches
        count_sql = f"SELECT count() AS total FROM {CLICKHOUSE_DB}.web_history {where}"
        # Fetch rows
        rows_sql = f"""
            SELECT 
                formatDateTime(timestamp, '%Y-%m-%d %H:%i:%s') AS ts,
                toDate(timestamp) AS day,
                formatDateTime(timestamp, '%H:%i:%s') AS time,
                source,
                domain,
                title,
                url,
                visit_duration_ms,
                visit_id
            FROM {CLICKHOUSE_DB}.web_history
            {where}
            ORDER BY timestamp DESC
            LIMIT {limit} OFFSET {offset}
        """

        try:
            c_res = run_clickhouse_query(count_sql)
            total = int(c_res.get("data", [{}])[0].get("total", 0))

            r_res = run_clickhouse_query(rows_sql)
            rows = r_res.get("data", [])
            took_ms = round(c_res.get("took_ms", 0) + r_res.get("took_ms", 0), 2)

            self.send_json(200, {
                "rows": rows,
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": (offset + len(rows)) < total,
                "took_ms": took_ms
            })
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    def handle_histogram(self, params: Dict[str, List[str]]) -> None:
        q = params.get("q", [""])[0]
        day = params.get("day", [""])[0]
        month = params.get("month", [""])[0]
        range_val = params.get("range", ["all"])[0].lower().strip()
        interval = params.get("interval", ["auto"])[0].lower().strip()
        source = params.get("source", ["all"])[0]
        domain = params.get("domain", [""])[0]
        today = datetime.date.today()

        # Determine bucket mode
        if day and re.match(r"^\d{4}-\d{2}-\d{2}$", day):
            bucket_mode = "hourly"
        elif month and re.match(r"^\d{4}-\d{2}$", month):
            bucket_mode = "daily" if interval in ("auto", "daily") else interval
        elif range_val == "today":
            bucket_mode = "hourly" if interval in ("auto", "hourly") else ("6h" if interval == "6h" else "daily")
        elif range_val == "7d":
            if interval == "daily":
                bucket_mode = "daily"
            elif interval == "hourly":
                bucket_mode = "hourly"
            else:
                bucket_mode = "6h"  # auto or 6h -> 7*4=28 buckets
        elif range_val in ("30d", "90d"):
            bucket_mode = "6h" if interval == "6h" else ("daily" if interval in ("auto", "daily") else interval)
        elif range_val in ("1y", "all"):
            bucket_mode = "daily" if interval == "daily" else "monthly"
        else:
            bucket_mode = "6h" if interval == "6h" else "monthly"

        try:
            if bucket_mode == "hourly":
                target_day = day if (day and re.match(r"^\d{4}-\d{2}-\d{2}$", day)) else today.strftime("%Y-%m-%d")
                where = build_where_clause(q=q, day=target_day, source=source, domain=domain)
                sql = f"""
                    SELECT 
                        formatDateTime(toStartOfHour(timestamp), '%H:00') AS bucket,
                        count() AS cnt
                    FROM {CLICKHOUSE_DB}.web_history
                    {where}
                    GROUP BY bucket
                    ORDER BY bucket ASC
                """
                res = run_clickhouse_query(sql)
                raw_data = {r["bucket"]: int(r["cnt"]) for r in res.get("data", [])}
                took_ms = res.get("took_ms", 0)

                buckets = []
                for h in range(24):
                    b_key = f"{h:02d}:00"
                    buckets.append({
                        "bucket": b_key,
                        "count": raw_data.get(b_key, 0),
                        "label": b_key,
                        "day": target_day,
                    })

            elif bucket_mode == "6h":
                if range_val == "today":
                    start_date = today
                    end_date = today
                    where = build_where_clause(q=q, time_range="today", source=source, domain=domain)
                else:
                    start_date = today - datetime.timedelta(days=6)
                    end_date = today
                    where = build_where_clause(q=q, time_range="7d", source=source, domain=domain)

                sql = f"""
                    SELECT 
                        formatDateTime(toStartOfInterval(timestamp, INTERVAL 6 HOUR), '%Y-%m-%d %H:00') AS bucket,
                        count() AS cnt
                    FROM {CLICKHOUSE_DB}.web_history
                    {where}
                    GROUP BY bucket
                    ORDER BY bucket ASC
                """
                res = run_clickhouse_query(sql)
                raw_data = {r["bucket"]: int(r["cnt"]) for r in res.get("data", [])}
                took_ms = res.get("took_ms", 0)

                buckets = []
                curr = start_date
                while curr <= end_date:
                    d_str = curr.strftime("%Y-%m-%d")
                    for h in (0, 6, 12, 18):
                        b_key = f"{d_str} {h:02d}:00"
                        buckets.append({
                            "bucket": b_key,
                            "count": raw_data.get(b_key, 0),
                            "label": f"{h:02d}:00",
                            "day": d_str,
                            "hour": h,
                            "interval_label": f"{h:02d}:00–{h+5:02d}:59",
                            "weekday": curr.weekday(),
                        })
                    curr += datetime.timedelta(days=1)


            elif bucket_mode == "daily":
                if month and re.match(r"^\d{4}-\d{2}$", month):
                    y, m = map(int, month.split("-"))
                    _, last_d = calendar.monthrange(y, m)
                    start_date = datetime.date(y, m, 1)
                    end_date = datetime.date(y, m, last_d)
                    where = build_where_clause(q=q, month=month, source=source, domain=domain)
                elif range_val == "7d":
                    start_date = today - datetime.timedelta(days=6)
                    end_date = today
                    where = build_where_clause(q=q, time_range="7d", source=source, domain=domain)
                elif range_val == "30d":
                    start_date = today - datetime.timedelta(days=29)
                    end_date = today
                    where = build_where_clause(q=q, time_range="30d", source=source, domain=domain)
                elif range_val == "90d":
                    start_date = today - datetime.timedelta(days=89)
                    end_date = today
                    where = build_where_clause(q=q, time_range="90d", source=source, domain=domain)
                elif range_val == "1y":
                    start_date = today - datetime.timedelta(days=365)
                    end_date = today
                    where = build_where_clause(q=q, time_range="1y", source=source, domain=domain)
                else:  # 'all' or fallback
                    start_date = datetime.date(2024, 11, 1)
                    end_date = today
                    where = build_where_clause(q=q, time_range="all", source=source, domain=domain)

                sql = f"""
                    SELECT 
                        formatDateTime(toDate(timestamp), '%Y-%m-%d') AS bucket,
                        count() AS cnt
                    FROM {CLICKHOUSE_DB}.web_history
                    {where}
                    GROUP BY bucket
                    ORDER BY bucket ASC
                """
                res = run_clickhouse_query(sql)
                raw_data = {r["bucket"]: int(r["cnt"]) for r in res.get("data", [])}
                took_ms = res.get("took_ms", 0)

                buckets = []
                curr = start_date
                while curr <= end_date:
                    d_str = curr.strftime("%Y-%m-%d")
                    buckets.append({
                        "bucket": d_str,
                        "count": raw_data.get(d_str, 0),
                        "label": d_str,
                        "month": d_str[:7],
                        "weekday": curr.weekday(),
                    })
                    curr += datetime.timedelta(days=1)

            else:  # monthly
                if range_val == "1y":
                    start_d = today - datetime.timedelta(days=365)
                    start_ym = start_d.strftime("%Y-%m")
                    end_ym = today.strftime("%Y-%m")
                    where = build_where_clause(q=q, time_range="1y", source=source, domain=domain)
                elif range_val == "90d":
                    start_d = today - datetime.timedelta(days=89)
                    start_ym = start_d.strftime("%Y-%m")
                    end_ym = today.strftime("%Y-%m")
                    where = build_where_clause(q=q, time_range="90d", source=source, domain=domain)
                else:  # 'all' or fallback
                    start_ym = "2024-11"
                    end_ym = today.strftime("%Y-%m")
                    where = build_where_clause(q=q, time_range="all", source=source, domain=domain)

                sql = f"""
                    SELECT 
                        formatDateTime(toStartOfMonth(timestamp), '%Y-%m') AS bucket,
                        count() AS cnt
                    FROM {CLICKHOUSE_DB}.web_history
                    {where}
                    GROUP BY bucket
                    ORDER BY bucket ASC
                """
                res = run_clickhouse_query(sql)
                raw_data = {r["bucket"]: int(r["cnt"]) for r in res.get("data", [])}
                took_ms = res.get("took_ms", 0)

                sy, sm = map(int, start_ym.split("-"))
                ey, em = map(int, end_ym.split("-"))
                buckets = []
                cy, cm = sy, sm
                while (cy < ey) or (cy == ey and cm <= em):
                    ym_str = f"{cy:04d}-{cm:02d}"
                    buckets.append({
                        "bucket": ym_str,
                        "count": raw_data.get(ym_str, 0),
                        "label": ym_str,
                        "year": cy,
                        "month_num": cm,
                    })
                    cm += 1
                    if cm > 12:
                        cm = 1
                        cy += 1

            total_hits = sum(b["count"] for b in buckets)
            max_hit = max((b["count"] for b in buckets), default=0)

            self.send_json(200, {
                "bucket_mode": bucket_mode,
                "range": range_val,
                "day": day,
                "month": month,
                "interval": interval,
                "buckets": buckets,
                "total": total_hits,
                "max": max_hit,
                "took_ms": took_ms,
            })
        except Exception as e:
            self.send_json(500, {"error": str(e)})


    def handle_days(self, params: Dict[str, List[str]]) -> None:
        """Returns distinct days with total count and journal date info."""
        sql = f"""
            SELECT 
                toDate(timestamp) AS day,
                count() AS cnt
            FROM {CLICKHOUSE_DB}.web_history
            GROUP BY day
            ORDER BY day DESC
            LIMIT 60
        """
        try:
            res = run_clickhouse_query(sql)
            days = res.get("data", [])
            self.send_json(200, {
                "days": [{"day": str(d["day"]), "count": int(d["cnt"])} for d in days],
                "took_ms": res.get("took_ms", 0)
            })
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    def handle_stats(self) -> None:
        """Overview stats: total rows, top domains, browser distribution."""
        sql_summary = f"""
            SELECT 
                count() AS total_visits,
                countDistinct(domain) AS unique_domains,
                min(timestamp) AS oldest,
                max(timestamp) AS newest
            FROM {CLICKHOUSE_DB}.web_history
        """
        sql_browsers = f"""
            SELECT source, count() AS cnt
            FROM {CLICKHOUSE_DB}.web_history
            GROUP BY source
            ORDER BY cnt DESC
        """
        sql_top_domains = f"""
            SELECT domain, count() AS cnt
            FROM {CLICKHOUSE_DB}.web_history
            WHERE domain != ''
            GROUP BY domain
            ORDER BY cnt DESC
            LIMIT 10
        """

        try:
            sum_res = run_clickhouse_query(sql_summary)
            b_res = run_clickhouse_query(sql_browsers)
            d_res = run_clickhouse_query(sql_top_domains)

            self.send_json(200, {
                "summary": sum_res.get("data", [{}])[0],
                "browsers": b_res.get("data", []),
                "top_domains": d_res.get("data", [])
            })
        except Exception as e:
            self.send_json(500, {"error": str(e)})


DEFAULT_SSL_CERT = Path(os.environ.get("SSL_CERT", ""))
DEFAULT_SSL_KEY = Path(os.environ.get("SSL_KEY", ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="Network Tracker Browser History Web Server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8780")), help="HTTP server port (default: 8780)")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"), help="HTTP bind address (default: 0.0.0.0)")
    parser.add_argument("--ssl", action="store_true", default=False, help="Enable HTTPS with SSL certs")
    parser.add_argument("--no-ssl", action="store_false", dest="ssl", help="Disable HTTPS and use plain HTTP")
    parser.add_argument("--cert", default=str(DEFAULT_SSL_CERT), help="Path to SSL certificate")
    parser.add_argument("--key", default=str(DEFAULT_SSL_KEY), help="Path to SSL private key")
    args = parser.parse_args()

    server_address = (args.host, args.port)
    # Threaded: one slow ClickHouse query or idle browser preconnect must not
    # stall every other request.
    httpd = ThreadingHTTPServer(server_address, NetworkTrackerHandler)
    httpd.daemon_threads = True

    proto = "http"
    cert_path = Path(args.cert) if args.cert else None
    key_path = Path(args.key) if args.key else None
    if args.ssl and cert_path and cert_path.exists() and key_path and key_path.exists():
        import ssl
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
            # Handshake lazily in the handler thread: done in accept() it blocks
            # the whole server on any client that connects and never speaks TLS.
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
            proto = "https"
        except Exception as e:
            logger.warning(f"Failed to initialize SSL ({e}), falling back to HTTP.")
            proto = "http"

    hostname = socket.gethostname() if proto == "https" else (args.host if args.host != "0.0.0.0" else "localhost")
    logger.info(f"🚀 Network Tracker Web UI running at {proto}://{hostname}:{args.port}/ (ClickHouse: {CLICKHOUSE_URL})")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down Network Tracker Web UI...")
        httpd.server_close()


if __name__ == "__main__":
    main()
