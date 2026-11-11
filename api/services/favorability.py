"""Per-clause commercial position ("pluses and minuses") for our company.

Compliance and commercial position are deliberately separate axes: a clause
can be fully lawful and still one-sided. This module never touches ``status``
or ``skor_kepatuhan``; it only fills four fields on a clause result:

    kelebihan[]        max 3 x {poin, alasan, dasar}
    kekurangan[]       max 3 x {poin, alasan, dasar}
    posisi_perusahaan  MENGUNTUNGKAN | NETRAL | MERUGIKAN
    catatan_posisi     one sentence

Two producers:

* the per-clause analyzer prompt (``compliance.process_single_pasal``) asks
  for the fields inline, so the call count stays flat;
* :func:`sweep` fills the clauses that never reached the LLM at all — the
  ``TIDAK DIATUR`` branch returns before any call — in batches of 20.

``dasar`` ("hukum" | "komersial") keeps legal claims visibly distinct from
business judgement in the UI. Nothing here decides legality.
"""
import re

from services.llm_client import call_glm, extract_json

MAX_BULLETS = 3
SWEEP_BATCH = 20
SWEEP_CHARS = 600

POSISI = ("MENGUNTUNGKAN", "NETRAL", "MERUGIKAN")
DASAR = ("hukum", "komersial")

# Fixed role set (agreed 2026-11-11): the upload modal offers exactly these, so
# the summary chip and the draft-comparison copy stay consistent. Anything else
# is dropped rather than echoed into a badge.
PERAN_CHOICES = ("Penyedia", "Pelanggan", "Pemberi Kerja", "Penerima Kerja")

POSITION_KEYS = ("kelebihan", "kekurangan", "posisi_perusahaan", "catatan_posisi")


def normalize_peran(raw) -> str | None:
    """Keep only a known role label; unknown / free text becomes None."""
    if not raw:
        return None
    text = str(raw).strip()
    for choice in PERAN_CHOICES:
        if text.lower() == choice.lower():
            return choice
    return None


def empty_position() -> dict:
    """Neutral placeholder used by the early-return and error branches."""
    return {
        "kelebihan": [],
        "kekurangan": [],
        "posisi_perusahaan": "NETRAL",
        "catatan_posisi": "",
    }


def normalize_position(raw) -> str:
    value = str(raw or "").strip().upper()
    if value in POSISI:
        return value
    # Substring rescue for chatty models ("Menguntungkan bagi perusahaan").
    for candidate in POSISI:
        if candidate in value:
            return candidate
    return "NETRAL"


def normalize_bullets(raw, limit: int = MAX_BULLETS) -> list[dict]:
    """Coerce the model's list into ``[{poin, alasan, dasar}]`` and cap it.

    Accepts plain strings as well as dicts so a lazy model reply still renders
    something useful instead of an empty column.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if isinstance(item, str):
            poin = item.strip()
            if not poin:
                continue
            out.append({"poin": poin[:200], "alasan": "", "dasar": "komersial"})
        elif isinstance(item, dict):
            poin = str(item.get("poin") or item.get("point") or "").strip()
            alasan = str(item.get("alasan") or item.get("reason") or "").strip()
            dasar = str(item.get("dasar") or item.get("basis") or "").strip().lower()
            if not poin:
                continue
            out.append({
                "poin": poin[:200],
                "alasan": alasan[:400],
                "dasar": dasar if dasar in DASAR else "komersial",
            })
        if len(out) >= limit:
            break
    return out


def apply_position(result: dict, ans: dict) -> dict:
    """Merge the model's position fields into a clause result dict."""
    result["kelebihan"] = normalize_bullets(ans.get("kelebihan"))
    result["kekurangan"] = normalize_bullets(ans.get("kekurangan"))
    result["posisi_perusahaan"] = normalize_position(ans.get("posisi_perusahaan"))
    result["catatan_posisi"] = str(ans.get("catatan_posisi") or "").strip()[:400]
    return result


def position_prompt_block(pihak_kita: str, peran_kita: str | None) -> str:
    """Instruction 5 appended to the per-clause analyzer prompt."""
    peran = f", {peran_kita}" if peran_kita else ""
    return (
        "5. Posisi komersial: nilai apakah klausul ini MENGUNTUNGKAN atau MERUGIKAN "
        f"bagi {pihak_kita}{peran} — bukan bagi pihak lain. "
        f"Tulis maksimal {MAX_BULLETS} kelebihan dan {MAX_BULLETS} kekurangan yang material. "
        'Isi "dasar" dengan "hukum" bila klaim didukung pasal regulasi di atas, '
        'selain itu "komersial".\n\n'
    )


