#!/usr/bin/env python3
"""
Scarica da LiveSoccerTV il palinsesto di piu' canali (pagine /channels/...) e lo
salva in channel_matches.json.

- Ogni canale ha il suo intervallo di aggiornamento (every_days).
- Ogni esecuzione scarica al massimo MAX_PER_RUN canali, partendo dai piu' vecchi
  (a parita' vale l'ordine della lista CHANNELS): con tanti canali non si fanno mai troppe richieste in un colpo.
- Pausa casuale tra una richiesta e l'altra; stop immediato se il sito risponde 403/429.
- Se un canale fallisce restano i suoi dati precedenti e gli altri vengono salvati.
- Exit 1 se almeno un canale ha dato errore (il file viene comunque scritto).

Uso:  python scrape_channels.py [--out channel_matches.json] [--force] [--all]
Env:  PAGE_TZ  fuso in cui il sito mostra gli orari (default America/New_York)
"""
import argparse, json, os, random, re, sys, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────────────────────────────────────
# CANALI. "name" = nome/id del canale nel tuo programma (EPG); "url" = pagina su
# livesoccertv.com; "every_days" = ogni quanti giorni riscaricarlo.
# ──────────────────────────────────────────────────────────────────────────────
_B = "https://www.livesoccertv.com/channels/"
CHANNELS = [
    {"name": "Zona DAZN 1",      "url": _B + "dazn-1-italy/",             "every_days": 1},
    {"name": "Zona DAZN 2",      "url": _B + "dazn-1-plus/",              "every_days": 1},
    {"name": "DAZN Italia",      "url": _B + "dazn-italy/",               "every_days": 1},
    {"name": "Sky Sport Uno",    "url": _B + "sky-sport-uno/",            "every_days": 1},
    {"name": "Sky Sport Calcio", "url": _B + "sky-sport-serie-a/",        "every_days": 1},
    {"name": "Sky Sport Max",    "url": _B + "sky-sport-collection/",     "every_days": 1},
    {"name": "Sky Sport Arena",  "url": _B + "sky-sport-arena-italy/",    "every_days": 1},
    {"name": "Sky Sport 251",    "url": _B + "sky-calcio-1/",             "every_days": 1},
    {"name": "Sky Sport 252",    "url": _B + "sky-calcio-2/",             "every_days": 1},
    {"name": "Sky Sport 253",    "url": _B + "sky-calcio-3/",             "every_days": 1},
    # ---- fin qui i primi 10 (esecuzione delle 05:17 UTC); il resto va alle 11:17 UTC ----
    {"name": "Sky Sport 254",    "url": _B + "sky-calcio-4/",             "every_days": 1},
    {"name": "Sky Sport 255",    "url": _B + "sky-calcio-5/",             "every_days": 1},
    {"name": "Sky Sport 256",    "url": _B + "sky-calcio-6/",             "every_days": 1},
    {"name": "Sky Sport 257",    "url": _B + "sky-calcio-7/",             "every_days": 1},
    {"name": "Sky Sport 258",    "url": _B + "sky-calcio-8/",             "every_days": 1},
    {"name": "Sky Sport 259",    "url": _B + "sky-calcio-9/",             "every_days": 1},
    {"name": "Sky Sport 260",    "url": _B + "sky-calcio-10/",            "every_days": 1},
    {"name": "RSI LA2",          "url": _B + "rsi-la-2/",                 "every_days": 1},
    {"name": "Prime Video",      "url": _B + "amazon-prime-video-italy/", "every_days": 7},
]

PAUSE_SECONDS = (10, 20)     # pausa casuale tra due richieste
MAX_PER_RUN = 10             # al massimo N canali per esecuzione (i piu' vecchi per primi)
KEEP_PAST_DAYS = 2           # le partite piu' vecchie vengono scartate

UA = "Mozilla/5.0 (compatible; MatchScheduleBot/1.0; personal use, few requests/day)"
DATE_RE = re.compile(r"/schedules/(\d{4}-\d{2}-\d{2})/")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([ap]m)?", re.I)


class Blocked(Exception):
    """403/429: il sito ci sta limitando, fermarsi."""


def fetch(url):
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en"}, timeout=30)
        except requests.RequestException as e:
            last = repr(e)
        else:
            if r.status_code == 200:
                return r.text
            if r.status_code in (403, 429):
                raise Blocked(f"HTTP {r.status_code}")
            last = f"HTTP {r.status_code}"
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


