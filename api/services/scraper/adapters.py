"""Reusable portal adapters & 22-portal registry for broad regulation scraping."""
from __future__ import annotations

import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import requests
from lxml import html as lhtml

from services.scraper.canonicalizer import (
    build_canonical_key,
    normalize_jenis,
    normalize_legal_status,
    normalize_nomor_tahun,
)


@dataclass(frozen=True)
class PortalDef:
    source_key: str
    display_name: str
    base_url: str
    domain_group: str  # 'national' | 'financial' | 'ict_cyber' | 'trade_investment' | 'labor_sectoral'
    adapter_type: str
    sektor: str


PORTAL_REGISTRY: tuple[PortalDef, ...] = (
    # National Aggregators & Parliament (5)
    PortalDef("bpk", "JDIH BPK", "https://peraturan.bpk.go.id", "national", "BpkAdapter", "Nasional"),
    PortalDef("peraturan_go_id", "Peraturan.go.id (Kemenkumham)", "https://peraturan.go.id", "national", "PeraturanGoIdAdapter", "Nasional"),
    PortalDef("dpr", "DPR RI", "https://jdih.dpr.go.id", "national", "JdihnTemplateAdapter", "Nasional"),
    PortalDef("setkab", "Sekretariat Kabinet", "https://jdih.setkab.go.id", "national", "JdihnTemplateAdapter", "Nasional"),
    PortalDef("mahkamah_agung", "Mahkamah Agung", "https://jdih.mahkamahagung.go.id", "national", "JdihnTemplateAdapter", "Peradilan"),
    # Financial, Monetary & Fiscal (6)
    PortalDef("ojk", "Otoritas Jasa Keuangan (OJK)", "https://jdih.ojk.go.id", "financial", "OjkAdapter", "Keuangan"),
    PortalDef("bi", "Bank Indonesia", "https://www.bi.go.id", "financial", "JdihnTemplateAdapter", "Perbankan"),
    PortalDef("kemenkeu", "Kementerian Keuangan", "https://jdih.kemenkeu.go.id", "financial", "JdihnTemplateAdapter", "Fiskal & Pajak"),
    PortalDef("lps", "Lembaga Penjamin Simpanan (LPS)", "https://lps.go.id", "financial", "JdihnTemplateAdapter", "Perbankan"),
    PortalDef("ppatk", "PPATK", "https://ppatk.go.id", "financial", "JdihnTemplateAdapter", "Anti Pencucian Uang"),
    PortalDef("bappebti", "Bappebti", "https://bappebti.go.id", "financial", "JdihnTemplateAdapter", "Komoditas & Kripto"),
    # ICT, Cyber & Digital Economy (2)
    PortalDef("kominfo", "Komdigi / Kominfo", "https://jdih.komdigi.go.id", "ict_cyber", "JdihnTemplateAdapter", "TIK & Data"),
    PortalDef("bssn", "BSSN", "https://jdih.bssn.go.id", "ict_cyber", "JdihnTemplateAdapter", "Keamanan Siber"),
    # Trade, Investment & Competition (4)
    PortalDef("kemendag", "Kementerian Perdagangan", "https://jdih.kemendag.go.id", "trade_investment", "JdihnTemplateAdapter", "Perdagangan"),
    PortalDef("kemenko_ekon", "Kemenko Perekonomian", "https://jdih.ekon.go.id", "trade_investment", "JdihnTemplateAdapter", "Perekonomian"),
    PortalDef("bkpm", "Kementerian Investasi / BKPM", "https://jdih.bkpm.go.id", "trade_investment", "JdihnTemplateAdapter", "Investasi & Perizinan"),
    PortalDef("kppu", "KPPU", "https://jdih.kppu.go.id", "trade_investment", "JdihnTemplateAdapter", "Persaingan Usaha"),
    # Labor, Social Security, Procurement & Environment (5)
    PortalDef("kemnaker", "Kementerian Ketenagakerjaan", "https://jdih.kemnaker.go.id", "labor_sectoral", "JdihnTemplateAdapter", "Ketenagakerjaan"),
    PortalDef("bpjs_kesehatan", "BPJS Kesehatan", "https://bpjs-kesehatan.go.id", "labor_sectoral", "JdihnTemplateAdapter", "Jaminan Sosial"),
    PortalDef("bpjs_ketenagakerjaan", "BPJS Ketenagakerjaan", "https://www.bpjsketenagakerjaan.go.id", "labor_sectoral", "JdihnTemplateAdapter", "Ketenagakerjaan"),
    PortalDef("lkpp", "LKPP", "https://jdih.lkpp.go.id", "labor_sectoral", "JdihnTemplateAdapter", "Pengadaan"),
    PortalDef("klhk", "Kementerian LHK", "https://jdih.menlhk.go.id", "labor_sectoral", "JdihnTemplateAdapter", "Lingkungan & ESG"),
)