def build_sweep_prompt(clauses: list[dict], doc_type: str, pihak_kita: str,
                       peran_kita: str | None) -> str:
    peran = f" ({peran_kita})" if peran_kita else ""
    blocks = []
    for c in clauses:
        body = str(c.get("isi_pasal") or "")[:SWEEP_CHARS]
        blocks.append(f'[{c.get("pasal", "Pasal")}]\n{body}')
    joined = "\n\n".join(blocks)
    return (
        "Anda adalah penasihat kontrak komersial senior Indonesia. "
        "Tugas Anda HANYA menilai posisi komersial klausul bagi satu pihak, "
        "bukan menilai kepatuhan regulasi.\n\n"
        f"KONTEKS:\n- Jenis dokumen: {doc_type}\n"
        f"- Pihak yang kami wakili: {pihak_kita}{peran}\n\n"
        f"KLAUSUL:\n{joined}\n\n"
        "Untuk SETIAP klausul di atas, tentukan apakah klausul itu menguntungkan "
        "atau merugikan pihak yang kami wakili. Maksimal "
        f"{MAX_BULLETS} kelebihan dan {MAX_BULLETS} kekurangan per klausul, "
        "hanya yang material. Alasan harus konkret dan singkat (maksimal 1 kalimat).\n\n"
        "Kembalikan HANYA JSON berikut (tanpa markdown, tanpa teks lain):\n"
        '{\n'
        '  "posisi": [\n'
        '    {\n'
        '      "pasal": "label persis seperti di atas",\n'
        '      "posisi_perusahaan": "MENGUNTUNGKAN" | "NETRAL" | "MERUGIKAN",\n'
        '      "kelebihan": [{"poin": "...", "alasan": "...", "dasar": "hukum|komersial"}],\n'
        '      "kekurangan": [{"poin": "...", "alasan": "...", "dasar": "hukum|komersial"}],\n'
        '      "catatan_posisi": "satu kalimat"\n'
        '    }\n'
        '  ]\n'
        '}'
    )


def label_key(label) -> str:
    """Cheap canonical form for matching a clause label across drafts / LLM rows.

    Mirrors the essentials of ``rag_service._normalize_clause_label`` without
    importing the RAG stack (which loads the embedder) into this module.
    """
    text = re.sub(r"\s+", " ", str(label or "").strip().lower())
    m = re.match(r"^(?:pasal|pasa[il1]|article)[,\s]+([0-9a-z]+)", text)
    if m:
        num = m.group(1).upper()
        num = (num.replace("O", "0").replace("I", "1").replace("L", "1")
                  .replace("T", "1").replace("S", "5").replace("B", "8"))
        return f"pasal {num.lower()}"
    return text[:80]


def _merge_batch(batch: list[dict], rows: list) -> int:
    """Fill ``batch`` in place from the model's rows. Returns how many changed."""
    pending: dict[str, list[dict]] = {}
    for clause in batch:
        pending.setdefault(label_key(clause.get("pasal")), []).append(clause)

    filled = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        bucket = pending.get(label_key(row.get("pasal")))
        if not bucket:
            continue
        apply_position(bucket.pop(0), row)
        filled += 1
    return filled


def sweep(results: list[dict], doc_type: str, pihak_kita: str,
          peran_kita: str | None = None, call=None) -> int:
    """Fill position fields for clauses that never reached the LLM.

    Only ``TIDAK DIATUR`` rows are touched — they are the ones whose branch in
    ``process_single_pasal`` returns before any model call. Rows that already
    carry a position (bullets or a note) are left alone, so a failed batch
    degrades to the previous behaviour instead of blanking data. Returns the
    number of clauses filled.
    """
    todo = [r for r in results
            if r.get("status") == "TIDAK DIATUR"
            and r.get("posisi_perusahaan") == "NETRAL"
            and not r.get("kelebihan")
            and not r.get("kekurangan")
            and not r.get("catatan_posisi")]
    if not todo:
        return 0

    caller = call or call_glm
    filled = 0
    for start in range(0, len(todo), SWEEP_BATCH):
        batch = todo[start:start + SWEEP_BATCH]
        try:
            raw = caller(
                [{"role": "user",
                  "content": build_sweep_prompt(batch, doc_type, pihak_kita, peran_kita)}],
                temperature=0.1,
                timeout=90,
            )
            ans = extract_json(raw)
        except Exception as e:  # network / parse: keep the neutral placeholders
            print(f"[Favorability] sweep batch skipped: {e}")
            continue

        rows = ans.get("posisi") if isinstance(ans, dict) else ans
        if not isinstance(rows, list):
            print("[Favorability] sweep reply has no 'posisi' list")
            continue
        filled += _merge_batch(batch, rows)
    return filled
