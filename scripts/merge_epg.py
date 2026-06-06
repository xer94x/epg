#!/usr/bin/env python3
"""
merge_epg.py
Legge tutti i file .gz EPG dalla cartella epg/, normalizza i channel id
leggendo la mappa degli alias da riferimento.txt, e produce epg/merged_epg.xml.gz
scegliendo per ogni canale la fonte con più informazioni.

IMPORTANTE: vengono inclusi SOLO i canali presenti in riferimento.txt.
Qualsiasi canale non mappato viene scartato sia come <channel> che come <programme>.

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
    """
    Legge riferimento.txt e restituisce:
      - alias_map:      alias_normalizzato → channel_id_canonico
      - canonical_ids:  set dei soli id canonici (whitelist)

    Formato atteso:
        NomeCanaleUfficiale:       ← termina con ':'  → canonical id
            Alias 1                ← righe successive → alias
            Alias 2
                                   ← riga vuota separa i blocchi
    """
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
            # Il canonical id è alias di se stesso
            alias_map[_norm(current_id)] = current_id
            continue

        if current_id is not None and "(no additional aliases)" not in line:
            alias_map[_norm(line)] = current_id

    return alias_map, canonical_ids


def _norm(s: str) -> str:
    """Normalizza una stringa per il confronto: strip + lower."""
    return s.strip().lower()


# Suffissi da rimuovere nel fallback matching
_STRIP_SUFFIXES = (
    " hd", " fhd", " sd", " full hd", " 4k",
    ".hd", ".sd", ".fhd",
    " hd.it", " sd.it", ".it",
)


def resolve_channel_id(raw_id: str, alias_map: dict[str, str]) -> str | None:
    """
    Risolve raw_id al canonical id tramite la mappa.
    Restituisce None se non trovato (canale da scartare).

    Strategie (in ordine):
      1. Match diretto
      2. Rimozione .it finale
      3. Rimozione suffissi HD/FHD/SD/4K
      4. Combinazione: suffix + .it
      5. Nessuna corrispondenza → None
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
            if k3.endswith(".it"):
                k3b = k3[:-3].rstrip(".")
                if k3b in alias_map:
                    return alias_map[k3b]

    return None  # non in whitelist → da scartare


# ---------------------------------------------------------------------------
# Scoring dei programmi
# ---------------------------------------------------------------------------

def score_programme(prog_elem) -> int:
    """
    Assegna un punteggio a un elemento <programme> in base alla ricchezza.
    Più info → punteggio più alto → quella fonte viene preferita.
    """
    score = 1
    for child in prog_elem:
        tag = child.tag
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
# Parsing di un singolo file EPG — supporta .gz e XML plain
# ---------------------------------------------------------------------------

def _read_file_bytes(filepath: str) -> bytes | None:
    """
    Legge un file tentando prima come gzip, poi come testo plain.
    Molte sorgenti vengono salvate come .gz ma contengono XML non compresso
    (es. quando curl segue un redirect HTTP che già decomprime il gzip).
    """
    # Tentativo 1: gzip
    try:
        with gzip.open(filepath, "rb") as f:
            data = f.read()
        # Sanity check: deve sembrare XML
        stripped = data.lstrip()
        if stripped.startswith(b"<") or stripped.startswith(b"<?"):
            return data
        # Contenuto non XML dopo decompressione → prova plain
    except Exception:
        pass

    # Tentativo 2: file plain (già XML non compresso)
    try:
        with open(filepath, "rb") as f:
            data = f.read()
        stripped = data.lstrip()
        if stripped.startswith(b"<") or stripped.startswith(b"<?"):
            return data
    except Exception as e:
        print(f"  [WARN] Impossibile leggere {filepath}: {e}", file=sys.stderr)

    return None


def parse_gz_epg(
    filepath: str,
    alias_map: dict[str, str],
) -> tuple[dict, dict]:
    """
    Parsa un file EPG e restituisce:
      channels:   canonical_id → <channel> element
      programmes: canonical_id → list of <programme> elements
    Solo i canali presenti nella alias_map (whitelist) vengono inclusi.
    """
    channels: dict[str, object] = {}
    programmes: dict[str, list] = defaultdict(list)

    data = _read_file_bytes(filepath)
    if data is None:
        return channels, programmes

    try:
        root = etree.fromstring(data)
    except etree.XMLSyntaxError:
        try:
            parser = etree.XMLParser(recover=True)
            root = etree.fromstring(data, parser)
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
        fname = os.path.basename(filepath)
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
    all_channels: dict[str, object] = {}
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
                avg_score = sum(score_programme(p) for p in prog_list) / len(prog_list)
                source_buckets[cid].append((avg_score, prog_list))

    # 4. Per ogni canale scegli la fonte con punteggio più alto
    best_programmes: dict[str, list] = {}
    for cid, sources in source_buckets.items():
        _, best_list = max(sources, key=lambda x: x[0])
        best_programmes[cid] = best_list

    # 5. Aggiungi <channel> per i canali della whitelist che hanno programmi
    #    ma non avevano un elemento <channel> in nessuna sorgente
    for cid in canonical_ids:
        if cid in best_programmes and cid not in all_channels:
            ch = etree.Element("channel")
            ch.set("id", cid)
            dn = etree.SubElement(ch, "display-name")
            dn.text = cid
            all_channels[cid] = ch

    # 6. Costruisci l'XML finale (solo canali con programmi)
    tv_root = etree.Element("tv")
    tv_root.set("generator-info-name", "merge_epg.py")

    # Canali con almeno un programma, ordinati per nome
    active_cids = sorted(cid for cid in all_channels if cid in best_programmes)
    for cid in active_cids:
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

    total_ch = len(active_cids)
    total_prog = len(all_progs)

    # Canali whitelist senza nessun programma
    missing = sorted(canonical_ids - set(best_programmes.keys()))
    if missing:
        print(f"\n[INFO] {len(missing)} canali della whitelist senza programmi in nessuna sorgente:")
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