PORTAL_BY_KEY = {p.source_key: p for p in PORTAL_REGISTRY}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LA-LegPro-Bot/2.0; +https://legal-analyzer.lintasarta.dev)"
}


class BasePortalAdapter:
    def __init__(self, portal: PortalDef):
        self.portal = portal
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)

    def fetch_listings(
        self,
        min_year: int = 2015,
        max_pages: int = 10,
        rate_limit_ms: int = 1200,
    ) -> list[dict]:
        raise NotImplementedError

    def _get(self, url: str, timeout: float | tuple[float, float] = 20) -> Optional[requests.Response]:
        try:
            resp = self.session.get(url, timeout=timeout, verify=False)
            if resp.ok:
                return resp
        except Exception as e:
            print(f"[Scraper:{self.portal.source_key}] GET {url} failed: {e}")
        return None

    def fetch_status_metadata(
        self,
        detail_url: str,
        timeout: float | tuple[float, float] = (2.0, 3.0),
    ) -> dict:
        """Fetch HTML metadata only (never PDFs) and extract legal status & relations."""
        if not detail_url or detail_url.startswith("canonical://"):
            return {"ok": False, "error": "no_detail_url"}
        resp = self._get(detail_url, timeout=timeout)
        if not resp:
            return {"ok": False, "error": "portal_unreachable"}
        return self.parse_status_html(resp.text, detail_url)

    def parse_status_html(self, html_text: str, detail_url: str = "") -> dict:
        """Extract legal_status, status_note, and amendment/repeal relations from HTML."""
        try:
            doc = lhtml.fromstring(html_text)
        except Exception as e:
            return {"ok": False, "error": f"parse_error: {e}"}

        status_nodes = doc.xpath(
            "//*[contains(translate(@class,'STATUS','status'),'status') "
            "or contains(translate(@class,'BADGE','badge'),'badge')]"
        )
        status_text = " ".join(" ".join(n.text_content().split()) for n in status_nodes[:6])
        body_text = " ".join(doc.text_content().split())
        legal_status = normalize_legal_status(status_text or body_text[:1500])
        if legal_status == "unknown":
            legal_status = normalize_legal_status(body_text[:2000]) or "berlaku"
            if legal_status == "unknown":
                legal_status = "berlaku"

        relations = _extract_relations_from_text(body_text, doc, self.portal.base_url)
        status_note = _extract_status_note(body_text)
        return {
            "ok": True,
            "legal_status": legal_status,
            "status_note": status_note,
            "relations": relations,
        }


