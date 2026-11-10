"""Tests for workspace icon classification and inference service."""
import pytest
from unittest.mock import patch

from api.services.workspace_icon import (
    detect_canonical_icon,
    infer_workspace_icon,
    validate_icon_name,
    CURATED_WORKSPACE_ICONS,
)


def test_detect_canonical_icons():
    # 1. PKS / Partnership -> handshake
    assert detect_canonical_icon("PKS Lintasarta 2026") == "handshake"
    assert detect_canonical_icon("Perjanjian Kerjasama Tripartit") == "handshake"
    assert detect_canonical_icon("Perjanjian Kerja Sama Tripartit") == "handshake"
    assert detect_canonical_icon("Strategic Partnership 2026") == "handshake"

    # 2. BAST / Handover -> clipboard-check
    assert detect_canonical_icon("BAST Proyek Fiber") == "clipboard-check"
    assert detect_canonical_icon("Berita Acara Serah Terima") == "clipboard-check"
    assert detect_canonical_icon("Hardware Handover Docs") == "clipboard-check"

    # 3. Addendum / Amendment -> file-plus
    assert detect_canonical_icon("Addendum No 1") == "file-plus"
    assert detect_canonical_icon("Amandemen Perjanjian No 2") == "file-plus"
    assert detect_canonical_icon("Contract Amendment 2026") == "file-plus"

    # 4. Data Sharing / DSA -> share-2
    assert detect_canonical_icon("Data Sharing Agreement v2") == "share-2"
    assert detect_canonical_icon("DSA Telco Feed") == "share-2"
    assert detect_canonical_icon("Berbagi Data Pelanggan") == "share-2"

    # 5. DPA / Data Processing -> shield-check
    assert detect_canonical_icon("Data Processing Agreement (DPA)") == "shield-check"
    assert detect_canonical_icon("DPA Cloud Vendor") == "shield-check"
    assert detect_canonical_icon("Perlindungan Data Pribadi") == "shield-check"

    # 6. NDA / Confidentiality -> lock
    assert detect_canonical_icon("Perjanjian Kerahasiaan (NDA)") == "lock"
    assert detect_canonical_icon("Non-Disclosure Agreement") == "lock"
    assert detect_canonical_icon("Surat Kerahasiaan Mitra") == "lock"
    assert detect_canonical_icon("Confidentiality Document") == "lock"

    # 7. SLA / Service Level -> gauge
    assert detect_canonical_icon("SLA Datacenter Tier 3") == "gauge"
    assert detect_canonical_icon("Service Level Agreement") == "gauge"
    assert detect_canonical_icon("Perjanjian Tingkat Layanan") == "gauge"

    # 8. SOW / Statement of Work -> list-checks
    assert detect_canonical_icon("SOW Managed Services") == "list-checks"
    assert detect_canonical_icon("Statement of Work Phase 1") == "list-checks"
    assert detect_canonical_icon("Ruang Lingkup Pekerjaan") == "list-checks"

    # 9. MoU / Memorandum -> file-signature
    assert detect_canonical_icon("MoU Kerjasama Universitas") == "file-signature"
    assert detect_canonical_icon("Nota Kesepahaman B2B") == "file-signature"
    assert detect_canonical_icon("Memorandum of Understanding") == "file-signature"

    # 10. PO / Purchase Order -> shopping-cart
    assert detect_canonical_icon("PO Server GPU") == "shopping-cart"
    assert detect_canonical_icon("Purchase Order Perangkat") == "shopping-cart"
    assert detect_canonical_icon("Pesanan Pembelian Router") == "shopping-cart"
    assert detect_canonical_icon("Purchase Agreement Dell") == "shopping-cart"

    # 11. SPK / Work Order -> file-text
    assert detect_canonical_icon("SPK Instalasi Tower") == "file-text"
    assert detect_canonical_icon("Surat Perintah Kerja Mandiri") == "file-text"
    assert detect_canonical_icon("Work Order Cabang Bandung") == "file-text"

    # 12. Reseller / Mitra / Distributor -> store
    assert detect_canonical_icon("Reseller Agreement PT ABC") == "store"
    assert detect_canonical_icon("Perjanjian Keagenan Distribusi") == "store"
    assert detect_canonical_icon("Distributor Tunggal Jawa Barat") == "store"
    assert detect_canonical_icon("Mitra Usaha Bersama") == "store"

    # Unmatched / Novel
    assert detect_canonical_icon("Random Project Without Category") is None
    assert detect_canonical_icon("") is None
    assert detect_canonical_icon(None) is None


