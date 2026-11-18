"""Canonical identifier normalization, legal status mapping, and cross-portal deduplication."""
from __future__ import annotations

import json
import re
from typing import Optional

_JENIS_MAP = {
    "UNDANG-UNDANG": "UU",
    "UNDANG UNDANG": "UU",
    "UU": "UU",
    "PERATURAN PEMERINTAH PENGGANTI UNDANG-UNDANG": "PERPPU",
    "PERPPU": "PERPPU",
    "PERATURAN PEMERINTAH": "PP",
    "PP": "PP",
    "PERATURAN PRESIDEN": "PERPRES",
    "PERPRES": "PERPRES",
    "KEPUTUSAN PRESIDEN": "KEPPRES",
    "KEPPRES": "KEPPRES",
    "INSTRUKSI PRESIDEN": "INPRES",
    "INPRES": "INPRES",
    "PERATURAN OTORITAS JASA KEUANGAN": "POJK",
    "POJK": "POJK",
    "SURAT EDARAN OTORITAS JASA KEUANGAN": "SEOJK",
    "SEOJK": "SEOJK",
    "PERATURAN BANK INDONESIA": "PBI",
    "PBI": "PBI",
    "PERATURAN MENTERI KEUANGAN": "PMK",
    "PMK": "PMK",
    "KEPUTUSAN MENTERI KEUANGAN": "KMK",
    "KMK": "KMK",
    "PERATURAN MENTERI KETENAGAKERJAAN": "PERMENAKER",
    "PERMENAKER": "PERMENAKER",
    "KEPUTUSAN MENTERI KETENAGAKERJAAN": "KEPMENAKER",
    "KEPMENAKER": "KEPMENAKER",
    "PERATURAN MENTERI PERDAGANGAN": "PERMENDAG",
    "PERMENDAG": "PERMENDAG",
    "PERATURAN MENTERI KOMUNIKASI DAN INFORMATIKA": "PERMENKOMINFO",
    "PERMENKOMINFO": "PERMENKOMINFO",
    "PERATURAN MENTERI LINGKUNGAN HIDUP DAN KEHUTANAN": "PERMENLHK",
    "PERMENLHK": "PERMENLHK",
    "PERATURAN MAHKAMAH AGUNG": "PERMA",
    "PERMA": "PERMA",
    "PERATURAN LEMBAGA PENJAMIN SIMPANAN": "PLPS",
    "PLPS": "PLPS",
    "PERATURAN KPPU": "PERKPPU",
    "PERKPPU": "PERKPPU",
    "PERATURAN BKPM": "PERBKPM",
    "PERBKPM": "PERBKPM",
    "PERATURAN LKPP": "PERLKPP",
    "PERLKPP": "PERLKPP",
    "PERATURAN BSSN": "PERBSSN",
    "PERBSSN": "PERBSSN",
}


def normalize_jenis(raw: str | None) -> str:
    txt = re.sub(r"\s+", " ", (raw or "").strip().upper())
    if not txt:
        return "PERATURAN"
    if txt in _JENIS_MAP:
        return _JENIS_MAP[txt]
    for long_name, abbr in _JENIS_MAP.items():
        if long_name in txt:
            return abbr
    return txt.replace(" ", "_")[:24]


def normalize_nomor_tahun(nomor_raw: str | None, tahun_hint: Optional[int] = None) -> tuple[str, Optional[int]]:
    txt = re.sub(r"\s+", " ", (nomor_raw or "").strip())
    tahun = tahun_hint

    m_year = re.search(r"(?:TAHUN\s+|/)((?:19|20)\d{2})\b", txt, re.IGNORECASE)
    if m_year and tahun is None:
        tahun = int(m_year.group(1))

    m_num = re.search(r"\b(?:NOMOR|NO\.?)\s*([0-9][A-Z0-9./-]*)\b", txt, re.IGNORECASE)
    if m_num:
        num = m_num.group(1).strip(" .-/")
    else:
        m_slash = re.match(r"^([A-Z0-9.-]+)\s*/\s*(?:19|20)\d{2}", txt, re.IGNORECASE)
        num = m_slash.group(1).strip() if m_slash else txt

    num = re.sub(r"\s+", "", num.upper())
    # Strip trailing /YYYY if already captured in tahun
    if tahun is not None:
        num = re.sub(rf"/{tahun}$", "", num)
    return num or "0", tahun


def build_canonical_key(jenis: str | None, nomor: str | None, tahun: Optional[int] = None) -> str:
    norm_jenis = normalize_jenis(jenis)
    norm_nomor, norm_tahun = normalize_nomor_tahun(nomor, tahun)
    if norm_tahun:
        return f"{norm_jenis}|{norm_nomor}|{norm_tahun}"
    return f"{norm_jenis}|{norm_nomor}"