class BpkAdapter(BasePortalAdapter):
    """Adapter for JDIH BPK (peraturan.bpk.go.id)."""

    def fetch_listings(self, min_year: int = 2015, max_pages: int = 10, rate_limit_ms: int = 1200) -> list[dict]:
        results: list[dict] = []
        for page in range(1, max(1, max_pages) + 1):
            url = f"{self.portal.base_url}/Search?keywords=&tahun={min_year}&page={page}"
            resp = self._get(url)
            if not resp:
                break
            doc = lhtml.fromstring(resp.text)
            cards = doc.xpath("//div[contains(@class,'card') or contains(@class,'search-result')] | //tr")
            page_found = 0
            for card in cards:
                links = card.xpath(".//a[contains(@href,'/Details/') or contains(@href,'/Home/Details/')]")
                if not links:
                    continue
                link = links[0]
                href = urllib.parse.urljoin(self.portal.base_url, link.get("href", ""))
                title = " ".join(link.text_content().split())
                text_blob = " ".join(card.text_content().split())
                m = re.search(r"\b(UU|PP|Perpres|Perppu|Permen[a-z]*|POJK|PBI)\b.*?(?:Nomor|No\.?)\s*([0-9A-Z./-]+)\s*(?:Tahun\s*((?:19|20)\d{2}))?", text_blob, re.IGNORECASE)
                jenis = normalize_jenis(m.group(1) if m else "Peraturan")
                nomor = m.group(2) if m else title[:40]
                tahun = int(m.group(3)) if (m and m.group(3)) else None
                if tahun and tahun < min_year:
                    continue
                status = normalize_legal_status(text_blob)
                pdf_links = card.xpath(".//a[contains(@href,'.pdf') or contains(@href,'/Download/')]")
                dl_url = urllib.parse.urljoin(self.portal.base_url, pdf_links[0].get("href", "")) if pdf_links else ""
                results.append({
                    "source_key": self.portal.source_key,
                    "domain_group": self.portal.domain_group,
                    "sektor": self.portal.sektor,
                    "jenis": jenis,
                    "nomor": nomor,
                    "tahun": tahun,
                    "judul": title,
                    "tentang": title,
                    "detail_url": href,
                    "download_url": dl_url,
                    "legal_status": status,
                    "relations": [],
                })
                page_found += 1
            if page_found == 0:
                break
            time.sleep(max(0.1, rate_limit_ms / 1000.0))
        return results


class PeraturanGoIdAdapter(BasePortalAdapter):
    """Adapter for Kemenkumham Peraturan.go.id."""

    def fetch_listings(self, min_year: int = 2015, max_pages: int = 10, rate_limit_ms: int = 1200) -> list[dict]:
        results: list[dict] = []
        for page in range(1, max(1, max_pages) + 1):
            url = f"{self.portal.base_url}/peraturan?page={page}"
            resp = self._get(url)
            if not resp:
                break
            doc = lhtml.fromstring(resp.text)
            items = doc.xpath("//div[contains(@class,'strip') or contains(@class,'card')] | //tr")
            page_found = 0
            for item in items:
                links = item.xpath(".//a[contains(@href,'/id/')]")
                if not links:
                    continue
                link = links[0]
                href = urllib.parse.urljoin(self.portal.base_url, link.get("href", ""))
                title = " ".join(link.text_content().split())
                blob = " ".join(item.text_content().split())
                norm_nomor, tahun = normalize_nomor_tahun(title)
                if tahun and tahun < min_year:
                    continue
                pdf_links = item.xpath(".//a[contains(@href,'.pdf')]")
                results.append({
                    "source_key": self.portal.source_key,
                    "domain_group": self.portal.domain_group,
                    "sektor": self.portal.sektor,
                    "jenis": normalize_jenis(title.split()[0] if title else "Peraturan"),
                    "nomor": norm_nomor,
                    "tahun": tahun,
                    "judul": title,
                    "tentang": title,
                    "detail_url": href,
                    "download_url": urllib.parse.urljoin(self.portal.base_url, pdf_links[0].get("href", "")) if pdf_links else "",
                    "legal_status": normalize_legal_status(blob),
                    "relations": [],
                })
                page_found += 1
            if page_found == 0:
                break
            time.sleep(max(0.1, rate_limit_ms / 1000.0))
        return results


