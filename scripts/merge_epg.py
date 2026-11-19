#!/usr/bin/env python3
"""
merge_epg.py
Legge tutti i file .gz EPG dalla cartella epg/, normalizza i channel id
leggendo la mappa degli alias da riferimento.txt, e produce epg/merged_epg.xml.gz
scegliendo per ogni canale la fonte con più informazioni.

IMPORTANTE: vengono inclusi SOLO i canali presenti in riferimento.txt.

Gestione fusi orari:
  - Se una sorgente ha mix +0000 e +0200, vengono tenuti SOLO i +0000
    (filtro applicato PRIMA del calcolo del punteggio).
  - L'output mantiene gli offset originali senza conversioni.

Eventi live (LiveSoccerTV):
  Prima del merge lo script scarica i palinsesti dei canali (scraper integrato,
  max MAX_PER_RUN canali per esecuzione, i piu' vecchi per primi) e li salva in
  channel_matches.json. Gli eventi vengono poi aggiunti all'EPG finale
  IGNORANDO le regole di merge: ci sono sempre, con descrizione "Evento Live",
  orario convertito dall'UTC all'offset usato dall'EPG di quel canale.
  Se l'EPG ha gia' quella partita (stesso orario, stesse squadre) si tiene il suo
  titolo/descrizione e si antepone "Evento Live - " alla descrizione; altri programmi
  che si sovrappongono a un evento nuovo vengono rimossi.

Uso: python merge_epg.py [epg_dir] [output_gz] [riferimento.txt]
         [--matches channel_matches.json] [--no-scrape] [--force] [--all]
     default: epg/  epg/merged_epg.xml.gz  riferimento.txt
"""

import os
import gzip
import glob
import sys
import re
import json
import random
import time
import argparse
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from lxml import etree


# ---------------------------------------------------------------------------
# Parsing di riferimento.txt  →  ALIAS_MAP  +  CANONICAL_IDS (whitelist)
# ---------------------------------------------------------------------------

def load_alias_map(ref_path: str) -> tuple[dict[str, str], set[str]]:
    alias_map: dict[str, str] = {}
    canonical_ids: set[str] = set()

    try:
        with open(ref_path, "r", encoding="utf-8-sig") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"[WARN] {ref_path} non trovato — nessun alias applicato.", file=sys.stderr)
        return alias_map, canonical_ids

    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    current_id: str | None = None

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.endswith(":") and "(" not in line:
            current_id = line[:-1].strip()
            canonical_ids.add(current_id)
            alias_map[_norm(current_id)] = current_id
            continue
        if current_id is not None and "(no additional aliases)" not in line:
            alias_map[_norm(line)] = current_id

    return alias_map, canonical_ids


def _norm(s: str) -> str:
    return s.strip().lower()


_STRIP_SUFFIXES = (
    " hd", " fhd", " sd", " full hd", " 4k",
    ".hd", ".sd", ".fhd",
    " hd.it", " sd.it", ".it",
)


def resolve_channel_id(raw_id: str, alias_map: dict[str, str]) -> str | None:
    key = _norm(raw_id)
    if key in alias_map:
        return alias_map[key]
    key2 = key
    if key2.endswith(".it"):
        key2 = key2[:-3].rstrip(".")
        if key2 in alias_map:
            return alias_map[key2]
    for suffix in _STRIP_SUFFIXES:
        if key.endswith(suffix):
            k3 = key[: -len(suffix)].strip(" .")
            if k3 in alias_map:
                return alias_map[k3]
            if k3.endswith(".it"):
                k3b = k3[:-3].rstrip(".")
                if k3b in alias_map:
                    return alias_map[k3b]
    return None


# ---------------------------------------------------------------------------
# Gestione timestamp EPG
# ---------------------------------------------------------------------------

_TS_RE = re.compile(r'^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\s*([+-]\d{4})?')

def _get_offset_str(ts: str) -> str | None:
    """Restituisce la stringa offset es. '+0200', '+0000', o None se assente."""
    m = _TS_RE.match(ts.strip())
    if not m:
        return None
    return m.group(7)


# ---------------------------------------------------------------------------
# Filtro fuso orario per lista programmi di una singola sorgente
# ---------------------------------------------------------------------------

