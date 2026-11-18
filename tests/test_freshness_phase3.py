"""Unit tests for Phase 3 of Live Regulation Freshness & Status Verification."""
from __future__ import annotations

import datetime
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "api"))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

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
        _lxml = types.ModuleType("lxml")
        _lhtml = types.ModuleType("lxml.html")
        _lxml.html = _lhtml
        sys.modules["lxml"] = _lxml
        sys.modules["lxml.html"] = _lhtml

from services.freshness.live_verifier import (  # noqa: E402
    get_amendment_chain,
    record_regulation_matches,
    verify_regulation_live,
)


class _FakeResult:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class TestLiveVerifierPhase3(unittest.TestCase):
    def _mock_conn(self, side_effects):
        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.execute.side_effect = side_effects
        return conn

    def test_verify_live_timeout_returns_cached_portal_unreachable(self):
        prev_verified = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=datetime.timezone.utc)
        reg_row = {
            "id": 7,
            "canonical_key": "UU|27|2022",
            "legal_status": "berlaku",
            "status_note": None,
            "last_verified_at": prev_verified,
            "last_verification_result": "verified_ok",
            "detail_url": "https://peraturan.bpk.go.id/Details/229798",
        }
        conn1 = self._mock_conn([_FakeResult(row=reg_row)])
        conn2 = self._mock_conn([_FakeResult()])

        fake_adapter = MagicMock()
        fake_adapter.fetch_status_metadata.return_value = {"ok": False, "error": "portal_unreachable"}

        with (
            patch("services.freshness.live_verifier.pg_service.get_conn", side_effect=[conn1, conn2]),
            patch("services.freshness.live_verifier.get_adapter_for_url", return_value=fake_adapter),
        ):
            res = verify_regulation_live(7, cooldown_seconds=60)

        self.assertEqual(res["verification_state"], "cached_portal_unreachable")
        self.assertEqual(res["legal_status"], "berlaku")
        self.assertEqual(res["last_verified_at"], prev_verified.isoformat())

    def test_verify_live_detects_status_change(self):
        old_verified = datetime.datetime(2026, 11, 1, 0, 0, tzinfo=datetime.timezone.utc)
        new_verified = datetime.datetime(2026, 11, 18, 5, 0, tzinfo=datetime.timezone.utc)
        reg_row = {
            "id": 9,
            "canonical_key": "UU|11|2008",
            "legal_status": "berlaku",
            "status_note": None,
            "last_verified_at": old_verified,
            "last_verification_result": "verified_ok",
            "detail_url": "https://peraturan.bpk.go.id/Details/123",
        }
        updated_row = {
            "legal_status": "diubah",
            "status_note": "Diubah dengan UU No. 1 Tahun 2024",
            "last_verified_at": new_verified,
        }
        conn1 = self._mock_conn([_FakeResult(row=reg_row)])
        conn2 = self._mock_conn([_FakeResult(row=updated_row)])

        fake_adapter = MagicMock()
        fake_adapter.fetch_status_metadata.return_value = {
            "ok": True,
            "legal_status": "diubah",
            "status_note": "Diubah dengan UU No. 1 Tahun 2024",
            "relations": [],
        }

        with (
            patch("services.freshness.live_verifier.pg_service.get_conn", side_effect=[conn1, conn2]),
            patch("services.freshness.live_verifier.get_adapter_for_url", return_value=fake_adapter),
            patch("services.freshness.live_verifier.apply_status_transition", return_value=True),
        ):
            res = verify_regulation_live(9, cooldown_seconds=60)

        self.assertEqual(res["verification_state"], "verified_live")
        self.assertEqual(res["legal_status"], "diubah")
        self.assertTrue(res["status_changed"])
        self.assertEqual(res["last_verified_at"], new_verified.isoformat())

    def test_verify_live_respects_cooldown(self):
        just_now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=10)
        reg_row = {
            "id": 12,
            "canonical_key": "PP|71|2019",
            "legal_status": "berlaku",
            "status_note": None,
            "last_verified_at": just_now,
            "last_verification_result": "verified_ok",
            "detail_url": "https://peraturan.bpk.go.id/Details/456",
        }
        conn1 = self._mock_conn([_FakeResult(row=reg_row)])

        with patch("services.freshness.live_verifier.pg_service.get_conn", return_value=conn1):
            res = verify_regulation_live(12, cooldown_seconds=60)

        self.assertEqual(res["verification_state"], "cached_cooldown")

    def test_get_amendment_chain_and_record_matches(self):
        conn = MagicMock()
        conn.execute.side_effect = [
            _FakeResult(rows=[
                {
                    "target_regulation_id": 5,
                    "relation_type": "AMENDED_BY",
                    "source_canonical_key": "UU|1|2024",
                    "source_regulation_id": 99,
                    "note": "Perubahan kedua",
                    "jenis": "UU",
                    "nomor": "1",
                    "tahun": 2024,
                    "tentang": "UU ITE",
                    "detail_url": "https://peraturan.bpk.go.id/Details/99",
                }
            ]),
            _FakeResult(),
            _FakeResult(rows=[{"id": 5}]),
        ]
        chain = get_amendment_chain(conn, [5])
        self.assertIn(5, chain)
        self.assertEqual(chain[5][0]["canonical_key"], "UU|1|2024")

        stale = record_regulation_matches(conn, [5, 6])
        self.assertEqual(stale, [5])


if __name__ == "__main__":
    unittest.main()
