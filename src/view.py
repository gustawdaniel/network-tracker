#!/usr/bin/env python3
"""
view.py — Fast terminal viewer for Brain Network & Web Telemetry stored in ClickHouse.
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from typing import Any, Dict, List

CLICKHOUSE_URL = os.environ.get("CLICKHOUSE_URL", "http://localhost:8123")


def query_clickhouse(sql: str) -> List[Dict[str, Any]]:
    url = f"{CLICKHOUSE_URL.rstrip('/')}/?query=" + urllib.parse.quote(sql + " FORMAT JSON")
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("data", [])
    except Exception as e:
        print(f"❌ Błąd zapytania do ClickHouse ({CLICKHOUSE_URL}): {e}", file=sys.stderr)
        sys.exit(1)


def show_web(limit: int, domain_filter: str = "") -> None:
    where = f"WHERE domain LIKE '%{domain_filter}%'" if domain_filter else ""
    sql = f"""
        SELECT timestamp, source, domain, title, url
        FROM signoz_logs.web_history
        {where}
        ORDER BY timestamp DESC
        LIMIT {limit}
    """
    rows = query_clickhouse(sql)
    print(f"\n🌐 Ostatnie wizyty WWW ({len(rows)} wpisów):\n")
    print(f"{'Czas (Warszawa)':<23} {'Źródło':<9} {'Domena':<28} {'Tytuł strony'}")
    print("─" * 100)
    for r in rows:
        ts = str(r.get("timestamp", ""))[:19]
        src = r.get("source", "")
        dom = r.get("domain", "")[:26]
        title = r.get("title", "")[:55]
        print(f"{ts:<23} {src:<9} {dom:<28} {title}")
    print()


def show_dns(limit: int, client: str = "", domain: str = "") -> None:
    conditions = []
    if client:
        conditions.append(f"(client_name LIKE '%{client}%' OR client_ip LIKE '%{client}%')")
    if domain:
        conditions.append(f"domain LIKE '%{domain}%'")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT timestamp, client_name, client_ip, domain, query_type, status, reply_time_ms
        FROM signoz_logs.dns_history
        {where}
        ORDER BY timestamp DESC
        LIMIT {limit}
    """
    rows = query_clickhouse(sql)
    print(f"\n🛡️  Ostatnie zapytania DNS Pi-hole ({len(rows)} wpisów):\n")
    print(f"{'Czas (Warszawa)':<23} {'Klient':<18} {'Typ':<6} {'Status':<16} {'Domena'}")
    print("─" * 100)
    for r in rows:
        ts = str(r.get("timestamp", ""))[:19]
        cname = (r.get("client_name") or r.get("client_ip") or "unknown")[:16]
        qtype = r.get("query_type", "")
        status = r.get("status", "")[:14]
        dom = r.get("domain", "")[:50]
        print(f"{ts:<23} {cname:<18} {qtype:<6} {status:<16} {dom}")
    print()


def show_searches(limit: int, keyword: str = "") -> None:
    where = f"WHERE query LIKE '%{keyword}%'" if keyword else ""
    sql = f"""
        SELECT timestamp, action, query, product
        FROM signoz_logs.google_searches
        {where}
        ORDER BY timestamp DESC
        LIMIT {limit}
    """
    rows = query_clickhouse(sql)
    print(f"\n🔍 Historia Google / YouTube ({len(rows)} wpisów):\n")
    print(f"{'Czas (Warszawa)':<23} {'Produkt':<14} {'Akcja':<12} {'Zapytanie / Treść'}")
    print("─" * 100)
    for r in rows:
        ts = str(r.get("timestamp", ""))[:19]
        prod = r.get("product", "")[:12]
        act = r.get("action", "")[:10]
        q = r.get("query", "")[:60]
        print(f"{ts:<23} {prod:<14} {act:<12} {q}")
    print()


def show_stats() -> None:
    sql = """
        SELECT 
            'Historia WWW (Zen, Chrome, Chromium)' AS kategoria,
            count() AS rekordy,
            min(timestamp) AS od_daty,
            max(timestamp) AS do_daty
        FROM signoz_logs.web_history
        UNION ALL
        SELECT 
            'Zapytania DNS (Pi-hole)',
            count(),
            min(timestamp),
            max(timestamp)
        FROM signoz_logs.dns_history
        UNION ALL
        SELECT 
            'Google Takeout (2012-2026)',
            count(),
            min(timestamp),
            max(timestamp)
        FROM signoz_logs.google_searches
    """
    rows = query_clickhouse(sql)
    print(f"\n📊 Stan Magazynu Telemetrii Sieciowej ClickHouse:\n")
    print(f"{'Kategoria':<40} {'Rekordy':>12}  {'Od daty':<20} {'Do daty':<20}")
    print("─" * 98)
    for r in rows:
        kat = r.get("kategoria", "")
        cnt = f"{int(r.get('rekordy', 0)):,}"
        od = str(r.get("od_daty", ""))[:19]
        do = str(r.get("do_daty", ""))[:19]
        print(f"{kat:<40} {cnt:>12}  {od:<20} {do:<20}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Przeglądarka telemetrii sieciowej Brain OS.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # web
    p_web = sub.add_parser("web", help="Ostatnie wizyty stron WWW")
    p_web.add_argument("--limit", type=int, default=25)
    p_web.add_argument("--domain", type=str, default="")

    # dns
    p_dns = sub.add_parser("dns", help="Ostatnie zapytania DNS z Pi-hole")
    p_dns.add_argument("--limit", type=int, default=25)
    p_dns.add_argument("--client", type=str, default="")
    p_dns.add_argument("--domain", type=str, default="")

    # searches
    p_search = sub.add_parser("searches", help="Historia wyszukiwań i aktywności Google")
    p_search.add_argument("--limit", type=int, default=25)
    p_search.add_argument("--query", type=str, default="")

    # stats
    sub.add_parser("stats", help="Ogólny stan i zakresy dat tabel ClickHouse")

    args = parser.parse_args()

    if args.cmd == "web":
        show_web(limit=args.limit, domain_filter=args.domain)
    elif args.cmd == "dns":
        show_dns(limit=args.limit, client=args.client, domain=args.domain)
    elif args.cmd == "searches":
        show_searches(limit=args.limit, keyword=args.query)
    elif args.cmd == "stats":
        show_stats()


if __name__ == "__main__":
    main()
