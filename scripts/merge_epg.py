#!/usr/bin/env python3
"""
merge_epg.py
Legge tutti i file .gz EPG dalla cartella epg/, normalizza i channel id
leggendo la mappa degli alias da riferimento.txt, e produce epg/merged_epg.xml.gz
scegliendo per ogni canale la fonte con più informazioni.

IMPORTANTE: vengono inclusi SOLO i canali presenti in riferimento.txt.
Qualsiasi canale non mappato viene scartato sia come <channel> che come <programme>.

De-duplicazione cross-source: quando una sorgente replica lo stesso programma
per più fusi orari (senza offset), il confronto tra tutte le sorgenti determina
per votazione quale orario è quello corretto.

Uso: python merge_epg.py [epg_dir] [output_gz] [riferimento.txt]
     default: epg/  epg/merged_epg.xml.gz  riferimento.txt
"""

import os
import gzip
import glob
import sys
import re
from collections import defaultdict
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

def parse_epg_ts(ts: str) -> tuple[str, int]:
    """
    Dato un timestamp EPG restituisce:
      - day_key:    stringa "YYYYMMDD" (giorno del programma in UTC se c'è offset,
                    altrimenti as-is)
      - start_min:  minuti dalla mezzanotte (normalizzati in UTC se c'è offset,
                    altrimenti as-is)
    Usato per raggruppare i programmi per giorno+titolo e trovare duplicati.
    """
    ts = ts.strip()
    m = re.match(r'^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})\d{2}\s*([+-]\d{4})?', ts)
    if not m:
        return ("00000000", 0)

    yy, mo, dd, hh, mn = m.group(1), m.group(2), m.group(3), int(m.group(4)), int(m.group(5))
    off_str = m.group(6)

    total_min = hh * 60 + mn

    if off_str:
        sign = 1 if off_str[0] == "+" else -1
        oh = int(off_str[1:3])
        om = int(off_str[3:5])
        total_min -= sign * (oh * 60 + om)   # porta in UTC

        # Aggiusta il giorno se lo scorrimento UTC attraversa la mezzanotte
        # (semplificato: non gestiamo cambio mese/anno, sufficiente per dedup)
        day_offset = 0
        if total_min < 0:
            total_min += 1440
            day_offset = -1
        elif total_min >= 1440:
            total_min -= 1440
            day_offset = 1

        if day_offset != 0:
            # Ricalcola la data (grezzo, senza librerie)
            days_in_month = [0,31,28,31,30,31,30,31,31,30,31,30,31]
            d = int(dd) + day_offset
            mo_int = int(mo)
            yy_int = int(yy)
            if d < 1:
                mo_int -= 1
                if mo_int < 1:
                    mo_int = 12; yy_int -= 1
                d = days_in_month[mo_int]
            elif d > days_in_month[mo_int]:
                d = 1; mo_int += 1
                if mo_int > 12:
                    mo_int = 1; yy_int += 1
            day_key = f"{yy_int:04d}{mo_int:02d}{d:02d}"
        else:
            day_key = f"{yy}{mo}{dd}"
    else:
        # Nessun offset: usiamo il valore as-is (non sappiamo il fuso)
        day_key = f"{yy}{mo}{dd}"

    return (day_key, total_min)


def ts_has_offset(ts: str) -> bool:
    """True se il timestamp contiene un offset esplicito (+HHMM o -HHMM)."""
    return bool(re.search(r'[+-]\d{4}', ts.strip()))


def title_of(prog) -> str:
    t = prog.find("title")
    return (t.text or "").strip().lower() if t is not None else ""


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
# Cross-source voting deduplication
# ---------------------------------------------------------------------------

# Tolleranza in minuti: due start time sono "lo stesso slot" se distano ≤ SLOT_TOL
SLOT_TOL = 5


def _group_key(prog) -> tuple[str, str]:
    """
    Chiave di raggruppamento: (giorno_UTC_o_locale, titolo_normalizzato).
    Due programmi con la stessa chiave sono candidati duplicati.
    """
    day, _ = parse_epg_ts(prog.get("start", ""))
    return (day, title_of(prog))


