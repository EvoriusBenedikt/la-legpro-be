"""Unit tests for the per-clause commercial-position helpers (no DB, no LLM)."""
import json

from services import favorability as fav


def test_normalize_peran_accepts_fixed_set_case_insensitively():
    assert fav.normalize_peran("penyedia") == "Penyedia"
    assert fav.normalize_peran(" PENERIMA KERJA ") == "Penerima Kerja"


def test_normalize_peran_drops_free_text():
    assert fav.normalize_peran("Penyedia Layanan TI") is None
    assert fav.normalize_peran("") is None
    assert fav.normalize_peran(None) is None


def test_normalize_position_rescues_chatty_values():
    assert fav.normalize_position("menguntungkan") == "MENGUNTUNGKAN"
    assert fav.normalize_position("Merugikan bagi perusahaan") == "MERUGIKAN"
    assert fav.normalize_position("entah") == "NETRAL"
    assert fav.normalize_position(None) == "NETRAL"


def test_normalize_bullets_caps_and_coerces():
    raw = [
        {"poin": "Bayar di muka", "alasan": "Kas aman", "dasar": "hukum"},
        "Klausul putus sepihak",
        {"poin": ""},
        {"poin": "Sanksi", "alasan": "", "dasar": "aneh"},
        {"poin": "Keempat"},
    ]
    out = fav.normalize_bullets(raw)
    assert len(out) == 3
    assert out[0] == {"poin": "Bayar di muka", "alasan": "Kas aman", "dasar": "hukum"}
    assert out[1]["poin"] == "Klausul putus sepihak"
    assert out[1]["dasar"] == "komersial"
    assert out[2]["dasar"] == "komersial"  # unknown dasar normalised


def test_normalize_bullets_handles_garbage():
    assert fav.normalize_bullets(None) == []
    assert fav.normalize_bullets("bukan list") == []
    assert fav.normalize_bullets([{}]) == []


def test_apply_position_always_sets_all_four_keys():
    result = {"pasal": "Pasal 1", "status": "BERESIKO"}
    out = fav.apply_position(result, {})
    for key in fav.POSITION_KEYS:
        assert key in out
    assert out["posisi_perusahaan"] == "NETRAL"
    assert out["kelebihan"] == []


def test_empty_position_returns_fresh_lists():
    a, b = fav.empty_position(), fav.empty_position()
    a["kelebihan"].append("x")
    assert b["kelebihan"] == []


def test_position_prompt_block_names_our_side():
    block = fav.position_prompt_block("PT TCI", "Penyedia")
    assert "PT TCI" in block
    assert "Penyedia" in block
    assert "MENGUNTUNGKAN" in block and "MERUGIKAN" in block


def test_sweep_prompt_batches_and_truncates():
    clauses = [{"pasal": f"Pasal {i}", "isi_pasal": "x" * 900} for i in range(25)]
    prompt = fav.build_sweep_prompt(clauses, "PKS", "PT TCI", "Penyedia")
    assert "Pasal 24" in prompt
    assert "x" * 601 not in prompt  # body capped at SWEEP_CHARS
    assert "PT TCI" in prompt


def _clause(pasal, status="TIDAK DIATUR", body="Isi klausul komersial."):
    return {
        "pasal": pasal,
        "isi_pasal": body,
        "status": status,
        **fav.empty_position(),
    }


def test_sweep_fills_only_unregulated_neutral_rows():
    results = [
        _clause("Pasal 1"),
        {**_clause("Pasal 2"), "posisi_perusahaan": "MENGUNTUNGKAN"},
        {**_clause("Pasal 3", status="SESUAI"), "posisi_perusahaan": "NETRAL"},
        _clause("Pasal 4"),
    ]
    reply = json.dumps({"posisi": [
        {"pasal": "Pasal 1", "posisi_perusahaan": "MERUGIKAN",
         "kekurangan": [{"poin": "Denda tanpa batas", "alasan": "Eksposur", "dasar": "komersial"}],
         "catatan_posisi": "Satu sisi."},
        {"pasal": "Pasal 2", "posisi_perusahaan": "MERUGIKAN"},   # must be left alone
        {"pasal": "Pasal 3", "posisi_perusahaan": "MERUGIKAN"},   # SESUAI: not swept
        {"pasal": "Pasal 4", "posisi_perusahaan": "MENGUNTUNGKAN",
         "kelebihan": [{"poin": "Pembayaran cepat", "dasar": "komersial"}]},
    ]})

    calls = []

    def fake_call(messages, temperature=0.1, timeout=90):
        calls.append(messages)
        return reply

    filled = fav.sweep(results, "PKS", "PT TCI", "Penyedia", call=fake_call)
    assert filled == 2
    assert results[0]["posisi_perusahaan"] == "MERUGIKAN"
    assert results[0]["kekurangan"][0]["poin"] == "Denda tanpa batas"
    assert results[1]["posisi_perusahaan"] == "MENGUNTUNGKAN"
    assert results[2]["posisi_perusahaan"] == "NETRAL"
    assert results[3]["posisi_perusahaan"] == "MENGUNTUNGKAN"
    assert len(calls) == 1


def test_sweep_batches_twenty_per_call():
    results = [_clause(f"Pasal {i}") for i in range(45)]
    calls = []

    def fake_call(messages, temperature=0.1, timeout=90):
        calls.append(messages)
        return json.dumps({"posisi": []})

    fav.sweep(results, "PKS", "PT TCI", None, call=fake_call)
    assert len(calls) == 3  # 20 + 20 + 5


def test_sweep_survives_caller_failure():
    results = [_clause("Pasal 1")]

    def boom(*_a, **_k):
        raise RuntimeError("network down")

    assert fav.sweep(results, "PKS", "PT TCI", None, call=boom) == 0
    assert results[0]["posisi_perusahaan"] == "NETRAL"


def test_sweep_matches_duplicate_labels_in_order():
    results = [_clause("Pasal 7"), _clause("Pasal 7")]
    reply = json.dumps({"posisi": [
        {"pasal": "Pasal 7", "posisi_perusahaan": "MENGUNTUNGKAN"},
        {"pasal": "Pasal 7", "posisi_perusahaan": "MERUGIKAN"},
    ]})
    fav.sweep(results, "PKS", "PT TCI", None, call=lambda *a, **k: reply)
    assert [r["posisi_perusahaan"] for r in results] == ["MENGUNTUNGKAN", "MERUGIKAN"]