def filter_timezone(prog_list: list) -> tuple[list, str]:
    """
    Analizza i timestamp della lista e:
      - Se ci sono sia +0200 che +0000 → tieni solo i +0000  (mix → filtra)
      - Se ci sono solo +0200 → lista invariata
      - Se ci sono solo +0000 → lista invariata
      - Se non c'è offset → lista invariata

    Restituisce (lista_filtrata, tipo):
      tipo = 'only_local'  (solo +0200 o offset non-zero)
             'only_utc'    (solo +0000)
             'mixed'       (aveva mix, ora solo +0000)
             'no_offset'   (nessun offset presente)
    """
    has_local = False
    has_utc   = False

    for prog in prog_list:
        for attr in ("start", "stop"):
            off = _get_offset_str(prog.get(attr, ""))
            if off is None:
                continue
            if off == "+0000":
                has_utc = True
            else:
                has_local = True

    if has_local and has_utc:
        filtered = [
            p for p in prog_list
            if _get_offset_str(p.get("start", "")) == "+0000"
        ]
        return filtered, "mixed"
    elif has_local and not has_utc:
        return prog_list, "only_local"
    elif has_utc and not has_local:
        return prog_list, "only_utc"
    else:
        return prog_list, "no_offset"


# ---------------------------------------------------------------------------
# Scoring dei programmi
# ---------------------------------------------------------------------------

def score_programme(prog_elem) -> int:
    score = 1
    for child in prog_elem:
        tag  = child.tag
        text = (child.text or "").strip()
        if text:
            score += 1
        if tag == "desc":
            score += 8
        elif tag == "episode-num":
            score += 4
        elif tag in ("icon", "image"):
            score += 3
        elif tag in ("category", "rating", "star-rating", "credits"):
            score += 2
        elif tag in ("sub-title", "date"):
            score += 2
        else:
            score += 1
    return score


# ---------------------------------------------------------------------------
# Parsing di un singolo file EPG
# ---------------------------------------------------------------------------

def _read_file_bytes(filepath: str) -> bytes | None:
    try:
        with gzip.open(filepath, "rb") as f:
            data = f.read()
        stripped = data.lstrip(b'\xef\xbb\xbf \t\r\n')
        if stripped.startswith(b"<") or stripped.startswith(b"<?"):
            return data
    except Exception:
        pass
    try:
        with open(filepath, "rb") as f:
            data = f.read()
        stripped = data.lstrip(b'\xef\xbb\xbf \t\r\n')
        if stripped.startswith(b"<") or stripped.startswith(b"<?"):
            return data
    except Exception as e:
        print(f"  [WARN] Impossibile leggere {filepath}: {e}", file=sys.stderr)
    return None


def parse_gz_epg(filepath: str, alias_map: dict) -> tuple[dict, dict]:
    channels: dict   = {}
    programmes: dict = defaultdict(list)

    data = _read_file_bytes(filepath)
    if data is None:
        return channels, programmes

    data = data.lstrip(b'\xef\xbb\xbf')

    try:
        root = etree.fromstring(data)
    except etree.XMLSyntaxError:
        try:
            root = etree.fromstring(data, etree.XMLParser(recover=True))
        except Exception as e:
            print(f"  [WARN] XML non valido in {filepath}: {e}", file=sys.stderr)
            return channels, programmes

    skipped_ch = 0
    for ch in root.findall("channel"):
        raw_id = ch.get("id", "").strip()
        if not raw_id:
            continue
        cid = resolve_channel_id(raw_id, alias_map)
        if cid is None:
            skipped_ch += 1
            continue
        ch.set("id", cid)
        if cid not in channels:
            channels[cid] = ch

    skipped_prog = 0
    for prog in root.findall("programme"):
        raw_ch = prog.get("channel", "").strip()
        if not raw_ch:
            continue
        cid = resolve_channel_id(raw_ch, alias_map)
        if cid is None:
            skipped_prog += 1
            continue
        prog.set("channel", cid)
        programmes[cid].append(prog)

    if skipped_ch or skipped_prog:
        print(f"    ↳ scartati {skipped_ch} canali e {skipped_prog} programmi non in whitelist")

    return channels, programmes


