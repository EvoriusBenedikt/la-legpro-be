"""Universal 14-Category Indonesian Legal Taxonomy & Deterministic Classifier."""
from __future__ import annotations

import re
from typing import Optional

from services import pg_service

LEGAL_CATEGORIES: tuple[dict, ...] = (
    {
        "code": "CIVIL_CONTRACT",
        "label_en": "Civil & General Contract",
        "label_id": "Hukum Perdata & Perikatan",
        "bpk_keywords": ["perdata", "perikatan", "kontrak", "meterai"],
    },
    {
        "code": "CORPORATE_INVESTMENT",
        "label_en": "Corporate, M&A & Investment",
        "label_id": "Perusahaan, M&A & Penanaman Modal",
        "bpk_keywords": ["perseroan terbatas", "penanaman modal", "perizinan berusaha"],
    },
    {
        "code": "FINANCE_BANKING",
        "label_en": "Finance, Banking & Fintech",
        "label_id": "Keuangan, Perbankan & Fintech",
        "bpk_keywords": ["perbankan", "pasar modal", "jasa keuangan", "asuransi"],
    },
    {
        "code": "TECHNOLOGY_DIGITAL",
        "label_en": "Technology, Cyber & Data Privacy",
        "label_id": "Teknologi, Siber & Perlindungan Data",
        "bpk_keywords": ["informasi dan transaksi elektronik", "pelindungan data pribadi", "telekomunikasi", "siber"],
    },
    {
        "code": "PROPERTY_AGRARIAN",
        "label_en": "Property, Land & Construction",
        "label_id": "Properti, Pertanahan & Konstruksi",
        "bpk_keywords": ["pertanahan", "agraria", "perumahan", "jasa konstruksi", "rumah susun"],
    },
    {
        "code": "LABOR_MANPOWER",
        "label_en": "Labor & Industrial Relations",
        "label_id": "Ketenagakerjaan & Hubungan Industrial",
        "bpk_keywords": ["ketenagakerjaan", "pengupahan", "jaminan sosial", "tenaga kerja"],
    },
    {
        "code": "TAX_CUSTOMS",
        "label_en": "Taxation & Customs",
        "label_id": "Perpajakan & Kepabeanan",
        "bpk_keywords": ["pajak", "kepabeanan", "cukai", "bea masuk", "pnbp"],
    },
    {
        "code": "ENERGY_MINING_ENV",
        "label_en": "Energy, Mining & Environment",
        "label_id": "Energi, Pertambangan & Lingkungan",
        "bpk_keywords": ["pertambangan", "lingkungan hidup", "ketenagalistrikan", "minyak dan gas"],
    },
    {
        "code": "IP_MEDIA",
        "label_en": "Intellectual Property & Media",
        "label_id": "Kekayaan Intelektual & Media",
        "bpk_keywords": ["hak cipta", "paten", "merek", "rahasia dagang", "kekayaan intelektual"],
    },
    {
        "code": "TRADE_COMPETITION",
        "label_en": "Trade, Consumer & Competition",
        "label_id": "Perdagangan, Konsumen & Persaingan Usaha",
        "bpk_keywords": ["perdagangan", "perlindungan konsumen", "larangan praktek monopoli", "ekspor", "impor"],
    },
    {
        "code": "HEALTH_PHARMA",
        "label_en": "Healthcare & Pharmaceuticals",
        "label_id": "Kesehatan & Farmasi",
        "bpk_keywords": ["kesehatan", "rumah sakit", "obat dan makanan", "farmasi"],
    },
    {
        "code": "TRANSPORT_LOGISTICS",
        "label_en": "Transportation & Logistics",
        "label_id": "Transportasi & Logistik",
        "bpk_keywords": ["perhubungan", "pelayaran", "penerbangan", "lalu lintas dan angkutan"],
    },
    {
        "code": "PROCUREMENT_SOE",
        "label_en": "Public Procurement & SOEs",
        "label_id": "Pengadaan Barang/Jasa & BUMN",
        "bpk_keywords": ["pengadaan barang/jasa", "badan usaha milik negara", "bumn"],
    },
    {
        "code": "CRIMINAL_DISPUTE",
        "label_en": "Criminal, Arbitration & Court Rules",
        "label_id": "Pidana, Arbitrase & Peradilan",
        "bpk_keywords": ["pidana", "arbitrase", "kepailitan", "mahkamah agung", "peradilan"],
    },
)

CATEGORY_CODES = {c["code"] for c in LEGAL_CATEGORIES}

