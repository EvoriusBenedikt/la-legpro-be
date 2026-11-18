"""Unit tests for Phase 1 & Phase 2 of Live Regulation Freshness & Status Verification."""
from __future__ import annotations

import os
import re
import sys
import types
import unittest
from html.parser import HTMLParser
from unittest.mock import MagicMock, patch

# Allow running unit tests on bare host Python when container deps aren't installed.
if "psycopg" not in sys.modules:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        _psycopg = types.ModuleType("psycopg")
        _psycopg.rows = types.SimpleNamespace(dict_row=object())
        sys.modules["psycopg"] = _psycopg
        sys.modules["psycopg.rows"] = _psycopg.rows
        _pool = types.ModuleType("psycopg_pool")
        _pool.ConnectionPool = MagicMock
        sys.modules["psycopg_pool"] = _pool

if "requests" not in sys.modules:
    try:
        import requests  # noqa: F401
    except ImportError:
        _req = types.ModuleType("requests")
        _req.Session = MagicMock
        _req.Response = MagicMock
        sys.modules["requests"] = _req

if "lxml" not in sys.modules:
    try:
        import lxml.html  # noqa: F401
    except ImportError:
        class _MiniNode:
            def __init__(self, text: str, classes: list[str] | None = None):
                self._text = text
                self._classes = classes or []

            def text_content(self) -> str:
                return self._text

            def xpath(self, expr: str):
                if "status" in expr.lower() or "badge" in expr.lower():
                    return [self] if self._classes else []
                return []

        class _MiniHTMLParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.texts: list[str] = []
                self.badge_texts: list[str] = []
                self._in_badge = False

            def handle_starttag(self, tag, attrs):
                cls = dict(attrs).get("class", "")
                if "status" in cls.lower() or "badge" in cls.lower():
                    self._in_badge = True

            def handle_endtag(self, tag):
                self._in_badge = False

            def handle_data(self, data):
                self.texts.append(data)
                if self._in_badge:
                    self.badge_texts.append(data)

        def _fromstring(html_text: str):
            p = _MiniHTMLParser()
            p.feed(html_text)
            full = " ".join(p.texts)
            badge = " ".join(p.badge_texts)
            root = _MiniNode(full)
            badge_node = _MiniNode(badge, classes=["badge"]) if badge else None
            root.xpath = lambda expr: ([badge_node] if badge_node and ("status" in expr.lower() or "badge" in expr.lower()) else [])
            return root

        _lxml = types.ModuleType("lxml")
        _lhtml = types.ModuleType("lxml.html")
        _lhtml.fromstring = _fromstring
        _lxml.html = _lhtml
        sys.modules["lxml"] = _lxml
        sys.modules["lxml.html"] = _lhtml

API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "api"))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from services.freshness.delta_poller import run_delta_poll  # noqa: E402
from services.freshness.heartbeat_worker import run_heartbeat_batch  # noqa: E402
from services.freshness.status_updater import apply_status_transition, cascade_relation_to_target  # noqa: E402
from services.scraper.adapters import get_adapter  # noqa: E402
from services.scraper.canonicalizer import normalize_legal_status  # noqa: E402


class TestMigration031AndCanonicalizer(unittest.TestCase):
    def test_migration_031_uses_integer_fk_and_doc_id_join(self):
        mig_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "migrations", "031_regulation_live_freshness.sql")
        )
        with open(mig_path, "r", encoding="utf-8") as f:
            sql = f.read()
        self.assertIn("regulation_id INTEGER NOT NULL REFERENCES regulations(id)", sql)
        self.assertIn("WHERE r.id::text = c.doc_id", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS regulation_status_history", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS regulation_relations", sql)

    def test_normalize_legal_status_handles_dicabut_sebagian(self):
        self.assertEqual(normalize_legal_status("Dicabut sebagian"), "dicabut_sebagian")
        self.assertEqual(normalize_legal_status("Mencabut sebagian PP 71/2019"), "dicabut_sebagian")
        self.assertEqual(normalize_legal_status("Dicabut dengan UU 1/2024"), "dicabut")
        self.assertEqual(normalize_legal_status("Tidak Berlaku"), "dicabut")
        self.assertEqual(normalize_legal_status("Diubah dengan UU 1/2024"), "diubah")
        self.assertEqual(normalize_legal_status("Berlaku"), "berlaku")
        # A new law that "mencabut" an older law is not itself dicabut
        self.assertNotEqual(normalize_legal_status("Mencabut UU 11/2008"), "dicabut")


class TestAdapterHtmlStatusParsing(unittest.TestCase):
    def test_parse_status_html_extracts_status_note_and_relations(self):
        html = """
        <html><body>
          <span class="badge status-badge">Diubah</span>
          <p>Status: Diubah dengan UU Nomor 1 Tahun 2024 tentang Perubahan Kedua UU ITE.</p>
          <p>Mencabut sebagian: PP Nomor 71 Tahun 2019</p>
          <p>Putusan MK Nomor 20/PUU-XIV/2016</p>
        </body></html>
        """
        adapter = get_adapter("bpk")
        parsed = adapter.parse_status_html(html, "https://peraturan.bpk.go.id/Details/123")
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["legal_status"], "diubah")
        self.assertIn("Diubah dengan UU Nomor 1 Tahun 2024", parsed["status_note"])
        mk_parsed = adapter.parse_status_html(
            '<span class="badge">Berlaku</span><p>Putusan MK Nomor 20/PUU-XIV/2016</p>',
            "https://peraturan.bpk.go.id/Details/124",
        )
        self.assertIn("Putusan MK", mk_parsed["status_note"])
        rel_keys = {(r["relation_type"], r["canonical_key"], r["direction"]) for r in parsed["relations"]}
        self.assertIn(("AMENDED_BY", "UU|1|2024", "incoming"), rel_keys)
        self.assertIn(("PARTIALLY_REPEALED_BY", "PP|71|2019", "outgoing"), rel_keys)