# ---------------------------------------------------------------------------
# Scraper LiveSoccerTV  →  channel_matches.json
# ---------------------------------------------------------------------------
# "name" = nome del canale come in riferimento.txt (alias) / nel tuo programma.
# Se il nome non e' un alias, aggiungi "epg_id": "<id canonico>".
_B = "https://www.livesoccertv.com/channels/"
CHANNELS = [
    {"name": "Zona DAZN",        "url": _B + "dazn-1-italy/",             "every_days": 1},
    {"name": "Zona DAZN 2",      "url": _B + "dazn-1-plus/",              "every_days": 1},
    {"name": "DAZN Italia",      "url": _B + "dazn-italy/",               "every_days": 1},
    {"name": "Sky Sport Uno",    "url": _B + "sky-sport-uno/",            "every_days": 1},
    {"name": "Sky Sport Calcio", "url": _B + "sky-sport-serie-a/",        "every_days": 1},
    {"name": "Sky Sport Max",    "url": _B + "sky-sport-collection/",     "every_days": 1},
    {"name": "Sky Sport Arena",  "url": _B + "sky-sport-arena-italy/",    "every_days": 1},
    {"name": "Sky Sport 251",    "url": _B + "sky-calcio-1/",             "every_days": 1},
    {"name": "Sky Sport 252",    "url": _B + "sky-calcio-2/",             "every_days": 1},
    {"name": "Sky Sport 253",    "url": _B + "sky-calcio-3/",             "every_days": 1},
    {"name": "Sky Sport 254",    "url": _B + "sky-calcio-4/",             "every_days": 1},
    {"name": "Sky Sport 255",    "url": _B + "sky-calcio-5/",             "every_days": 1},
    {"name": "Sky Sport 256",    "url": _B + "sky-calcio-6/",             "every_days": 1},
    {"name": "Sky Sport 257",    "url": _B + "sky-calcio-7/",             "every_days": 1},
    {"name": "Sky Sport 258",    "url": _B + "sky-calcio-8/",             "every_days": 1},
    {"name": "Sky Sport 259",    "url": _B + "sky-calcio-9/",             "every_days": 1},
    {"name": "Sky Sport 260",    "url": _B + "sky-calcio-10/",            "every_days": 1},
    {"name": "RSI LA2",          "url": _B + "rsi-la-2/",                 "every_days": 1},
    {"name": "Prime Video",      "url": _B + "amazon-prime-video-italy/", "every_days": 1},
]

PAUSE_SECONDS     = (10, 20)   # pausa casuale tra due richieste
MAX_PER_RUN       = len(CHANNELS)   # max canali per esecuzione (tutti); abbassalo per ruotarli (i piu' vecchi per primi)
KEEP_PAST_DAYS    = 2          # partite piu' vecchie scartate dal JSON
RETRY_AFTER_HOURS = 6          # dopo un errore/blocco un canale non viene ritentato prima
EVENT_DURATION    = timedelta(minutes=120)   # il sito non da' la fine: durata fissa
EVENT_DESC        = "Evento Live"
MATCH_TOLERANCE   = timedelta(minutes=60)   # scarto max inizio EPG / inizio evento per "stessa partita"

_UA = "Mozilla/5.0 (compatible; MatchScheduleBot/1.0; personal use, few requests/day)"
_DATE_RE = re.compile(r"/schedules/(\d{4}-\d{2}-\d{2})/")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([ap]m)?", re.I)


class Blocked(Exception):
    """403/429: il sito ci sta limitando, fermarsi."""


def _fetch(url):
    import requests
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers={"User-Agent": _UA, "Accept-Language": "en"}, timeout=30)
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


def _parse_page_time(text):
    m = _TIME_RE.search(text or "")
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if ap == "pm" and h != 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    return h, mi


def _to_utc(date_s, hm, tz_name):
    if not date_s or not hm:
        return None
    try:
        local = datetime.strptime(date_s, "%Y-%m-%d").replace(
            hour=hm[0], minute=hm[1], tzinfo=ZoneInfo(tz_name))
        return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    except Exception:
        return None


def _parse_page(html, tz_name, h1_contains=None):
    from bs4 import BeautifulSoup
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
            for a in tr.find_all("a", href=_DATE_RE):
                cur_date = _DATE_RE.search(a["href"]).group(1)
            continue
        title = (match_a.get("title") or match_a.get_text(" ", strip=True) or "").strip()
        parts = re.split(r"\s+vs\.?\s+", title, maxsplit=1, flags=re.I)
        if len(parts) != 2:
            continue
        comp_a = tr.find("a", href=re.compile(r"/competitions/"))
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        hm = next((_parse_page_time(c) for c in cells if _parse_page_time(c)), None)
        matches.append({
            "home": parts[0].strip(),
            "away": parts[1].strip(),
            "date": cur_date,
            "time_page": f"{hm[0]:02d}:{hm[1]:02d}" if hm else None,
            "start_utc": _to_utc(cur_date, hm, tz_name),
            "competition": (comp_a.get("title") or comp_a.get_text(strip=True)) if comp_a else None,
            "url": match_a.get("href"),
        })

    if not matches and "no upcoming" not in soup.get_text(" ", strip=True).lower():
        raise RuntimeError("nessuna partita e nessun messaggio 'no upcoming': struttura HTML cambiata?")
    return matches