_PORTAL_DEFAULT_CATEGORY = {
    "ojk": "FINANCE_BANKING",
    "bi": "FINANCE_BANKING",
    "lps": "FINANCE_BANKING",
    "ppatk": "FINANCE_BANKING",
    "kominfo": "TECHNOLOGY_DIGITAL",
    "bssn": "TECHNOLOGY_DIGITAL",
    "kemnaker": "LABOR_MANPOWER",
    "bpjs_kesehatan": "HEALTH_PHARMA",
    "bpjs_ketenagakerjaan": "LABOR_MANPOWER",
    "kemenkeu": "TAX_CUSTOMS",
    "klhk": "ENERGY_MINING_ENV",
    "kemendag": "TRADE_COMPETITION",
    "kppu": "TRADE_COMPETITION",
    "bappebti": "TRADE_COMPETITION",
    "bkpm": "CORPORATE_INVESTMENT",
    "kemenko_ekon": "CORPORATE_INVESTMENT",
    "lkpp": "PROCUREMENT_SOE",
    "mahkamah_agung": "CRIMINAL_DISPUTE",
}

_KEYWORD_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("PROPERTY_AGRARIAN", re.compile(r"\b(pertanahan|agraria|hak tanggungan|rumah susun|perumahan|jasa konstruksi|konstruksi|bangunan gedung|tata ruang|sewa menyewa gedung|atr/bpn|pupr|pendaftaran tanah|hak atas tanah)\b", re.I)),
    ("IP_MEDIA", re.compile(r"\b(hak cipta|paten|merek|indikasi geografis|desain industri|rahasia dagang|kekayaan intelektual|royalti|sirkuit terpadu|lisensi kekayaan intelektual)\b", re.I)),
    ("HEALTH_PHARMA", re.compile(r"\b(bpjs kesehatan|jaminan kesehatan|rumah sakit|alat kesehatan|kefarmasian|farmasi|obat dan makanan|bpom|tenaga kesehatan|tenaga medis|rekam medis|karantina kesehatan|wabah penyakit|pangan olahan|keamanan pangan|tentang kesehatan)\b", re.I)),
    ("TRANSPORT_LOGISTICS", re.compile(r"\b(pelayaran|penerbangan|perkeretaapian|lalu lintas dan angkutan|angkutan jalan|logistik|kepelabuhanan|ekspedisi|kargo|perhubungan|pengurusan transportasi|armada)\b", re.I)),
    ("PROCUREMENT_SOE", re.compile(r"\b(pengadaan barang/jasa|pengadaan barang dan jasa|e-katalog|tender|lkpp|badan usaha milik negara|bumn|mbu\b)\b", re.I)),
    ("ENERGY_MINING_ENV", re.compile(r"\b(pertambangan|minerba|mineral dan batubara|batubara|minyak dan gas|migas|ketenagalistrikan|energi|lingkungan hidup|amdal|ukl-upl|limbah b3|kehutanan|nilai ekonomi karbon|emisi gas rumah kaca|esg)\b", re.I)),
    ("TECHNOLOGY_DIGITAL", re.compile(r"\b(data pribadi|pdp\b|transaksi elektronik|ite\b|sistem elektronik|pste|siber|keamanan informasi|sertifikat elektronik|komdigi|kominfo|bssn|telekomunikasi|cloud|perangkat lunak|spbe)\b", re.I)),
    ("CRIMINAL_DISPUTE", re.compile(r"\b(tindak pidana|hukum pidana|kuhp|arbitrase|alternatif penyelesaian sengketa|kepailitan|penundaan kewajiban pembayaran utang|pkpu|mahkamah agung|perma\b|sema\b|mediasi di pengadilan|gugatan sederhana|korupsi|tipikor)\b", re.I)),
    ("TAX_CUSTOMS", re.compile(r"\b(pajak|ppn\b|pph\b|perpajakan|kepabeanan|bea masuk|cukai|pabean|faktur pajak|pnbp|pmk\b|kmk\b|direktorat jenderal pajak|beacukai|akuntansi pemerintahan)\b", re.I)),
    ("LABOR_MANPOWER", re.compile(r"\b(ketenagakerjaan|tenaga kerja|pkwt|pkwtt|alih daya|outsourcing|upah|pengupahan|pesangon|phk\b|bpjs ketenagakerjaan|jaminan hari tua|jaminan pensiun|jaminan kecelakaan kerja|jaminan kehilangan pekerjaan|sistem jaminan sosial nasional|hubungan industrial|serikat pekerja|keselamatan dan kesehatan kerja|k3\b)\b", re.I)),
    ("TRADE_COMPETITION", re.compile(r"\b(perdagangan|perlindungan konsumen|monopoli|persaingan usaha|kppu|ekspor|impor|distribusi barang|waralaba|franchise|komoditi|bappebti)\b", re.I)),
    ("CORPORATE_INVESTMENT", re.compile(r"\b(perseroan terbatas|penanaman modal|perizinan berusaha|cipta kerja|oss\b|bkpm|akuisisi|merger|konsolidasi|rups|direksi|komisaris|koperasi|usaha mikro|umkm)\b", re.I)),
    ("FINANCE_BANKING", re.compile(r"\b(perbankan|bank indonesia|otoritas jasa keuangan|ojk\b|pojk\b|seojk\b|pbi\b|pasar modal|efek|asuransi|pembiayaan|fintech|pinjaman|kredit|likuiditas|pencucian uang|ppatk|lps\b|simpanan)\b", re.I)),
    ("CIVIL_CONTRACT", re.compile(r"\b(perdata|perikatan|perjanjian|wanprestasi|ganti rugi|bea meterai|bahasa indonesia|bendera, bahasa|force majeure|kerahasiaan|nda\b|jabatan notaris)\b", re.I)),
)