class OjkAdapter(BasePortalAdapter):
    """Adapter for OJK JDIH (jdih.ojk.go.id)."""

    def fetch_listings(self, min_year: int = 2015, max_pages: int = 10, rate_limit_ms: int = 1200) -> list[dict]:
        results: list[dict] = []
        for page in range(1, max(1, max_pages) + 1):
            url = f"{self.portal.base_url}/id/regulasi?page={page}"
            resp = self._get(url)
            if not resp:
                break
            doc = lhtml.fromstring(resp.text)
            rows = doc.xpath("//tr | //div[contains(@class,'regulasi-item') or contains(@class,'card')]")
            page_found = 0
            for row in rows:
                links = row.xpath(".//a[@href]")
                if not links:
                    continue
                link = links[0]
                title = " ".join(link.text_content().split())
                if len(title) < 6:
                    continue
                href = urllib.parse.urljoin(self.portal.base_url, link.get("href", ""))
                norm_nomor, tahun = normalize_nomor_tahun(title)
                if tahun and tahun < min_year:
                    continue
                pdf_links = row.xpath(".//a[contains(@href,'.pdf')]")
                jenis = "SEOJK" if "SEOJK" in title.upper() or "SURAT EDARAN" in title.upper() else "POJK"
                results.append({
                    "source_key": self.portal.source_key,
                    "domain_group": self.portal.domain_group,
                    "sektor": self.portal.sektor,
                    "jenis": jenis,
                    "nomor": norm_nomor,
                    "tahun": tahun,
                    "judul": title,
                    "tentang": title,
                    "detail_url": href,
                    "download_url": urllib.parse.urljoin(self.portal.base_url, pdf_links[0].get("href", "")) if pdf_links else "",
                    "legal_status": normalize_legal_status(" ".join(row.text_content().split())),
                    "relations": [],
                })
                page_found += 1
            if page_found == 0:
                break
            time.sleep(max(0.1, rate_limit_ms / 1000.0))
        return results


class JdihnTemplateAdapter(BasePortalAdapter):
    """Shared adapter for JDIHN-standard ministry/agency portals."""

    def fetch_listings(self, min_year: int = 2015, max_pages: int = 10, rate_limit_ms: int = 1200) -> list[dict]:
        results: list[dict] = []
        for page in range(1, max(1, max_pages) + 1):
            url = f"{self.portal.base_url}/peraturan?page={page}"
            resp = self._get(url)
            if not resp:
                # Fallback to root listing once
                if page == 1:
                    resp = self._get(self.portal.base_url)
                if not resp:
                    break
            doc = lhtml.fromstring(resp.text)
            rows = doc.xpath("//table//tr | //div[contains(@class,'card') or contains(@class,'list-group-item')] | //article")
            page_found = 0
            for row in rows:
                pdf_links = row.xpath(".//a[contains(@href,'.pdf') or contains(@href,'download') or contains(@href,'peraturan')]")
                if not pdf_links:
                    continue
                pdf_link = pdf_links[0]
                title = " ".join(pdf_link.text_content().split()) or " ".join(row.text_content().split())[:120]
                if len(title) < 5:
                    continue
                href = urllib.parse.urljoin(self.portal.base_url, pdf_link.get("href", ""))
                norm_nomor, tahun = normalize_nomor_tahun(title)
                if tahun and tahun < min_year:
                    continue
                results.append({
                    "source_key": self.portal.source_key,
                    "domain_group": self.portal.domain_group,
                    "sektor": self.portal.sektor,
                    "jenis": normalize_jenis(title.split()[0] if title else "Peraturan"),
                    "nomor": norm_nomor,
                    "tahun": tahun,
                    "judul": title,
                    "tentang": title,
                    "detail_url": href,
                    "download_url": href if href.lower().endswith(".pdf") else "",
                    "legal_status": normalize_legal_status(" ".join(row.text_content().split())),
                    "relations": [],
                })
                page_found += 1
            if page_found == 0:
                break
            time.sleep(max(0.1, rate_limit_ms / 1000.0))
        return results


