"""Unit tests for Reverse-RAG missing & incomplete clause detection."""
import json
import os
import sys
import types
import unittest
from unittest.mock import patch

API_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

try:
    from services import gap_detector  # noqa: E402
except ModuleNotFoundError:
    # Host Python lacks container deps (fastapi); stub services.llm_client for unit testing
    stub_llm = types.ModuleType("services.llm_client")
    stub_llm.call_llm = lambda *a, **kw: "{}"
    stub_llm.extract_json = lambda raw: json.loads(raw)
    sys.modules["services.llm_client"] = stub_llm
    from services import gap_detector  # noqa: E402


class TestGapDetector(unittest.TestCase):
    def test_archetype_normalization_and_fallback(self):
        self.assertEqual(gap_detector.normalize_archetype("DSA"), "DSA")
        self.assertEqual(gap_detector.normalize_archetype("Non-Disclosure Agreement"), "NDA")
        self.assertEqual(
            gap_detector.normalize_archetype("Kontrak", fallback_text="PERJANJIAN KERAHASIAAN (NON-DISCLOSURE AGREEMENT)"),
            "NDA",
        )
        self.assertEqual(
            gap_detector.normalize_archetype("PKS", fallback_text="Perjanjian Distribusi dan Berbagi Data (Data Sharing)"),
            "DSA",
        )

    def test_dsa_flags_missing_margin_split_and_nda_does_not(self):
        dsa_clauses = [
            {"pasal": "Pasal 1 Definisi", "deskripsi": "Layanan adalah platform distribusi."},
            {"pasal": "Pasal 2 Ruang Lingkup", "deskripsi": "Para Pihak bekerja sama memasarkan layanan."},
        ]
        nda_clauses = [
            {"pasal": "Pasal 1 Kerahasiaan", "deskripsi": "Informasi Rahasia wajib dijaga selama 3 tahun setelah berakhirnya perjanjian, kecuali yang sudah menjadi ranah publik atau diminta pengadilan."},
        ]

        with patch.object(gap_detector, "call_llm", side_effect=RuntimeError("offline")):
            arch_dsa, gaps_dsa = gap_detector.detect_missing_clauses(
                pasal_items=dsa_clauses,
                contract_type="DSA",
                pihak_kita="PT Lintasarta",
                peran_kita="Penyedia",
            )
            arch_nda, gaps_nda = gap_detector.detect_missing_clauses(
                pasal_items=nda_clauses,
                contract_type="NDA",
                pihak_kita="PT Lintasarta",
            )

        self.assertEqual(arch_dsa, "DSA")
        self.assertIn("margin_revenue_split", {g["clause_key"] for g in gaps_dsa})
        self.assertEqual(arch_nda, "NDA")
        self.assertNotIn("margin_revenue_split", {g["clause_key"] for g in gaps_nda})

    def test_present_suppressed_and_vague_incomplete_keeps_excerpt(self):
        dsa_clauses = [
            {
                "pasal": "Pasal 4 Skema Pembagian Pendapatan",
                "deskripsi": "Para Pihak sepakat membagi pendapatan bersih 80% untuk Pihak Pertama dan 20% untuk Pihak Kedua setiap bulan.",
            },
            {
                "pasal": "Pasal 5 Pembayaran",
                "deskripsi": "Pembayaran akan diatur kemudian oleh Para Pihak.",
            },
        ]
        mock_reply = json.dumps({
            "assessments": [
                {"clause_key": "margin_revenue_split", "state": "PRESENT"},
                {
                    "clause_key": "payment_invoicing_terms",
                    "state": "VAGUE_INCOMPLETE",
                    "why_dangerous": "Payment timeline and tax responsibilities are deferred.",
                    "advisory_guidance": "Specify Net 30 due date and PPN/PPh 23.",
                    "recommended_elements": ["Due date", "BAST trigger", "Tax split"],
                    "suggested_insertion_anchor": "Pasal 5 Pembayaran",
                    "evidence_excerpt": "Pembayaran akan diatur kemudian oleh Para Pihak.",
                },
            ]
        })

        with patch.object(gap_detector, "call_llm", return_value=mock_reply):
            _, gaps = gap_detector.detect_missing_clauses(
                pasal_items=dsa_clauses,
                contract_type="DSA",
                pihak_kita="PT Lintasarta",
                peran_kita="Penyedia",
            )

        by_key = {g["clause_key"]: g for g in gaps}
        self.assertNotIn("margin_revenue_split", by_key)
        self.assertEqual(by_key["payment_invoicing_terms"]["detection_state"], "vague_incomplete")
        self.assertIn("diatur kemudian", by_key["payment_invoicing_terms"]["evidence_excerpt"])

    def test_title_and_filename_override_recital_keywords(self):
        nda_with_cooperation_recitals = (
            "PERJANJIAN KERAHASIAAN (NON-DISCLOSURE AGREEMENT)\n"
            "ANTARA PT A DAN PT B\n"
            "Menimbang bahwa para pihak bermaksud menjajaki kerja sama dan kemitraan strategis.\n"
        )
        self.assertEqual(
            gap_detector.normalize_archetype("PKS", fallback_text=nda_with_cooperation_recitals),
            "NDA",
        )
        self.assertEqual(
            gap_detector.normalize_archetype(
                "GENERAL",
                fallback_text="PERJANJIAN\nANTARA PT A DAN PT B",
                filename="Draft_Reseller_Agreement_2026.pdf",
            ),
            "RESELLER",
        )
        self.assertEqual(
            gap_detector.normalize_archetype(
                "PKS",
                fallback_text="PERJANJIAN KERJA SAMA RESELLER\nANTARA PT A DAN PT B",
            ),
            "RESELLER",
        )

    def test_nda_and_reseller_flag_expected_missing_clauses(self):
        minimal_clauses = [
            {"pasal": "Pasal 1 Para Pihak", "deskripsi": "Para pihak sepakat membuat perjanjian ini."},
        ]
        with patch.object(gap_detector, "call_llm", side_effect=RuntimeError("offline")):
            arch_nda, gaps_nda = gap_detector.detect_missing_clauses(
                pasal_items=minimal_clauses,
                contract_type="GENERAL",
                full_text="NON-DISCLOSURE AGREEMENT\nBETWEEN PT A AND PT B",
            )
            arch_reseller, gaps_reseller = gap_detector.detect_missing_clauses(
                pasal_items=minimal_clauses,
                contract_type="GENERAL",
                full_text="PERJANJIAN RESELLER\nANTARA PT A DAN PT B",
            )

        self.assertEqual(arch_nda, "NDA")
        nda_keys = {g["clause_key"] for g in gaps_nda}
        self.assertIn("nda_confidential_info_definition", nda_keys)
        self.assertIn("nda_return_destruction_remedies", nda_keys)

        self.assertEqual(arch_reseller, "RESELLER")
        reseller_keys = {g["clause_key"] for g in gaps_reseller}
        self.assertIn("reseller_territory_exclusivity_terms", reseller_keys)
        self.assertIn("margin_revenue_split", reseller_keys)

    def test_workspace_document_classes_and_playbooks(self):
        cases = [
            ("ADDENDUM PERJANJIAN KERJA SAMA", "ADDENDUM", "addendum_principal_reference"),
            ("BERITA ACARA SERAH TERIMA (BAST)", "BAST", "bast_deliverables_signoff"),
            ("DATA PROCESSING AGREEMENT (DPA)", "DPA", "dpa_subprocessor_crossborder_security"),
            ("SURAT PERINTAH KERJA (SPK)", "SPK", "spk_sow_reference_timeline_penalty"),
            ("STATEMENT OF WORK (SOW)", "SOW", "spk_sow_reference_timeline_penalty"),
            ("PURCHASE ORDER NO. 123", "PO", "po_item_specs_delivery_terms"),
            ("PURCHASE AGREEMENT", "PROCUREMENT", "po_item_specs_delivery_terms"),
            ("SERVICE LEVEL AGREEMENT (SLA)", "SLA", "sla_metrics_uptime_credits"),
            ("MEMORANDUM OF UNDERSTANDING (MoU)", "MOU", "mou_binding_status"),
            ("SOFTWARE LICENSE AGREEMENT", "LICENSE", "license_grant_restrictions"),
        ]
        minimal_clauses = [{"pasal": "Pasal 1 Umum", "deskripsi": "Ketentuan umum."}]
        with patch.object(gap_detector, "call_llm", side_effect=RuntimeError("offline")):
            for title, expected_arch, expected_rule in cases:
                arch, gaps = gap_detector.detect_missing_clauses(
                    pasal_items=minimal_clauses,
                    contract_type="GENERAL",
                    full_text=f"{title}\nANTARA PT A DAN PT B",
                )
                self.assertEqual(arch, expected_arch, msg=f"Failed archetype for {title}")
                self.assertIn(expected_rule, {g["clause_key"] for g in gaps}, msg=f"Missing rule for {title}")


if __name__ == "__main__":
    unittest.main()
