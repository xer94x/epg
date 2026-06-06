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

