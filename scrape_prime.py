#!/usr/bin/env python3
"""
Scarica da LiveSoccerTV il palinsesto del canale "Amazon Prime Video (Italy)"
e lo salva in prime_matches.json.

Pagina: https://www.livesoccertv.com/channels/amazon-prime-video-italy/
(la pagina del canale e' per paese: non dipende dall'IP, a differenza delle
pagine dei campionati che mostrano i canali del paese del visitatore).

Uso:   python scrape_prime.py [--out prime_matches.json]
Env:   PAGE_TZ  fuso orario in cui il sito mostra gli orari (default America/New_York,
        osservato aprendo la pagina da un IP USA). Se cambia, l'ora UTC calcolata sbaglia:
        per questo conviene abbinare le partite per data + squadre, non per ora.
Exit:  0 ok | 1 pagina non riconosciuta / bloccata (il JSON precedente NON viene toccato)
"""
import argparse, json, os, re, sys, time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

URL = "https://www.livesoccertv.com/channels/amazon-prime-video-italy/"
UA = "Mozilla/5.0 (compatible; PrimeScheduleBot/1.0; personal use, 1 request/day)"
DATE_RE = re.compile(r"/schedules/(\d{4}-\d{2}-\d{2})/")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([ap]m)?", re.I)


def fetch(url):
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en"}, timeout=30)
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = repr(e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"download fallito: {last}")


def parse_time(text):
    m = TIME_RE.search(text or "")
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if ap == "pm" and h != 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    return h, mi


def to_utc(date_s, hm, tz_name):
    if not date_s or not hm:
        return None
    try:
        local = datetime.strptime(date_s, "%Y-%m-%d").replace(hour=hm[0], minute=hm[1], tzinfo=ZoneInfo(tz_name))
        return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    except Exception:
        return None


def parse(html, tz_name):
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if not h1 or "prime video" not in h1.get_text(" ", strip=True).lower():
        raise RuntimeError("pagina non riconosciuta (manca l'h1 'Amazon Prime Video'): possibile blocco/captcha")

    matches, cur_date = [], None
    for tr in soup.find_all("tr"):
        match_a = tr.find("a", href=re.compile(r"/match/"))
        if not match_a:
            for a in tr.find_all("a", href=DATE_RE):          # riga-intestazione del giorno
                cur_date = DATE_RE.search(a["href"]).group(1)
            continue
        title = (match_a.get("title") or match_a.get_text(" ", strip=True) or "").strip()
        parts = re.split(r"\s+vs\.?\s+", title, maxsplit=1, flags=re.I)
        if len(parts) != 2:
            continue
        comp_a = tr.find("a", href=re.compile(r"/competitions/"))
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        hm = next((parse_time(c) for c in cells if parse_time(c)), None)
        matches.append({
            "home": parts[0].strip(),
            "away": parts[1].strip(),
            "date": cur_date,                                   # data nel fuso della pagina
            "time_page": f"{hm[0]:02d}:{hm[1]:02d}" if hm else None,
            "start_utc": to_utc(cur_date, hm, tz_name),         # indicativo (vedi PAGE_TZ)
            "competition": (comp_a.get("title") or comp_a.get_text(strip=True)) if comp_a else None,
            "url": match_a.get("href"),
        })

    if not matches and "no upcoming" not in soup.get_text(" ", strip=True).lower():
        raise RuntimeError("nessuna partita trovata e nessun messaggio 'no upcoming': struttura HTML cambiata?")
    return matches


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="prime_matches.json")
    ap.add_argument("--html", help="file HTML locale (per test) invece di scaricare")
    args = ap.parse_args()
    tz_name = os.environ.get("PAGE_TZ", "America/New_York")

    try:
        html = open(args.html, encoding="utf-8").read() if args.html else fetch(URL)
        matches = parse(html, tz_name)
    except Exception as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        sys.exit(1)

    data = {
        "source": URL,
        "channel": "Prime Video",
        "page_timezone": tz_name,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "matches": matches,
    }
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, args.out)
    print(f"OK: {len(matches)} partite -> {args.out}")
    for m in matches:
        print(f"  {m['date']} {m['time_page']}  {m['home']} vs {m['away']}  [{m['competition']}]")


if __name__ == "__main__":
    main()