_REL_PATTERNS: tuple[tuple[str, str, str], ...] = (
    # (regex, relation_type, direction: 'outgoing' means this reg acts on target; 'incoming' means target acts on this reg)
    (
        r"(?:Mencabut\s+sebagian|Dicabut\s+sebagian\s+dengan)\s*:?\s*"
        r"(UU|PP|Perpres|Perppu|Permen[a-z]*|POJK|SEOJK|PBI|PADG|Perban|Peraturan\s+[A-Za-z]+)\s+"
        r"(?:Nomor|No\.?)\s*([0-9A-Z./-]+)(?:\s+Tahun\s+((?:19|20)\d{2}))?",
        "PARTIALLY_REPEALED_BY",
        "auto",
    ),
    (
        r"(?:Mencabut|Dicabut\s+dengan)\s*:?\s*"
        r"(UU|PP|Perpres|Perppu|Permen[a-z]*|POJK|SEOJK|PBI|PADG|Perban|Peraturan\s+[A-Za-z]+)\s+"
        r"(?:Nomor|No\.?)\s*([0-9A-Z./-]+)(?:\s+Tahun\s+((?:19|20)\d{2}))?",
        "REPEALED_BY",
        "auto",
    ),
    (
        r"(?:Mengubah|Diubah\s+dengan)\s*:?\s*"
        r"(UU|PP|Perpres|Perppu|Permen[a-z]*|POJK|SEOJK|PBI|PADG|Perban|Peraturan\s+[A-Za-z]+)\s+"
        r"(?:Nomor|No\.?)\s*([0-9A-Z./-]+)(?:\s+Tahun\s+((?:19|20)\d{2}))?",
        "AMENDED_BY",
        "auto",
    ),
)


def _extract_relations_from_text(text: str, doc=None, base_url: str = "") -> list[dict]:
    """Parse Mencabut / Mengubah / Dicabut dengan / Diubah dengan references from HTML text."""
    seen: set[tuple[str, str]] = set()
    relations: list[dict] = []
    for pattern, rel_type, _ in _REL_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            jenis = normalize_jenis(m.group(1))
            raw_nomor = m.group(2)
            tahun_hint = int(m.group(3)) if m.group(3) else None
            norm_nomor, norm_tahun = normalize_nomor_tahun(raw_nomor, tahun_hint)
            canon = build_canonical_key(jenis, norm_nomor, norm_tahun)
            key = (rel_type, canon)
            if key in seen:
                continue
            seen.add(key)
            matched_phrase = m.group(0).strip()
            is_incoming = any(w in matched_phrase.lower() for w in ("dicabut", "diubah"))
            relations.append({
                "relation_type": rel_type,
                "canonical_key": canon,
                "direction": "incoming" if is_incoming else "outgoing",
                "raw_match": matched_phrase[:200],
            })
    return relations


def _extract_status_note(text: str) -> Optional[str]:
    """Extract a concise human-readable note for Putusan MK or amending/repealing laws."""
    m = re.search(
        r"(Putusan\s+MK\s+(?:Nomor|No\.?)\s*[0-9A-Z./-]+(?:\s+Tahun\s+(?:19|20)\d{2})?|"
        r"(?:Diubah|Dicabut(?:\s+sebagian)?)\s+dengan\s+(?:UU|PP|Perpres|Perppu|Permen[a-z]*|POJK|SEOJK|PBI)\s+"
        r"(?:Nomor|No\.?)\s*[0-9A-Z./-]+(?:\s+Tahun\s+(?:19|20)\d{2})?)",
        text,
        re.IGNORECASE,
    )
    return m.group(1).strip()[:240] if m else None


def get_adapter(source_key: str) -> BasePortalAdapter:
    portal = PORTAL_BY_KEY[source_key]
    if portal.adapter_type == "BpkAdapter":
        return BpkAdapter(portal)
    if portal.adapter_type == "PeraturanGoIdAdapter":
        return PeraturanGoIdAdapter(portal)
    if portal.adapter_type == "OjkAdapter":
        return OjkAdapter(portal)
    return JdihnTemplateAdapter(portal)


def get_adapter_for_url(detail_url: str) -> BasePortalAdapter:
    """Resolve the matching portal adapter for a regulation's detail_url."""
    host = (urllib.parse.urlparse(detail_url or "").hostname or "").lower()
    for portal in PORTAL_REGISTRY:
        portal_host = (urllib.parse.urlparse(portal.base_url).hostname or "").lower()
        if portal_host and (host == portal_host or host.endswith("." + portal_host)):
            return get_adapter(portal.source_key)
    return get_adapter("bpk")