def test_detect_canonical_negative_boundary_protection():
    """Verify that acronyms bounded by \b do not falsely match Indonesian words."""
    assert detect_canonical_icon("Laporan Keuangan") is None       # 'po' in 'Laporan'
    assert detect_canonical_icon("Agenda Rapat") is None           # 'nda' in 'Agenda'
    assert detect_canonical_icon("Standar Dokumen") is None        # 'nda' in 'Standar'
    assert detect_canonical_icon("Kelompok Diskusi") is None       # 'po' in 'Kelompok'
    assert detect_canonical_icon("Legislatif Nasional") is None    # 'sla' in 'Legislatif'
    assert detect_canonical_icon("Pasal Pidana Khusus") is None    # 'pks' subwords
    assert detect_canonical_icon("Disposisi Surat") is None        # 'po' in 'Disposisi'


def test_validate_icon_name():
    assert validate_icon_name("handshake") == "handshake"
    assert validate_icon_name("folder") == "folder"
    assert validate_icon_name("server") == "server"
    assert validate_icon_name("lock") == "lock"
    assert validate_icon_name("invalid<script>") == "folder"
    assert validate_icon_name(None) == "folder"
    assert validate_icon_name("") == "folder"
    assert validate_icon_name("   ") == "folder"
    assert validate_icon_name("non-existent-random-icon-999") == "folder"
    assert validate_icon_name(12345) == "folder"


def test_curated_icons_list():
    assert "handshake" in CURATED_WORKSPACE_ICONS
    assert "lock" in CURATED_WORKSPACE_ICONS
    assert "folder" in CURATED_WORKSPACE_ICONS
    assert len(CURATED_WORKSPACE_ICONS) >= 30
    for icon in CURATED_WORKSPACE_ICONS:
        assert isinstance(icon, str)
        assert icon == icon.lower()
        assert not icon.startswith("-") and not icon.endswith("-")


def test_infer_workspace_icon_canonical_fastpath():
    with patch("api.services.workspace_icon.call_glm") as mock_glm:
        # Fast path should never call LLM
        icon = infer_workspace_icon("PKS Lintasarta 2026")
        assert icon == "handshake"
        mock_glm.assert_not_called()

        icon = infer_workspace_icon("BAST Proyek Fiber")
        assert icon == "clipboard-check"
        mock_glm.assert_not_called()


def test_infer_workspace_icon_empty_or_none():
    with patch("api.services.workspace_icon.call_glm") as mock_glm:
        assert infer_workspace_icon("") == "folder"
        assert infer_workspace_icon("   ") == "folder"
        assert infer_workspace_icon(None) == "folder"
        mock_glm.assert_not_called()


def test_infer_workspace_icon_llm_success():
    with patch("api.services.workspace_icon.call_glm") as mock_glm:
        mock_glm.return_value = '{"icon": "server"}'
        icon = infer_workspace_icon("Infrastruktur Data Center Cikarang", description="Manajemen server")
        assert icon == "server"
        mock_glm.assert_called_once()
        # Verify prompt contents
        args, kwargs = mock_glm.call_args
        messages = args[0]
        assert any("Infrastruktur Data Center Cikarang" in m["content"] for m in messages)
        assert any("Manajemen server" in m["content"] for m in messages)


def test_infer_workspace_icon_llm_markdown_fence():
    with patch("api.services.workspace_icon.call_glm") as mock_glm:
        mock_glm.return_value = 'Berikut adalah icon yang cocok:\n```json\n{"icon": "database"}\n```'
        icon = infer_workspace_icon("Sistem Backup Database Oracle")
        assert icon == "database"


def test_infer_workspace_icon_llm_invalid_icon_fallback():
    with patch("api.services.workspace_icon.call_glm") as mock_glm:
        mock_glm.return_value = '{"icon": "non-existent-alien-icon"}'
        icon = infer_workspace_icon("Proyek Eksplorasi Luar Angkasa")
        assert icon == "folder"


def test_infer_workspace_icon_llm_exception_defensive():
    with patch("api.services.workspace_icon.call_glm") as mock_glm:
        mock_glm.side_effect = RuntimeError("Connection timeout to LLM endpoint")
        icon = infer_workspace_icon("Proyek Novel Tanpa Kategori")
        assert icon == "folder"


def test_infer_workspace_icon_llm_malformed_json_fallback():
    with patch("api.services.workspace_icon.call_glm") as mock_glm:
        mock_glm.return_value = "Maaf, saya tidak mengerti permintaannya."
        icon = infer_workspace_icon("Proyek Novel Tanpa Kategori")
        assert icon == "folder"