def cross_source_deduplicate(
    source_prog_lists: list[list],   # una lista per sorgente, già filtrate per canale
) -> tuple[list, int]:
    """
    De-duplicazione cross-source per un singolo canale.

    Algoritmo:
    1. Raccoglie tutti i programmi da tutte le sorgenti con il loro start_min UTC/locale.
    2. Raggruppa per (giorno, titolo).
    3. Per ogni gruppo con più di un orario distinto (= duplicati fuso orario):
       - Conta quante sorgenti usano ciascun orario (voting).
       - L'orario con più voti è quello "reale".
       - In caso di parità vince la sorgente con score medio più alto.
    4. Nella lista finale vengono mantenuti solo i programmi con l'orario vincente,
       scegliendo tra essi quello con score_programme più alto (più ricco).

    Restituisce (lista_finale, n_duplicati_rimossi).
    """

    # Struttura: per ogni (day, title) → lista di (start_min, prog, source_idx, has_offset)
    groups: dict[tuple, list] = defaultdict(list)

    for src_idx, prog_list in enumerate(source_prog_lists):
        for prog in prog_list:
            ts  = prog.get("start", "")
            day, smin = parse_epg_ts(ts)
            has_off   = ts_has_offset(ts)
            key = _group_key(prog)
            groups[key].append((smin, prog, src_idx, has_off))

    final_progs: list = []
    total_removed = 0

    for (day, title), entries in groups.items():
        if len(entries) == 1:
            final_progs.append(entries[0][1])
            continue

        # ── Raggruppa per "slot orario" (start_min a ±SLOT_TOL minuti) ──
        # Ogni slot è una lista di entry che condividono lo stesso orario.
        slots: list[list] = []
        used = [False] * len(entries)

        for i, (smin_i, prog_i, src_i, off_i) in enumerate(entries):
            if used[i]:
                continue
            slot = [entries[i]]
            used[i] = True
            for j in range(i + 1, len(entries)):
                if used[j]:
                    continue
                smin_j = entries[j][0]
                if abs(smin_i - smin_j) <= SLOT_TOL:
                    slot.append(entries[j])
                    used[j] = True
            slots.append(slot)

        if len(slots) == 1:
            # Tutti nello stesso slot: nessun duplicato fuso orario,
            # tieni quello con score più alto
            best = max(slots[0], key=lambda e: score_programme(e[1]))
            final_progs.append(best[1])
            total_removed += len(slots[0]) - 1
            continue

        # ── Voting: quale slot è quello "reale"? ──
        #
        # Regola 1 (priorità massima): se ALMENO UNO slot ha tutti i suoi
        #   programmi con offset esplicito, quegli orari sono già UTC → sono
        #   affidabili. Tra gli slot con offset, scegliamo quello con più voti.
        #
        # Regola 2: se nessuno slot ha offset, contiamo quante sorgenti DISTINTE
        #   usano ciascun orario. L'orario più "votato" dalle sorgenti è il reale.
        #
        # Regola 3 (spareggio): a parità di voti, vince lo slot con score medio
        #   più alto (programma più ricco di informazioni).

        def slot_votes(slot):
            """Numero di sorgenti distinte che usano questo slot."""
            return len(set(e[2] for e in slot))

        def slot_has_offset(slot):
            return any(e[3] for e in slot)

        def slot_avg_score(slot):
            return sum(score_programme(e[1]) for e in slot) / len(slot)

        slots_with_offset = [s for s in slots if slot_has_offset(s)]

        if slots_with_offset:
            # Regola 1: preferiamo gli slot con offset esplicito
            candidate_slots = slots_with_offset
        else:
            # Regola 2: voting puro tra tutti gli slot
            candidate_slots = slots

        winning_slot = max(
            candidate_slots,
            key=lambda s: (slot_votes(s), slot_avg_score(s))
        )

        # Dal winning slot, tieni il programma con score più alto
        best = max(winning_slot, key=lambda e: score_programme(e[1]))
        final_progs.append(best[1])

        # Tutto il resto (altri slot + duplicati nello stesso slot) è rimosso
        total_removed += len(entries) - 1

    return final_progs, total_removed


# ---------------------------------------------------------------------------
# Merge principale
# ---------------------------------------------------------------------------

def merge_epg(epg_dir: str, output_path: str, ref_path: str) -> None:
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
    #    Teniamo TUTTE le liste per canale (una per sorgente) per il cross-voting.
    all_channels: dict = {}
    # per_channel_sources[cid] = lista di (avg_score, prog_list, source_idx)
    per_channel_sources: dict[str, list[tuple[float, list, int]]] = defaultdict(list)

    for src_idx, gz_path in enumerate(gz_files):
        fname = os.path.basename(gz_path)
        print(f"  Parsing: {fname} ... ", end="", flush=True)
        chs, progs = parse_gz_epg(gz_path, alias_map)
        n_ch   = len(chs)
        n_prog = sum(len(v) for v in progs.values())
        print(f"{n_ch} canali, {n_prog} programmi")

        for cid, ch_elem in chs.items():
            if cid not in all_channels:
                all_channels[cid] = ch_elem

        for cid, prog_list in progs.items():
            if prog_list:
                avg_score = sum(score_programme(p) for p in prog_list) / len(prog_list)
                per_channel_sources[cid].append((avg_score, prog_list, src_idx))

    # 4. Cross-source deduplication per ogni canale
    print("\nDe-duplicazione cross-source...")
    best_programmes: dict[str, list] = {}
    total_removed = 0

    for cid, sources in per_channel_sources.items():
        # Passiamo tutte le liste sorgente al deduplicatore
        all_lists = [prog_list for (_, prog_list, _) in sources]
        deduped, removed = cross_source_deduplicate(all_lists)
        best_programmes[cid] = deduped
        if removed:
            print(f"  {cid}: rimossi {removed} duplicati")
            total_removed += removed

    if total_removed:
        print(f"  → Totale duplicati rimossi: {total_removed}")
    else:
        print("  Nessun duplicato rilevato.")

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
    epg_dir  = sys.argv[1] if len(sys.argv) > 1 else "epg"
    output   = sys.argv[2] if len(sys.argv) > 2 else "epg/merged_epg.xml.gz"
    ref_path = sys.argv[3] if len(sys.argv) > 3 else "riferimento.txt"
    merge_epg(epg_dir, output, ref_path)