class _FakeCursorResult:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class TestStatusUpdaterAndWorkers(unittest.TestCase):
    def test_apply_status_transition_syncs_chunks_and_history(self):
        conn = MagicMock()
        conn.execute.side_effect = [
            _FakeCursorResult(row={"id": 42, "legal_status": "berlaku", "status_note": None}),
            _FakeCursorResult(),  # UPDATE regulations
            _FakeCursorResult(),  # UPDATE chunks
            _FakeCursorResult(),  # INSERT regulation_status_history
        ]
        changed = apply_status_transition(
            conn,
            regulation_id=42,
            new_status="dicabut",
            trigger_source="delta_poll_cascade",
            related_canonical_key="UU|1|2024",
            note="Dicabut UU 1/2024",
        )
        self.assertTrue(changed)
        calls = [c.args for c in conn.execute.call_args_list]
        self.assertIn("UPDATE chunks SET is_active_law = %s WHERE doc_id = %s", calls[2][0])
        self.assertEqual(calls[2][1], (False, "42"))
        self.assertIn("INSERT INTO regulation_status_history", calls[3][0])

    def test_cascade_does_not_downgrade_dicabut_to_diubah(self):
        conn = MagicMock()
        conn.execute.side_effect = [
            _FakeCursorResult(row={"id": 10, "legal_status": "dicabut"}),  # target lookup
            _FakeCursorResult(),  # INSERT regulation_relations
        ]
        changed = cascade_relation_to_target(
            conn,
            source_canonical_key="UU|2|2020",
            source_regulation_id=None,
            target_canonical_key="UU|11|2008",
            relation_type="AMENDED_BY",
            trigger_source="delta_poll_cascade",
        )
        self.assertFalse(changed)

    def test_heartbeat_circuit_breaker_skips_failing_domain(self):
        candidates = [
            {"id": i, "canonical_key": f"UU|{i}|2020", "legal_status": "berlaku", "detail_url": f"https://down.go.id/reg/{i}"}
            for i in range(1, 6)
        ]
        fake_conn = MagicMock()
        fake_conn.__enter__ = MagicMock(return_value=fake_conn)
        fake_conn.__exit__ = MagicMock(return_value=False)

        fake_adapter = MagicMock()
        fake_adapter.fetch_status_metadata.return_value = {"ok": False, "error": "portal_unreachable"}

        with (
            patch("services.freshness.heartbeat_worker.pg_service.get_conn", return_value=fake_conn),
            patch("services.freshness.heartbeat_worker.select_heartbeat_candidates", return_value=candidates),
            patch("services.freshness.heartbeat_worker.get_adapter_for_url", return_value=fake_adapter),
            patch("services.freshness.heartbeat_worker.time.sleep"),
        ):
            summary = run_heartbeat_batch(batch_size=5, domain_jitter_s=0.0)

        self.assertEqual(summary["checked"], 3)
        self.assertEqual(summary["unreachable"], 3)
        self.assertEqual(summary["circuit_skipped"], 2)

    def test_delta_poller_enqueues_new_and_cascades_repeal(self):
        fake_adapter = MagicMock()
        fake_adapter.fetch_listings.return_value = [
            {
                "source_key": "bpk",
                "jenis": "UU",
                "nomor": "1",
                "tahun": 2024,
                "judul": "UU No. 1 Tahun 2024",
                "tentang": "Perubahan UU ITE",
                "sektor": "Nasional",
                "detail_url": "https://peraturan.bpk.go.id/Details/999",
                "download_url": "https://peraturan.bpk.go.id/Download/999.pdf",
                "legal_status": "berlaku",
                "relations": [
                    {"relation_type": "REPEALED_BY", "canonical_key": "UU|11|2008", "direction": "outgoing"}
                ],
            }
        ]
        fake_conn = MagicMock()
        fake_conn.__enter__ = MagicMock(return_value=fake_conn)
        fake_conn.__exit__ = MagicMock(return_value=False)
        fake_conn.execute.return_value = _FakeCursorResult(rows=[])

        with (
            patch("services.freshness.delta_poller.get_adapter", return_value=fake_adapter),
            patch("services.freshness.delta_poller.pg_service.get_conn", return_value=fake_conn),
            patch("services.freshness.delta_poller.classify_regulation_category", return_value="ICT_CYBER"),
            patch("services.freshness.delta_poller.cascade_relation_to_target", return_value=True) as mock_cascade,
        ):
            summary = run_delta_poll(portals=("bpk",), max_pages=1, min_year=2024)

        self.assertEqual(summary["enqueued_new"], 1)
        self.assertEqual(summary["cascaded_updates"], 1)
        mock_cascade.assert_called_once()


if __name__ == "__main__":
    unittest.main()