def _now_iso(dt=None):
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%MZ")


def _parse_iso(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _is_due(entry, every_days, force):
    if force or not entry or not entry.get("fetched"):
        return True
    t = _parse_iso(entry["fetched"])
    if t is None:
        return True
    # margine di 2 ore: il cron puo' scivolare
    return datetime.now(timezone.utc) - t >= timedelta(days=every_days) - timedelta(hours=2)


def _in_backoff(entry):
    """True se il canale ha avuto un errore da meno di RETRY_AFTER_HOURS."""
    err = (entry or {}).get("last_error")
    if not err:
        return False
    t = _parse_iso(err.split(" ", 1)[0])
    return bool(t) and datetime.now(timezone.utc) - t < timedelta(hours=RETRY_AFTER_HOURS)


def _drop_past(matches):
    limit = (datetime.now(timezone.utc) - timedelta(days=KEEP_PAST_DAYS)).strftime("%Y-%m-%d")
    return [m for m in matches if not m.get("date") or m["date"] >= limit]


def scrape_channels(out_path: str, force: bool = False, all_channels: bool = False) -> int:
    """Aggiorna out_path. Ritorna il numero di errori (il file viene scritto comunque)."""
    max_per_run = len(CHANNELS) if all_channels else MAX_PER_RUN
    if all_channels:
        force = True
    tz_name = os.environ.get("PAGE_TZ", "America/New_York")

    try:
        with open(out_path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("channels"), dict):
            data = {"channels": {}}
    except Exception:
        data = {"channels": {}}
    chans = data["channels"]
    changed = False

    # blocco globale ancora attivo → non toccare il sito
    bu = _parse_iso(data.get("blocked_until", ""))
    if bu and not force:
        if datetime.now(timezone.utc) < bu:
            print(f"[scrape] blocco attivo fino a {data['blocked_until']}: nessuna richiesta")
            return 0
    if "blocked_until" in data:
        del data["blocked_until"]
        changed = True

    due = [c for c in CHANNELS
           if _is_due(chans.get(c["name"]), c.get("every_days", 1), force)
           and (force or not _in_backoff(chans.get(c["name"])))]
    order = {c["name"]: i for i, c in enumerate(CHANNELS)}
    due.sort(key=lambda c: ((chans.get(c["name"]) or {}).get("fetched") or "", order[c["name"]]))
    todo = due[:max_per_run]
    print(f"[scrape] {len(due)} canali da aggiornare, ne scarico {len(todo)} (max {max_per_run})")

    errors, done, blocked = 0, 0, False
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for ch in todo:
        name = ch["name"]
        if blocked:
            print(f"[{name}] rimandato (blocco in corso)")
            continue
        if done:
            time.sleep(random.uniform(*PAUSE_SECONDS))
        done += 1
        try:
            html = _fetch(ch["url"])
            matches = _drop_past(_parse_page(html, tz_name, ch.get("h1_contains")))
            future = sum(1 for m in matches if m.get("date") and m["date"] >= today)
            chans[name] = {"url": ch["url"], "fetched": _now_iso(), "matches": matches}
            warn = "" if future else "  <-- nessuna partita futura"
            print(f"[{name}] OK: {len(matches)} partite ({future} future){warn}")
        except Blocked as e:
            blocked = True
            errors += 1
            print(f"[{name}] BLOCCATO ({e}): mi fermo, tengo i dati precedenti", file=sys.stderr)
            chans.setdefault(name, {"url": ch["url"], "matches": []})["last_error"] = f"{_now_iso()} {e}"
            data["blocked_until"] = _now_iso(datetime.now(timezone.utc) + timedelta(hours=RETRY_AFTER_HOURS))
        except Exception as e:
            errors += 1
            print(f"[{name}] ERRORE: {e}", file=sys.stderr)
            chans.setdefault(name, {"url": ch["url"], "matches": []})["last_error"] = f"{_now_iso()} {e}"

    wanted = {c["name"] for c in CHANNELS}
    for stale in [k for k in chans if k not in wanted]:
        del chans[stale]
        changed = True

    if done or changed:
        data["updated"] = _now_iso()
        data["page_timezone"] = tz_name
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, out_path)
        print(f"[scrape] scritto {out_path} ({done} richieste, {errors} errori)")
    return errors