def parse(html, tz_name, h1_contains=None):
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if not h1:
        raise RuntimeError("pagina non riconosciuta (nessun h1): possibile blocco/captcha")
    if h1_contains and h1_contains.lower() not in h1.get_text(" ", strip=True).lower():
        raise RuntimeError(f"titolo inatteso: {h1.get_text(' ', strip=True)!r}")

    matches, cur_date = [], None
    for tr in soup.find_all("tr"):
        match_a = tr.find("a", href=re.compile(r"/match/"))
        if not match_a:
            for a in tr.find_all("a", href=DATE_RE):
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
            "date": cur_date,
            "time_page": f"{hm[0]:02d}:{hm[1]:02d}" if hm else None,
            "start_utc": to_utc(cur_date, hm, tz_name),
            "competition": (comp_a.get("title") or comp_a.get_text(strip=True)) if comp_a else None,
            "url": match_a.get("href"),
        })

    if not matches and "no upcoming" not in soup.get_text(" ", strip=True).lower():
        raise RuntimeError("nessuna partita e nessun messaggio 'no upcoming': struttura HTML cambiata?")
    return matches


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def parse_iso(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def is_due(entry, every_days, force):
    if force or not entry or not entry.get("fetched"):
        return True
    t = parse_iso(entry["fetched"])
    if t is None:
        return True
    # margine di 2 ore: il cron giornaliero puo' scivolare di qualche minuto
    return datetime.now(timezone.utc) - t >= timedelta(days=every_days) - timedelta(hours=2)


def drop_past(matches):
    limit = (datetime.now(timezone.utc) - timedelta(days=KEEP_PAST_DAYS)).strftime("%Y-%m-%d")
    return [m for m in matches if not m.get("date") or m["date"] >= limit]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="channel_matches.json")
    ap.add_argument("--force", action="store_true", help="ignora every_days (resta il tetto MAX_PER_RUN)")
    ap.add_argument("--all", action="store_true", help="TEST: scarica tutti i canali, ignorando every_days e MAX_PER_RUN")
    args = ap.parse_args()
    max_per_run = len(CHANNELS) if args.all else MAX_PER_RUN
    if args.all:
        args.force = True
    tz_name = os.environ.get("PAGE_TZ", "America/New_York")

    try:
        with open(args.out, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("channels"), dict):
            data = {"channels": {}}
    except Exception:
        data = {"channels": {}}
    chans = data["channels"]

    # canali da scaricare: scaduti, ordinati dal piu' vecchio (mai scaricati per primi)
    due = [c for c in CHANNELS if is_due(chans.get(c["name"]), c.get("every_days", 1), args.force)]
    # piu' vecchi per primi; a parita' (es. primo giro, mai scaricati) vale l'ordine della lista
    order = {c["name"]: i for i, c in enumerate(CHANNELS)}
    due.sort(key=lambda c: ((chans.get(c["name"]) or {}).get("fetched") or "", order[c["name"]]))
    todo, later = due[:max_per_run], due[max_per_run:]
    for c in CHANNELS:
        if c not in due:
            print(f"[{c['name']}] aggiornato il {chans[c['name']]['fetched']}: salto")
    for c in later:
        print(f"[{c['name']}] rimandato alla prossima esecuzione")

    errors, requests_done, blocked = 0, 0, False
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for ch in todo:
        name = ch["name"]
        if blocked:
            print(f"[{name}] rimandato (blocco in corso)")
            continue
        if requests_done:
            time.sleep(random.uniform(*PAUSE_SECONDS))
        requests_done += 1
        try:
            html = fetch(ch["url"])
            matches = drop_past(parse(html, tz_name, ch.get("h1_contains")))
            future = sum(1 for m in matches if m.get("date") and m["date"] >= today)
            chans[name] = {"url": ch["url"], "fetched": now_iso(), "matches": matches}
            warn = "" if future else "  <-- ATTENZIONE: nessuna partita futura"
            print(f"[{name}] OK: {len(matches)} partite ({future} future){warn}")
        except Blocked as e:
            blocked = True
            errors += 1
            print(f"[{name}] BLOCCATO ({e}): mi fermo, tengo i dati precedenti", file=sys.stderr)
            chans.setdefault(name, {"url": ch["url"], "matches": []})["last_error"] = f"{now_iso()} {e}"
        except Exception as e:
            errors += 1
            print(f"[{name}] ERRORE: {e}", file=sys.stderr)
            chans.setdefault(name, {"url": ch["url"], "matches": []})["last_error"] = f"{now_iso()} {e}"

    # scarta dal file i canali tolti dalla lista CHANNELS
    wanted = {c["name"] for c in CHANNELS}
    for stale in [k for k in chans if k not in wanted]:
        del chans[stale]

    data["updated"] = now_iso()
    data["page_timezone"] = tz_name
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, args.out)
    print(f"Scritto {args.out} ({requests_done} richieste, {errors} errori)")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
