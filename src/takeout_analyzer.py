#!/usr/bin/env python3
"""
takeout_analyzer.py — Analytical inspection and ClickHouse export tool for Google Takeout archive.

Parses:
1. Wyszukiwarka/Moja_aktywność.html (Google Searches & Visited URLs, 2012-2026)
2. Chrome/Moja_aktywność.html (Chrome history)
3. YouTube/Moja_aktywność.html (Watched videos & YouTube searches)
4. Oś czasu.json (15 years of location timeline segments and GPS points)
"""

import argparse
import datetime
import html
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] takeout-analyzer: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("takeout-analyzer")

MONTH_MAP = {
    # Polish
    "sty": 1, "lut": 2, "mar": 3, "kwi": 4, "maj": 5, "cze": 6,
    "lip": 7, "sie": 8, "wrz": 9, "paź": 10, "lis": 11, "gru": 12,
    # English
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

DATE_REGEX = re.compile(
    r"(\d{1,2})\s+([a-ząćęłńóśźż]{3,4})\s+(\d{4}),\s+(\d{2}):(\d{2}):(\d{2})"
)


def parse_date(date_str: str) -> Optional[datetime.datetime]:
    m = DATE_REGEX.search(date_str)
    if not m:
        return None
    day_str, month_name, year_str, h_str, m_str, s_str = m.groups()
    mon = MONTH_MAP.get(month_name.lower())
    if not mon:
        return None
    try:
        return datetime.datetime(
            int(year_str), mon, int(day_str), int(h_str), int(m_str), int(s_str)
        )
    except ValueError:
        return None


def extract_domain(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.netloc.lower().lstrip("www.")
    except Exception:
        return ""


def parse_takeout_html(
    file_path: Path, product_name: str
) -> Iterator[Dict[str, Any]]:
    if not file_path.exists():
        logger.warning(f"File {file_path} not found. Skipping.")
        return

    logger.info(f"Scanning {product_name} ({file_path.stat().st_size / (1024*1024):.1f} MB)...")

    # Regular expressions for cell content
    cell_pattern = re.compile(
        r'<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">([^<:]+):[\s\xa0]*(.*?)</div>',
        re.DOTALL,
    )
    link_pattern = re.compile(r'<a href="([^"]+)">([^<]*)</a>')

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    for m in cell_pattern.finditer(content):
        action = m.group(1).strip().replace("\xa0", " ")
        body = m.group(2)

        # Check for link
        link_match = link_pattern.search(body)
        url = ""
        text = ""
        if link_match:
            url = link_match.group(1).strip()
            text = html.unescape(link_match.group(2).strip())
        else:
            # Plain text before <br>
            lines = body.split("<br>")
            if lines:
                text = re.sub(r"<[^>]+>", " ", lines[0]).strip()
                text = html.unescape(text)

        dt = parse_date(body)
        if not dt:
            continue

        yield {
            "timestamp": dt.strftime("%Y-%m-%d %H:%M:%S.000"),
            "datetime": dt,
            "action": action,
            "query": text,
            "url": url,
            "product": product_name,
            "host": "takeout",
        }


def analyze_stats(takeout_dir: Path) -> None:
    print(f"\n========================================================")
    print(f"📊 GOOGLE TAKEOUT ARCHIVE ANALYSIS: {takeout_dir}")
    print(f"========================================================\n")

    wyszukiwarka_html = takeout_dir / "Moja aktywność" / "Wyszukiwarka" / "Moja_aktywność.html"
    chrome_html = takeout_dir / "Moja aktywność" / "Chrome" / "Moja_aktywność.html"
    youtube_html = takeout_dir / "Moja aktywność" / "YouTube" / "Moja_aktywność.html"
    timeline_json = takeout_dir / "Oś czasu.json"

    # 1. Wyszukiwarka
    if wyszukiwarka_html.exists():
        actions_counter = Counter()
        years_counter = Counter()
        min_date = None
        max_date = None
        total = 0

        for item in parse_takeout_html(wyszukiwarka_html, "Wyszukiwarka"):
            total += 1
            actions_counter[item["action"]] += 1
            dt = item["datetime"]
            years_counter[dt.year] += 1
            if min_date is None or dt < min_date:
                min_date = dt
            if max_date is None or dt > max_date:
                max_date = dt

        print(f"🔍 [Wyszukiwarka Google]")
        print(f"   • Całkowita liczba wpisów: {total:,}")
        print(f"   • Zakres dat: {min_date} — {max_date}")
        print(f"   • Podział akcji:")
        for act, cnt in actions_counter.most_common():
            print(f"       - {act}: {cnt:,}")
        print(f"   • Aktywność w latach:")
        for yr in sorted(years_counter.keys()):
            bar = "█" * min(50, int(years_counter[yr] / max(years_counter.values()) * 50))
            print(f"       {yr}: {years_counter[yr]:6,} {bar}")
        print()

    # 2. YouTube
    if youtube_html.exists():
        yt_total = 0
        yt_actions = Counter()
        for item in parse_takeout_html(youtube_html, "YouTube"):
            yt_total += 1
            yt_actions[item["action"]] += 1
        print(f"▶️  [YouTube]")
        print(f"   • Całkowita liczba zdarzeń: {yt_total:,}")
        for act, cnt in yt_actions.most_common():
            print(f"       - {act}: {cnt:,}")
        print()

    # 3. Chrome
    if chrome_html.exists():
        ch_total = 0
        for item in parse_takeout_html(chrome_html, "Chrome"):
            ch_total += 1
        print(f"🌐 [Chrome (Aktywność z chmury)]")
        print(f"   • Całkowita liczba zdarzeń: {ch_total:,}\n")

    # 4. Location Timeline
    if timeline_json.exists():
        size_mb = timeline_json.stat().st_size / (1024 * 1024)
        print(f"📍 [Oś czasu GPS (Timeline)]")
        print(f"   • Rozmiar pliku: {size_mb:.1f} MB")
        try:
            with open(timeline_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            segments = data.get("semanticSegments", [])
            points_count = sum(len(s.get("timelinePath", [])) for s in segments)
            print(f"   • Segmenty semantyczne: {len(segments):,}")
            print(f"   • Zarejestrowane punkty GPS: {points_count:,}")
            if segments:
                print(f"   • Pierwszy zarejestrowany czas: {segments[0].get('startTime')}")
                print(f"   • Ostatni zarejestrowany czas: {segments[-1].get('startTime')}")
        except Exception as e:
            print(f"   • Błąd odczytu JSON: {e}")
        print()


def search_queries(takeout_dir: Path, keyword: str, limit: int = 50) -> None:
    wyszukiwarka_html = takeout_dir / "Moja aktywność" / "Wyszukiwarka" / "Moja_aktywność.html"
    if not wyszukiwarka_html.exists():
        logger.error(f"File {wyszukiwarka_html} does not exist.")
        return

    pattern = keyword.lower()
    matches = []
    print(f"\n🔎 Szukanie frazy '{keyword}' w historii Google (2012-2026)...\n")

    for item in parse_takeout_html(wyszukiwarka_html, "Wyszukiwarka"):
        if pattern in item["query"].lower():
            matches.append(item)
            if len(matches) >= limit:
                break

    for m in matches:
        print(f"[{m['timestamp']}] ({m['action']}) {m['query']}")
        if m["url"] and "search?q=" not in m["url"]:
            print(f"   🔗 {m['url']}")

    print(f"\nZnaleziono {len(matches)} dopasowań (limit: {limit}).")


def top_domains(takeout_dir: Path, limit: int = 30) -> None:
    wyszukiwarka_html = takeout_dir / "Moja aktywność" / "Wyszukiwarka" / "Moja_aktywność.html"
    domains_counter = Counter()

    for item in parse_takeout_html(wyszukiwarka_html, "Wyszukiwarka"):
        if item["url"]:
            d = extract_domain(item["url"])
            if d and d not in ("google.com", "google.pl"):
                domains_counter[d] += 1

    print(f"\n🌐 Top {limit} najczęściej odwiedzanych domen w historii Google:\n")
    for i, (domain, count) in enumerate(domains_counter.most_common(limit), 1):
        print(f"{i:2d}. {domain:<35} {count:6,} wizyt")


def top_queries(takeout_dir: Path, limit: int = 30) -> None:
    wyszukiwarka_html = takeout_dir / "Moja aktywność" / "Wyszukiwarka" / "Moja_aktywność.html"
    queries_counter = Counter()

    for item in parse_takeout_html(wyszukiwarka_html, "Wyszukiwarka"):
        if item["action"] == "Szukano" and item["query"]:
            q = item["query"].strip().lower()
            if len(q) > 2:
                queries_counter[q] += 1

    print(f"\n🔍 Top {limit} najczęstszych zapytań w Google:\n")
    for i, (q, count) in enumerate(queries_counter.most_common(limit), 1):
        print(f"{i:2d}. {q:<40} {count:5,} razy")


def export_to_clickhouse(
    takeout_dir: Path,
    clickhouse_url: str = "http://localhost:8123",
    batch_size: int = 5000,
    pause_sec: float = 0.1,
) -> None:
    wyszukiwarka_html = takeout_dir / "Moja aktywność" / "Wyszukiwarka" / "Moja_aktywność.html"
    youtube_html = takeout_dir / "Moja aktywność" / "YouTube" / "Moja_aktywność.html"
    chrome_html = takeout_dir / "Moja aktywność" / "Chrome" / "Moja_aktywność.html"

    url = f"{clickhouse_url.rstrip('/')}/?query=INSERT+INTO+signoz_logs.google_searches+FORMAT+JSONEachRow"

    total_exported = 0
    batch: List[Dict[str, Any]] = []

    def flush_batch() -> None:
        nonlocal batch, total_exported
        if not batch:
            return
        payload = ("\n".join(json.dumps(r, ensure_ascii=False) for r in batch) + "\n").encode("utf-8")
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
                    raise RuntimeError(f"ClickHouse returned HTTP {resp.status}: {body}")
        except Exception as e:
            logger.critical(f"Failed to export batch to ClickHouse: {e}")
            raise RuntimeError(f"ClickHouse export failed: {e}") from e

        total_exported += len(batch)
        if total_exported % 20000 == 0:
            logger.info(f"Exported {total_exported:,} records to ClickHouse...")
        batch = []
        if pause_sec > 0:
            time.sleep(pause_sec)

    # 1. Wyszukiwarka
    if wyszukiwarka_html.exists():
        for item in parse_takeout_html(wyszukiwarka_html, "Wyszukiwarka"):
            batch.append({
                "timestamp": item["timestamp"],
                "action": item["action"],
                "query": item["query"][:2000],
                "url": item["url"][:2000],
                "product": "Wyszukiwarka",
                "host": "takeout",
            })
            if len(batch) >= batch_size:
                flush_batch()

    # 2. YouTube
    if youtube_html.exists():
        for item in parse_takeout_html(youtube_html, "YouTube"):
            batch.append({
                "timestamp": item["timestamp"],
                "action": item["action"],
                "query": item["query"][:2000],
                "url": item["url"][:2000],
                "product": "YouTube",
                "host": "takeout",
            })
            if len(batch) >= batch_size:
                flush_batch()

    # 3. Chrome
    if chrome_html.exists():
        for item in parse_takeout_html(chrome_html, "Chrome"):
            batch.append({
                "timestamp": item["timestamp"],
                "action": item["action"],
                "query": item["query"][:2000],
                "url": item["url"][:2000],
                "product": "Chrome",
                "host": "takeout",
            })
            if len(batch) >= batch_size:
                flush_batch()

    flush_batch()
    logger.info(f"Export finished! Pushed {total_exported:,} Takeout records into signoz_logs.google_searches.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Google Takeout Archive Analyzer & Ingestor.")
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path.home() / "Downloads/takeout/Takeout",
        help="Path to unzipped Takeout directory",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # stats
    subparsers.add_parser("stats", help="Show full summary statistics of Google Takeout")

    # search
    search_p = subparsers.add_parser("search", help="Search Google history for keyword")
    search_p.add_argument("keyword", type=str, help="Search query or keyword")
    search_p.add_argument("--limit", type=int, default=50, help="Max results")

    # top-domains
    domains_p = subparsers.add_parser("top-domains", help="List top visited domains")
    domains_p.add_argument("--limit", type=int, default=30, help="Max domains")

    # top-queries
    queries_p = subparsers.add_parser("top-queries", help="List top searched queries")
    queries_p.add_argument("--limit", type=int, default=30, help="Max queries")

    # export-clickhouse
    export_p = subparsers.add_parser("export-clickhouse", help="Bulk stream records to ClickHouse")
    export_p.add_argument(
        "--clickhouse-url",
        type=str,
        default=os.environ.get("CLICKHOUSE_URL", "http://localhost:8123"),
    )
    export_p.add_argument("--batch-size", type=int, default=5000)
    export_p.add_argument("--pause", type=float, default=0.1)

    args = parser.parse_args()

    if args.command == "stats":
        analyze_stats(args.dir)
    elif args.command == "search":
        search_queries(args.dir, args.keyword, limit=args.limit)
    elif args.command == "top-domains":
        top_domains(args.dir, limit=args.limit)
    elif args.command == "top-queries":
        top_queries(args.dir, limit=args.limit)
    elif args.command == "export-clickhouse":
        export_to_clickhouse(
            args.dir,
            clickhouse_url=args.clickhouse_url,
            batch_size=args.batch_size,
            pause_sec=args.pause,
        )


if __name__ == "__main__":
    main()