# ---------------------------------------------------------------------------
# Eventi live  →  <programme> nell'EPG finale (ignorano le regole di merge)
# ---------------------------------------------------------------------------

def _off_delta(off: str) -> timedelta:
    sign = -1 if off.startswith("-") else 1
    return sign * timedelta(hours=int(off[1:3]), minutes=int(off[3:5]))


def _parse_ts(ts: str):
    """Timestamp XMLTV → datetime aware (senza offset = UTC). None se illeggibile."""
    m = _TS_RE.match((ts or "").strip())
    if not m:
        return None
    try:
        y, mo, d, h, mi, s = (int(x) for x in m.groups()[:6])
        return datetime(y, mo, d, h, mi, s, tzinfo=timezone(_off_delta(m.group(7) or "+0000")))
    except ValueError:
        return None


def _fmt_ts(dt_utc: datetime, off: str) -> str:
    local = dt_utc.astimezone(timezone(_off_delta(off)))
    return local.strftime("%Y%m%d%H%M%S") + " " + off


def _dominant_offset(progs: list) -> str | None:
    c = Counter(o for p in progs if (o := _get_offset_str(p.get("start", ""))))
    return c.most_common(1)[0][0] if c else None


def load_events(matches_path: str, alias_map: dict) -> dict[str, list[dict]]:
    """channel_matches.json → {id canonico: [evento, ...]} (solo eventi non finiti)."""
    try:
        with open(matches_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[eventi] {matches_path} non leggibile ({e}): nessun evento aggiunto")
        return {}

    epg_id = {c["name"]: c.get("epg_id") or c["name"] for c in CHANNELS}
    horizon = datetime.now(timezone.utc) - timedelta(hours=1)
    out: dict[str, list[dict]] = defaultdict(list)
    seen = set()
    unresolved = []

    for name, entry in (data.get("channels") or {}).items():
        cid = resolve_channel_id(epg_id.get(name, name), alias_map)
        if cid is None:
            unresolved.append(name)
            continue
        for m in entry.get("matches", []):
            start = _parse_iso(m.get("start_utc") or "")
            if start is None or start + EVENT_DURATION < horizon:
                continue
            key = (cid, start, m.get("home"), m.get("away"))
            if key in seen:
                continue
            seen.add(key)
            out[cid].append({"start": start, "end": start + EVENT_DURATION,
                             "home": m.get("home") or "?", "away": m.get("away") or "?",
                             "comp": m.get("competition")})
    if unresolved:
        print(f"[eventi] [WARN] canali non presenti in riferimento.txt (eventi ignorati): {', '.join(unresolved)}")
    return out


def _words(text: str) -> list[str]:
    import unicodedata
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z0-9]+", t)


_STOP = {"fc", "ac", "as", "us", "cf", "de", "la", "el", "the", "sc", "cd", "ud", "afc", "calcio"}


def _team_in(team: str, words: list[str]) -> bool:
    toks = [t for t in _words(team) if t not in _STOP]
    for t in toks:
        for w in words:
            if w == t or (min(len(w), len(t)) >= 4 and (w.startswith(t) or t.startswith(w))):
                return True
    return False


def _same_match(prog, ev) -> bool:
    """Programma EPG = stessa partita dell'evento? (inizio vicino + entrambe le squadre nel testo)"""
    s = _parse_ts(prog.get("start", ""))
    if s is None or abs(s - ev["start"]) > MATCH_TOLERANCE:
        return False
    text = " ".join(prog.findtext(tag) or "" for tag in ("title", "sub-title", "desc"))
    words = _words(text)
    return _team_in(ev["home"], words) and _team_in(ev["away"], words)