def normalize_legal_status(raw: str | None) -> str:
    txt = (raw or "").strip().lower()
    if any(k in txt for k in ("dicabut sebagian", "mencabut sebagian", "partially repealed")):
        return "dicabut_sebagian"
    if any(k in txt for k in ("dicabut", "tidak berlaku", "repealed")):
        return "dicabut"
    if any(k in txt for k in ("diubah", "perubahan", "amended")):
        return "diubah"
    if any(k in txt for k in ("berlaku", "aktif", "active")):
        return "berlaku"
    return "berlaku" if not txt else "unknown"


def merge_or_replace_regulation(conn, item: dict, on_duplicate: str = "skip") -> tuple[str, int, bool]:
    """Upsert or deduplicate a scraped regulation by canonical_key.

    Returns (outcome, reg_id, is_duplicate):
      - ('inserted', reg_id, False)
      - ('duplicate_skipped', reg_id, True)
      - ('replaced', reg_id, True)
    """
    jenis = normalize_jenis(item.get("jenis"))
    norm_nomor, tahun = normalize_nomor_tahun(item.get("nomor"), item.get("tahun"))
    canonical_key = build_canonical_key(jenis, norm_nomor, tahun)
    portal = item.get("source_key") or "unknown"
    detail_url = item.get("detail_url") or ""
    download_url = item.get("download_url") or ""
    legal_status = normalize_legal_status(item.get("legal_status") or item.get("status"))
    display_status = {
        "dicabut": "Dicabut",
        "dicabut_sebagian": "Dicabut Sebagian",
        "diubah": "Diubah",
    }.get(legal_status, "Berlaku")

    existing = conn.execute(
        "SELECT id, source_portals, source_urls, legal_status "
        "FROM regulations WHERE canonical_key = %s OR detail_url = %s LIMIT 1",
        (canonical_key, detail_url),
    ).fetchone()

    if existing:
        reg_id = existing["id"]
        portals = list(existing.get("source_portals") or [])
        if portal not in portals:
            portals.append(portal)
        urls = dict(existing.get("source_urls") or {})
        if detail_url:
            urls[portal] = detail_url

        if on_duplicate == "replace":
            conn.execute(
                "UPDATE regulations SET judul = COALESCE(%s, judul), "
                "nomor = COALESCE(%s, nomor), jenis = COALESCE(%s, jenis), "
                "sektor = COALESCE(%s, sektor), status = %s, legal_status = %s, "
                "download_url = COALESCE(NULLIF(%s, ''), download_url), "
                "tahun = COALESCE(%s, tahun), tentang = COALESCE(%s, tentang), "
                "source_portals = %s::jsonb, source_urls = %s::jsonb, "
                "klasifikasi = 'Umum' "
                "WHERE id = %s",
                (
                    item.get("judul"),
                    item.get("nomor"),
                    jenis,
                    item.get("sektor"),
                    display_status,
                    legal_status,
                    download_url,
                    tahun,
                    item.get("tentang") or item.get("judul"),
                    json.dumps(portals),
                    json.dumps(urls),
                    reg_id,
                ),
            )
            conn.execute("DELETE FROM chunks WHERE doc_id = %s", (str(reg_id),))
            return "replaced", reg_id, True

        # Default: skip redundant chunking/embedding while enriching provenance
        new_legal = legal_status if legal_status in ("dicabut", "dicabut_sebagian", "diubah") else existing.get("legal_status", "berlaku")
        conn.execute(
            "UPDATE regulations SET source_portals = %s::jsonb, source_urls = %s::jsonb, "
            "legal_status = %s WHERE id = %s",
            (json.dumps(portals), json.dumps(urls), new_legal, reg_id),
        )
        return "duplicate_skipped", reg_id, True

    row = conn.execute(
        "INSERT INTO regulations "
        "(domain, judul, nomor, jenis, sektor, status, detail_url, download_url, "
        "local_path, klasifikasi, canonical_key, legal_status, source_portals, "
        "source_urls, tahun, tentang) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'Umum', %s, %s, %s::jsonb, %s::jsonb, %s, %s) "
        "RETURNING id",
        (
            portal.upper(),
            item.get("judul") or f"{jenis} {norm_nomor}",
            item.get("nomor") or norm_nomor,
            jenis,
            item.get("sektor") or item.get("domain_group") or "Nasional",
            display_status,
            detail_url or f"canonical://{canonical_key}",
            download_url,
            item.get("local_path") or "",
            canonical_key,
            legal_status,
            json.dumps([portal]),
            json.dumps({portal: detail_url} if detail_url else {}),
            tahun,
            item.get("tentang") or item.get("judul"),
        ),
    ).fetchone()
    return "inserted", row["id"], False
