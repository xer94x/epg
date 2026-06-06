#!/usr/bin/env python3
"""
merge_epg.py
Legge tutti i file .gz EPG dalla cartella epg/, normalizza i channel id
leggendo la mappa degli alias da riferimento.txt, e produce epg/merged_epg.xml.gz
scegliendo per ogni canale la fonte con più informazioni.

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
# Parsing di riferimento.txt  →  ALIAS_MAP  +  CANONICAL_IDS
# ---------------------------------------------------------------------------

def load_alias_map(ref_path: str) -> dict[str, str]:
    """
    Legge riferimento.txt e restituisce un dict
        alias_normalizzato → channel_id_canonico
    
    Formato atteso:
        NomeCanaleUfficiale:       ← riga che termina con ':'  → diventa il canonical id
            Alias 1                ← righe successive (non vuote) → alias di quel canale
            Alias 2
                                   ← riga vuota separa i blocchi
        AltroCanale:
            ...
    """
    alias_map: dict[str, str] = {}

    try:
        with open(ref_path, "r", encoding="utf-8-sig") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"[WARN] {ref_path} non trovato — nessun alias applicato.", file=sys.stderr)
        return alias_map

    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    current_id: str | None = None

    for raw_line in lines:
        line = raw_line.strip()

        if not line:
            continue

        # Riga canale: termina con ':' e non contiene '(' (es. "(no additional aliases)")
        if line.endswith(":") and "(" not in line:
            current_id = line[:-1].strip()
            # Il canonical id stesso è anche un alias di se stesso
            alias_map[_norm(current_id)] = current_id
            continue

        # Riga alias (qualsiasi altra riga non vuota dentro un blocco)
        if current_id is not None and "(no additional aliases)" not in line:
            alias_map[_norm(line)] = current_id

    return alias_map


def _norm(s: str) -> str:
    """Normalizza una stringa per il confronto: strip + lower."""
    return s.strip().lower()


# Suffissi da rimuovere nel fallback matching
_STRIP_SUFFIXES = (
    " hd", " fhd", " sd", " full hd", " 4k",
    ".hd", ".sd", ".fhd",
    " hd.it", " sd.it", ".it",
)


def resolve_channel_id(raw_id: str, alias_map: dict[str, str]) -> str:
    """
    Risolve raw_id (da <channel id="..."> o <programme channel="...">) 
    al canonical id tramite la mappa.
    Strategie (in ordine):
      1. Match diretto
      2. Rimozione .it finale
      3. Rimozione suffissi HD/FHD/SD/4K
      4. Combinazione: suffix + .it
      5. Fallback: raw_id invariato
    """
    key = _norm(raw_id)

    if key in alias_map:
        return alias_map[key]

    # Prova senza trailing .it
    key2 = key
    if key2.endswith(".it"):
        key2 = key2[:-3].rstrip(".")
        if key2 in alias_map:
            return alias_map[key2]

    # Prova rimuovendo suffissi comuni
    for suffix in _STRIP_SUFFIXES:
        if key.endswith(suffix):
            k3 = key[: -len(suffix)].strip(" .")
            if k3 in alias_map:
                return alias_map[k3]
            # Prova anche senza .it residuo
            if k3.endswith(".it"):
                k3b = k3[:-3].rstrip(".")
                if k3b in alias_map:
                    return alias_map[k3b]

    # Fallback
    return raw_id


# ---------------------------------------------------------------------------
# Scoring dei programmi
# ---------------------------------------------------------------------------

def score_programme(prog_elem) -> int:
    """
    Assegna un punteggio a un elemento <programme> in base alla ricchezza.
    Più info → punteggio più alto → quella fonte viene preferita.
    """
    score = 1  # base: il programma esiste
    for child in prog_elem:
        tag = child.tag
        text = (child.text or "").strip()
        if text:
            score += 1
        if tag == "desc":
            score += 8           # descrizione è la cosa più importante
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
# Parsing di un singolo file EPG .gz
# ---------------------------------------------------------------------------

def parse_gz_epg(
    filepath: str, alias_map: dict[str, str]
) -> tuple[dict, dict]:
    """
    Parsa un file EPG .gz e restituisce:
      channels:   canonical_id → <channel> element (primo trovato)
      programmes: canonical_id → list of <programme> elements
    """
    channels: dict[str, object] = {}
    programmes: dict[str, list] = defaultdict(list)

    try:
        with gzip.open(filepath, "rb") as f:
            data = f.read()
    except Exception as e:
        print(f"  [WARN] Impossibile aprire {filepath}: {e}", file=sys.stderr)
        return channels, programmes

    # Alcuni feed hanno encoding dichiarato errato; prova con recover=True
    try:
        root = etree.fromstring(data)
    except etree.XMLSyntaxError:
        try:
            parser = etree.XMLParser(recover=True)
            root = etree.fromstring(data, parser)
        except Exception as e:
            print(f"  [WARN] XML non valido in {filepath}: {e}", file=sys.stderr)
            return channels, programmes

    for ch in root.findall("channel"):
        raw_id = ch.get("id", "").strip()
        if not raw_id:
            continue
        cid = resolve_channel_id(raw_id, alias_map)
        ch.set("id", cid)
        # Mantieni il primo <channel> trovato per questo id (di solito è già buono)
        if cid not in channels:
            channels[cid] = ch

    for prog in root.findall("programme"):
        raw_ch = prog.get("channel", "").strip()
        if not raw_ch:
            continue
        cid = resolve_channel_id(raw_ch, alias_map)
        prog.set("channel", cid)
        programmes[cid].append(prog)

    return channels, programmes


# ---------------------------------------------------------------------------
# Merge principale
# ---------------------------------------------------------------------------

def merge_epg(epg_dir: str, output_path: str, ref_path: str) -> None:
    # 1. Carica la mappa alias
    print(f"Caricamento alias da: {ref_path}")
    alias_map = load_alias_map(ref_path)
    print(f"  {len(alias_map)} alias caricati per {len(set(alias_map.values()))} canali canonici.")

    # 2. Trova i file sorgente (escludi l'output se già presente)
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
    all_channels: dict[str, object] = {}
    # canonical_id → lista di (score_totale, [<programme> elements])
    source_buckets: dict[str, list[tuple[int, list]]] = defaultdict(list)

    for gz_path in gz_files:
        fname = os.path.basename(gz_path)
        print(f"  Parsing: {fname} ... ", end="", flush=True)
        chs, progs = parse_gz_epg(gz_path, alias_map)
        n_ch = len(chs)
        n_prog = sum(len(v) for v in progs.values())
        print(f"{n_ch} canali, {n_prog} programmi")

        for cid, ch_elem in chs.items():
            if cid not in all_channels:
                all_channels[cid] = ch_elem

        for cid, prog_list in progs.items():
            if prog_list:
                total_score = sum(score_programme(p) for p in prog_list)
                source_buckets[cid].append((total_score, prog_list))

    # 4. Per ogni canale scegli la fonte con punteggio più alto
    best_programmes: dict[str, list] = {}
    for cid, sources in source_buckets.items():
        _, best_list = max(sources, key=lambda x: x[0])
        best_programmes[cid] = best_list

    # 5. Costruisci l'XML finale
    tv_root = etree.Element("tv")
    tv_root.set("generator-info-name", "merge_epg.py")

    # Canali ordinati
    for cid in sorted(all_channels.keys()):
        ch = all_channels[cid]
        ch.set("id", cid)
        if ch.find("display-name") is None:
            dn = etree.SubElement(ch, "display-name")
            dn.text = cid
        tv_root.append(ch)

    # Programmi ordinati per start time
    all_progs = [p for progs in best_programmes.values() for p in progs]
    all_progs.sort(key=lambda p: p.get("start", ""))
    for prog in all_progs:
        tv_root.append(prog)

    total_ch = len(all_channels)
    total_prog = len(all_progs)
    print(f"\nEPG unificato: {total_ch} canali, {total_prog} programmi.")

    # 6. Scrivi output gz
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
    epg_dir   = sys.argv[1] if len(sys.argv) > 1 else "epg"
    output    = sys.argv[2] if len(sys.argv) > 2 else "epg/merged_epg.xml.gz"
    ref_path  = sys.argv[3] if len(sys.argv) > 3 else "riferimento.txt"
    merge_epg(epg_dir, output, ref_path)