def inject_events(best_programmes: dict[str, list], events: dict[str, list[dict]]) -> int:
    """Aggiunge gli eventi a best_programmes. I programmi che li coprono vengono rimossi."""
    global_off = _dominant_offset([p for pl in best_programmes.values() for p in pl]) or "+0000"
    added = removed = unified = 0

    for cid, evs in events.items():
        existing = best_programmes.get(cid, [])
        off = _dominant_offset(existing) or global_off

        # 1) partita gia' presente nell'EPG (stesso orario, stesse squadre): si tiene
        #    il programma dell'EPG e si aggiunge "Evento Live" alla sua descrizione
        merged_with: dict[int, object] = {}
        new_evs = []
        for ev in sorted(evs, key=lambda x: x["start"]):
            hit = next((p for p in existing if id(p) not in merged_with and _same_match(p, ev)), None)
            if hit is not None:
                d = hit.find("desc")
                if d is None:
                    d = etree.SubElement(hit, "desc")
                    d.set("lang", "it")
                    d.text = EVENT_DESC
                elif EVENT_DESC not in (d.text or ""):
                    # in testa: i player tagliano le descrizioni lunghe
                    d.text = (EVENT_DESC + " - " + (d.text or "").lstrip()).rstrip(" -")
                merged_with[id(hit)] = hit
                unified += 1
            else:
                new_evs.append(ev)

        # 2) evento non presente: viene aggiunto, e i programmi che si sovrappongono (diversi) vengono rimossi
        keep = []
        for p in existing:
            if id(p) in merged_with:
                keep.append(p)
                continue
            s_, e_ = _parse_ts(p.get("start", "")), _parse_ts(p.get("stop", ""))
            if s_ is not None:
                e_ = e_ or s_
                if any(ev["start"] < e_ and ev["end"] > s_ for ev in new_evs):
                    removed += 1
                    continue
            keep.append(p)

        for ev in new_evs:
            p = etree.Element("programme")
            p.set("start", _fmt_ts(ev["start"], off))
            p.set("stop", _fmt_ts(ev["end"], off))
            p.set("channel", cid)
            t = etree.SubElement(p, "title"); t.set("lang", "it"); t.text = f"{ev['home']} - {ev['away']}"
            if ev["comp"]:
                st = etree.SubElement(p, "sub-title"); st.set("lang", "it"); st.text = ev["comp"]
            d = etree.SubElement(p, "desc"); d.set("lang", "it"); d.text = EVENT_DESC
            c = etree.SubElement(p, "category"); c.set("lang", "it"); c.text = "Sport"
            keep.append(p)
            added += 1
        best_programmes[cid] = keep

    print(f"[eventi] {added} eventi nuovi su {len(events)} canali "
          f"({unified} uniti a programmi EPG esistenti, {removed} programmi sovrapposti rimossi)")
    return added


# ---------------------------------------------------------------------------
# Merge principale
# ---------------------------------------------------------------------------

