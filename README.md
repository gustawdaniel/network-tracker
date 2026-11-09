# 🌐 Brain Network & Web Telemetry (`network-tracker`)

Moduł telemetrii sieciowej ekosystemu Brain OS. Odpowiada za ciągłe, inkrementalne i nieobciążające zbieranie zdarzeń przeglądarek WWW, zapytań DNS (Pi-hole) oraz archiwalnych danych wyszukiwania i aktywności (Google Takeout).

---

## 🏗️ Architektura i Przepływ Danych

```mermaid
flowchart TD
    subgraph rog [Stacja rog (Arch Linux)]
        Zen["Zen Browser (places.sqlite)"]
        Chrome["Chrome / Chromium (History)"]
        BSync["browser_sync.py (Timer co 3 min)"]
        CursorRog["~/.local/state/brain/browser_history_cursor.json"]
        Takeout["takeout_analyzer.py (Takeout 2012-2026)"]
        Zen --> BSync
        Chrome --> BSync
        CursorRog <--> BSync
    end

    subgraph dns_host [Pi-hole DNS Server]
        PiholeDB["/etc/pihole/pihole-FTL.db"]
        PSync["pihole_sync.py (Timer co 3 min)"]
        CursorPi["~/.local/state/brain/pihole_cursor.json"]
        PiholeDB --> PSync
        CursorPi <--> PSync
    end

    subgraph telemetry [ClickHouse Telemetry Store]
        CH["ClickHouse (:8123 HTTP API)"]
        TWeb["signoz_logs.web_history"]
        TDNS["signoz_logs.dns_history"]
        TSearch["signoz_logs.google_searches"]
        CH --> TWeb
        CH --> TDNS
        CH --> TSearch
    end

    BSync -->|Batch 1500 + 100ms pause| CH
    PSync -->|Batch 10000 + 50ms pause| CH
    Takeout -->|Batch 5000 + 100ms pause| CH
```

---

## 🚀 Komponenty

### 1. Synchronizator Historii Przeglądarek (`browser_sync.py`)
- **Ścieżka:** `src/browser_sync.py`
- **Źródła:**
  - Zen Browser: `~/.zen/*/places.sqlite` (odczyt przez SQLite URI `?immutable=1`)
  - Google Chrome: `~/.config/google-chrome/*/History`
  - Chromium: `~/.config/chromium/*/History`
- **Gwarancje:**
  - Kursor pamiętający ostatni odwiedzony `visit_id` / `visit_time`.
  - Batching 1,500 rekordów z pauzą 100ms.
  - Wyjątki krytyczne (Fail Loudly) — brak cichych błędów.
  - Zabezpieczenie systemd: `Nice=19` i `CPUSchedulingPolicy=idle`.
- **Zarządzanie:**
  ```bash
  systemctl --user status brain-browser-sync.timer
  journalctl --user -u brain-browser-sync.service -n 50 -f
  ```

### 2. Synchronizator Zapytań DNS Pi-hole (`pihole_sync.py`)
- **Ścieżka:** `src/pihole_sync.py`
- **Host instalacji:** Serwer Pi-hole
- **Baza:** `/etc/pihole/pihole-FTL.db`
- **Tabela ClickHouse:** `signoz_logs.dns_history`
- **Gwarancje wydajnościowe:**
  - Przetwarzanie paczek po 10,000 wierszy z pauzą 50ms (`time.sleep(0.05)`).
  - Zużycie CPU na Raspberry Pi poniżej 20% (w tle, bez wpływu na czasy odpowiedzi DNS).
  - Kursor `last_query_id` w `~/.local/state/brain/pihole_cursor.json`.
- **Zarządzanie na serwerze DNS:**
  ```bash
  systemctl status brain-pihole-sync.timer
  journalctl -u brain-pihole-sync.service -n 50 -f
  ```

### 3. Analizator Archiwum Google Takeout (`takeout_analyzer.py`)
- **Ścieżka:** `src/takeout_analyzer.py`
- **Katalog źródłowy:** `~/Downloads/takeout/Takeout/`
- **Możliwości:**
  - `stats`: Podsumowanie wolumenu, podział na kategorie, wykres słupkowy aktywności w latach.
  - `search <fraza>`: Błyskawiczne przeszukiwanie zapytań i wizyt Google.
  - `top-domains`: Ranking najczęściej odwiedzanych domen.
  - `top-queries`: Ranking najczęstszych wyszukiwań.
  - `export-clickhouse`: Import paczkami do `signoz_logs.google_searches`.
- **Przykłady użycia:**
  ```bash
  # Statystyki Takeout
  python3 src/takeout_analyzer.py stats

  # Szukanie frazy w historii
  python3 src/takeout_analyzer.py search "search_phrase" --limit 10

  # Najczęstsze zapytania
  python3 src/takeout_analyzer.py top-queries --limit 20
  ```

---

## 📊 Tabele Telemetrii w ClickHouse

Wszystkie dane są zapisywane w silniku `ReplacingMergeTree` z automatyczną deduplikacją:

1. **`signoz_logs.web_history`**:
   - `timestamp`: DateTime64(3, 'Europe/Warsaw')
   - `source`: LowCardinality(String) (`zen`, `chrome`, `chromium`)
   - `url`: String
   - `domain`: String
   - `title`: String
   - `visit_duration_ms`: UInt32
   - `visit_id`: UInt64
   - `host`: LowCardinality(String)

2. **`signoz_logs.dns_history`**:
   - `timestamp`: DateTime64(3, 'Europe/Warsaw')
   - `query_id`: UInt64
   - `client_ip`: LowCardinality(String)
   - `client_name`: LowCardinality(String)
   - `domain`: String
   - `query_type`: LowCardinality(String) (`A`, `AAAA`, `HTTPS`, etc.)
   - `status`: LowCardinality(String) (`CACHED`, `FORWARDED`, `BLOCKED_GRAVITY`, etc.)
   - `reply_time_ms`: Float32
   - `server`: LowCardinality(String)

3. **`signoz_logs.google_searches`**:
   - `timestamp`: DateTime64(3, 'Europe/Warsaw')
   - `action`: LowCardinality(String) (`Szukano`, `Odwiedzono`, `Obejrzano`)
   - `query`: String
   - `url`: String
   - `product`: LowCardinality(String) (`Wyszukiwarka`, `YouTube`, `Chrome`)
   - `host`: LowCardinality(String) (`takeout`)
