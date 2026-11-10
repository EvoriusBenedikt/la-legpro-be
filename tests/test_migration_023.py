import os
import pathlib
import uuid
import pytest
import conftest
from services import pg_service

# Ensure TEST_DB is populated so db tests run even if TEST_DATABASE_URL wasn't pre-set in the shell
if not getattr(conftest, "TEST_DB", None):
    conftest.TEST_DB = os.getenv("DATABASE_URL", "postgresql://legpro:legpro@127.0.0.1:5432/legpro")

pytestmark = pytest.mark.db

MIGRATION_FILE = pathlib.Path(__file__).resolve().parent.parent / "migrations" / "023_workspace_icon.sql"


def apply_migration():
    """Apply migration 023 to the active database."""
    sql = MIGRATION_FILE.read_text(encoding="utf-8")
    with pg_service.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)


def test_migration_023_column_exists():
    """Verify that notebooks table has an 'icon' column with DEFAULT 'folder' and NOT NULL."""
    row = pg_service.query_one(
        "SELECT column_name, column_default, is_nullable, data_type, character_maximum_length "
        "FROM information_schema.columns "
        "WHERE table_name = 'notebooks' AND column_name = 'icon'"
    )
    assert row is not None, "Column 'icon' does not exist in 'notebooks' table"
    assert "folder" in str(row["column_default"]).lower()
    assert row["is_nullable"] == "NO"
    assert row["data_type"] == "character varying"
    assert row["character_maximum_length"] == 64


def test_migration_023_is_idempotent():
    """Verify that applying 023 multiple times runs cleanly without error."""
    assert MIGRATION_FILE.exists(), f"Migration file {MIGRATION_FILE} does not exist"
    apply_migration()
    apply_migration()
    test_migration_023_column_exists()


def test_migration_023_backfill_patterns():
    """Verify that existing rows matching canonical contract patterns get backfilled."""
    test_cases = [
        ("PKS Pengadaan Jaringan", "handshake"),
        ("Perjanjian Kerjasama Tripartit", "handshake"),
        ("Strategic Partnership 2026", "handshake"),
        ("BAST Proyek Fiber Optic", "clipboard-check"),
        ("Berita Acara Serah Terima", "clipboard-check"),
        ("Hardware Handover Docs", "clipboard-check"),
        ("Addendum Kontrak Sewa", "file-plus"),
        ("Amandemen Perjanjian No 2", "file-plus"),
        ("Contract Amendment 2026", "file-plus"),
        ("Data Sharing Agreement PT ABC", "share-2"),
        ("DSA Telco Feed", "share-2"),
        ("Berbagi Data Pelanggan", "share-2"),
        ("DPA Cloud Vendor", "shield-check"),
        ("Data Processing Agreement", "shield-check"),
        ("Perlindungan Data Pribadi", "shield-check"),
        ("NDA Kerahasiaan", "lock"),
        ("Non-Disclosure Agreement", "lock"),
        ("Surat Kerahasiaan Mitra", "lock"),
        ("Confidentiality Document", "lock"),
        ("SLA Datacenter Tier 3", "gauge"),
        ("Service Level Agreement", "gauge"),
        ("Perjanjian Tingkat Layanan", "gauge"),
        ("SOW Managed Services", "list-checks"),
        ("Statement of Work Phase 1", "list-checks"),
        ("Ruang Lingkup Pekerjaan", "list-checks"),
        ("MoU Kerjasama Kampus", "file-signature"),
        ("Nota Kesepahaman B2B", "file-signature"),
        ("Memorandum of Understanding", "file-signature"),
        ("PO Server GPU H100", "shopping-cart"),
        ("Purchase Order Perangkat", "shopping-cart"),
        ("Pesanan Pembelian Router", "shopping-cart"),
        ("Purchase Agreement Dell", "shopping-cart"),
        ("SPK Instalasi Tower", "file-text"),
        ("Surat Perintah Kerja Mandiri", "file-text"),
        ("Work Order Cabang Bandung", "file-text"),
        ("Reseller Agreement PT ABC", "store"),
        ("Perjanjian Keagenan Distribusi", "store"),
        ("Distributor Tunggal Jawa Barat", "store"),
        ("Mitra Usaha Bersama", "store"),
        ("Proyek Internal Non-Contract", "folder"),
    ]

    user_id = f"test_user_{uuid.uuid4().hex[:8]}"
    inserted_ids = []
    try:
        # First ensure migration is applied so we can insert with or without icon
        if MIGRATION_FILE.exists():
            apply_migration()
        else:
            pytest.fail("Migration file does not exist yet")

        for name, _ in test_cases:
            nid = f"test_nb_{uuid.uuid4().hex[:12]}"
            inserted_ids.append(nid)
            # Reset icon to 'folder' to simulate pre-migration or unclassified row
            pg_service.execute(
                "INSERT INTO notebooks (id, user_id, name, icon) VALUES (%s, %s, %s, 'folder')",
                (nid, user_id, name),
            )

        # Run migration backfill again
        apply_migration()

        # Check each inserted workspace
        for nid, (name, expected_icon) in zip(inserted_ids, test_cases):
            row = pg_service.query_one("SELECT icon FROM notebooks WHERE id = %s", (nid,))
            assert row is not None
            assert row["icon"] == expected_icon, (
                f"For workspace name '{name}', expected icon '{expected_icon}', but got '{row['icon']}'"
            )
    finally:
        if inserted_ids:
            with pg_service.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM notebooks WHERE user_id = %s", (user_id,))