def merge_epg(epg_dir: str, output_path: str, ref_path: str, matches_path: str | None = None) -> None:
    # 1. Carica alias map + whitelist
    print(f"Caricamento alias da: {ref_path}")
    alias_map, canonical_ids = load_alias_map(ref_path)
    print(f"  {len(alias_map)} alias per {len(canonical_ids)} canali canonici (whitelist).")

    if not canonical_ids:
        print("[ERRORE] Nessun canale trovato in riferimento.txt", file=sys.stderr)
        sys.exit(1)

    # 2. Trova file sorgente
    output_basename = os.path.basename(output_path)
    gz_files = sorted(
        f for f in glob.glob(os.path.join(epg_dir, "*.gz"))
        if os.path.basename(f) != output_basename
    )

    if not gz_files:
        print(f"Nessun file .gz trovato in '{epg_dir}'", file=sys.stderr)
        sys.exit(1)

    print(f"\nTrovati {len(gz_files)} file EPG sorgente.")

    # 3. Parsa tutte le sorgenti
    all_channels: dict = {}
    # per_channel_sources[cid] = lista di (avg_score, prog_list_filtrata, tz_type, fname)
    per_channel_sources: dict[str, list[tuple[float, list, str, str]]] = defaultdict(list)

    for gz_path in gz_files:
        fname = os.path.basename(gz_path)
        print(f"  Parsing: {fname} ... ", end="", flush=True)
        chs, progs = parse_gz_epg(gz_path, alias_map)
        n_ch   = len(chs)
        n_prog = sum(len(v) for v in progs.values())
        print(f"{n_ch} canali, {n_prog} programmi")

        for cid, ch_elem in chs.items():
            if cid not in all_channels:
                all_channels[cid] = ch_elem

        # Conta canali con +0200 rimossi per questa sorgente
        mixed_removed_count = 0

        for cid, prog_list in progs.items():
            if not prog_list:
                continue

            filtered_list, tz_type = filter_timezone(prog_list)

            if not filtered_list:
                continue

            if tz_type == "mixed":
                mixed_removed_count += 1

            avg_score = sum(score_programme(p) for p in filtered_list) / len(filtered_list)
            per_channel_sources[cid].append((avg_score, filtered_list, tz_type, fname))

        if mixed_removed_count > 0:
            print(f"    ↳ [{fname}] canali con +0200 rimossi (mix): {mixed_removed_count}")

    # 4. Per ogni canale scegli la fonte con punteggio medio più alto
    print("\nSelezione sorgente migliore per canale...")

    best_programmes: dict[str, list] = {}
    # Conta quante volte ogni sorgente vince
    wins_per_source: dict[str, int] = defaultdict(int)

    for cid, sources in per_channel_sources.items():
        best_score, best_list, best_tz, best_fname = max(sources, key=lambda x: x[0])
        best_programmes[cid] = best_list
        wins_per_source[best_fname] += 1

    # Stampa riepilogo vincitori
    print("\n── Sorgenti vincitrici ──")
    for fname, count in sorted(wins_per_source.items(), key=lambda x: -x[1]):
        print(f"  {fname}: {count} canali vinti")

    # 4b. Eventi live: sempre presenti, fuori dalle regole di merge
    if matches_path:
        inject_events(best_programmes, load_events(matches_path, alias_map))

    # 5. Aggiungi <channel> sintetici per canali senza elemento <channel>
    for cid in canonical_ids:
        if cid in best_programmes and cid not in all_channels:
            ch = etree.Element("channel")
            ch.set("id", cid)
            dn = etree.SubElement(ch, "display-name")
            dn.text = cid
            all_channels[cid] = ch

    # 6. Costruisci l'XML finale
    tv_root = etree.Element("tv")
    tv_root.set("generator-info-name", "merge_epg.py")

    active_cids = sorted(cid for cid in all_channels if cid in best_programmes)
    for cid in active_cids:
        ch = all_channels[cid]
        ch.set("id", cid)
        if ch.find("display-name") is None:
            dn = etree.SubElement(ch, "display-name")
            dn.text = cid
        tv_root.append(ch)

    all_progs = [p for progs in best_programmes.values() for p in progs]
    all_progs.sort(key=lambda p: p.get("start", ""))
    for prog in all_progs:
        tv_root.append(prog)

    total_ch   = len(active_cids)
    total_prog = len(all_progs)

    missing = sorted(canonical_ids - set(best_programmes.keys()))
    if missing:
        print(f"\n[INFO] {len(missing)} canali senza programmi in nessuna sorgente:")
        for m in missing:
            print(f"  - {m}")

    print(f"\nEPG unificato: {total_ch} canali, {total_prog} programmi.")

    # 7. Scrivi output gz
    xml_bytes = etree.tostring(
        tv_root, xml_declaration=True, encoding="UTF-8", pretty_print=False
    )
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with gzip.open(output_path, "wb", compresslevel=6) as f:
        f.write(xml_bytes)

    size_kb = os.path.getsize(output_path) // 1024
    print(f"Output: {output_path} ({size_kb} KB)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("epg_dir", nargs="?", default="epg")
    ap.add_argument("output", nargs="?", default="epg/merged_epg.xml.gz")
    ap.add_argument("ref", nargs="?", default="riferimento.txt")
    ap.add_argument("--matches", default="channel_matches.json", help="JSON dei palinsesti canali")
    ap.add_argument("--no-scrape", action="store_true", help="non scaricare, usa il JSON esistente")
    ap.add_argument("--force", action="store_true", help="ignora every_days e backoff (resta MAX_PER_RUN)")
    ap.add_argument("--all", action="store_true", help="TEST: scarica tutti i canali in un colpo")
    args = ap.parse_args()

    scrape_errors = 0
    if not args.no_scrape:
        try:
            scrape_errors = scrape_channels(args.matches, args.force, args.all)
        except Exception as e:   # lo scraping non deve mai bloccare l'EPG
            scrape_errors = 1
            print(f"[scrape] ERRORE imprevisto: {e!r}", file=sys.stderr)
    if scrape_errors:
        print(f"::warning::scraper palinsesti: {scrape_errors} errori (EPG creato comunque)")

    merge_epg(args.epg_dir, args.output, args.ref, args.matches)