def classify_regulation_category(
    portal_key: Optional[str] = None,
    jenis: Optional[str] = None,
    judul: Optional[str] = None,
    raw_subject: Optional[str] = None,
    sektor: Optional[str] = None,
) -> str:
    """Deterministically assign one of the 14 canonical category_code values.
    Matches against title & subject first, falling back to jenis/sektor/portal."""
    primary_blob = " ".join(filter(None, [judul, raw_subject]))
    for code, pattern in _KEYWORD_RULES:
        if pattern.search(primary_blob):
            return code

    secondary_blob = " ".join(filter(None, [jenis, judul, raw_subject, sektor]))
    for code, pattern in _KEYWORD_RULES:
        if pattern.search(secondary_blob):
            return code

    j_up = (jenis or "").strip().upper()
    if j_up in ("POJK", "SEOJK", "PBI", "PLPS"):
        return "FINANCE_BANKING"
    if j_up in ("PMK", "KMK"):
        return "TAX_CUSTOMS"
    if j_up in ("PERMENAKER", "KEPMENAKER"):
        return "LABOR_MANPOWER"
    if j_up == "PERMA":
        return "CRIMINAL_DISPUTE"

    p_low = (portal_key or "").strip().lower()
    if p_low in _PORTAL_DEFAULT_CATEGORY:
        return _PORTAL_DEFAULT_CATEGORY[p_low]

    return "CIVIL_CONTRACT"


def classify_contract_categories(text_or_title: str, max_categories: int = 3) -> list[str]:
    """Identify the top 1-3 relevant legal categories for a contract or query,
    always including CIVIL_CONTRACT as baseline."""
    txt = text_or_title or ""
    matched: list[str] = []
    for code, pattern in _KEYWORD_RULES:
        if code == "CIVIL_CONTRACT":
            continue
        hits = len(pattern.findall(txt[:12000]))
        if hits > 0:
            matched.append((hits, code))

    matched.sort(key=lambda x: x[0], reverse=True)
    top = [code for _, code in matched[:max_categories]]
    if "CIVIL_CONTRACT" not in top:
        top.append("CIVIL_CONTRACT")
    return top


def backfill_existing_categories() -> dict:
    """Classify all existing rows in public.regulations and propagate category_code
    and is_active_law onto public.chunks."""
    rows = pg_service.query("SELECT id, domain, jenis, judul, sektor, status, legal_status FROM regulations")
    counts: dict[str, int] = {c["code"]: 0 for c in LEGAL_CATEGORIES}

    with pg_service.get_conn() as conn:
        with conn.transaction():
            for r in rows:
                cat = classify_regulation_category(
                    portal_key=r.get("domain"),
                    jenis=r.get("jenis"),
                    judul=r.get("judul"),
                    sektor=r.get("sektor"),
                )
                counts[cat] = counts.get(cat, 0) + 1
                is_active = (r.get("legal_status") or "berlaku") != "dicabut" and (r.get("status") or "") != "Dicabut"
                conn.execute(
                    "UPDATE regulations SET category_code = %s WHERE id = %s",
                    (cat, r["id"]),
                )
                conn.execute(
                    "UPDATE chunks SET category_code = %s, is_active_law = %s WHERE doc_id = %s",
                    (cat, is_active, str(r["id"])),
                )
    return {"total_classified": len(rows), "by_category": counts}
