"""Knowledge-base staleness tracking and real compliance re-evaluation."""
from __future__ import annotations

import concurrent.futures
import html as html_lib
import json
import re
from typing import Optional

from fastapi import HTTPException
from lxml import html as lhtml

from services import pg_service

COSINE_THRESHOLD = 0.38


def ensure_kb_sync_columns() -> None:
    """Idempotently add kb_synced_at, kb_stale, kb_stale_reason to compliance_history."""
    try:
        pg_service.execute(
            """
            ALTER TABLE compliance_history
                ADD COLUMN IF NOT EXISTS kb_synced_at TIMESTAMPTZ DEFAULT NOW(),
                ADD COLUMN IF NOT EXISTS kb_stale BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS kb_stale_reason TEXT;
            """
        )
    except Exception as e:
        print(f"[KBSync] Column check skipped: {e}")


def mark_report_stale(report_id: str, reason: str) -> None:
    """Mark a single compliance report as stale after workspace reference changes."""
    if not report_id:
        return
    try:
        ensure_kb_sync_columns()
        pg_service.execute(
            "UPDATE compliance_history SET kb_stale = TRUE, kb_stale_reason = %s WHERE id = %s",
            (reason, report_id),
        )
    except Exception as e:
        print(f"[KBSync] mark_report_stale failed for {report_id}: {e}")


def mark_all_reports_stale(reason: str) -> None:
    """Mark analyzed compliance reports as stale when public regulations change."""
    try:
        ensure_kb_sync_columns()
        pg_service.execute(
            "UPDATE compliance_history SET kb_stale = TRUE, kb_stale_reason = %s "
            "WHERE results_json IS NOT NULL AND kb_stale = FALSE",
            (reason,),
        )
    except Exception as e:
        print(f"[KBSync] mark_all_reports_stale failed: {e}")


def html_to_plain_for_recheck(content_html: str) -> str:
    """Convert editor HTML to clean plain text, dropping <del> tracked deletions."""
    if not content_html:
        return ""
    try:
        frag = lhtml.fragment_fromstring(f"<div>{content_html}</div>")
        for del_el in list(frag.xpath(".//del")):
            parent = del_el.getparent()
            if parent is not None:
                if del_el.tail:
                    prev = del_el.getprevious()
                    if prev is not None:
                        prev.tail = (prev.tail or "") + del_el.tail
                    else:
                        parent.text = (parent.text or "") + del_el.tail
                parent.remove(del_el)
        blocks: list[str] = []
        for el in frag.iter("p", "li", "h1", "h2", "h3", "h4", "tr", "div"):
            if el is frag:
                continue
            if el.tag == "div" and el.xpath("./p|./h1|./h2|./h3|./h4|./li|./table"):
                continue
            txt = re.sub(r"\s+", " ", el.text_content() or "").strip()
            if txt:
                blocks.append(txt)
        if blocks:
            return "\n\n".join(blocks)
    except Exception:
        pass
    cleaned = re.sub(r"<del\b[^>]*>.*?</del>", "", content_html, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"</(?:p|div|h[1-6]|li|tr)>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html_lib.unescape(cleaned)
    return re.sub(r"[ \t]+", " ", cleaned).strip()


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\u00a0", " ")).strip()


def _parse_results_json(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def recheck_report_compliance(
    report_id: str,
    content_html: Optional[str],
    user_id: str,
) -> dict:
    """Re-evaluate clauses against the live knowledge base & workspace references,
    persist updated results_json, and clear kb_stale."""
    from routers.compliance import process_single_pasal
    from services.rag_service import extract_pasal_items, get_user_exclusions

    ensure_kb_sync_columns()
    row = pg_service.query_one(
        "SELECT id, results_json, COALESCE(kb_stale, FALSE) AS kb_stale "
        "FROM compliance_history WHERE id = %s",
        (report_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Document report not found")

    data = _parse_results_json(row.get("results_json"))
    summary = dict(data.get("summary") or {})
    old_results = list(data.get("results") or [])
    base_score = int(summary.get("skor_kepatuhan") or 0)
    kb_stale = bool(row.get("kb_stale"))

    html_src = (content_html or "").strip()
    if not html_src:
        draft_row = pg_service.query_one(
            "SELECT content_html FROM document_editor_drafts WHERE report_id = %s",
            (report_id,),
        )
        html_src = (draft_row or {}).get("content_html") or ""

    clauses_to_check: list[dict] = []
    if html_src:
        plain = html_to_plain_for_recheck(html_src)
        if len(plain.strip()) >= 40:
            clauses_to_check = extract_pasal_items(plain)

    if not clauses_to_check and old_results:
        clauses_to_check = [
            {
                "pasal": r.get("pasal") or f"Klausul {idx + 1}",
                "isi": r.get("isi_pasal") or "",
            }
            for idx, r in enumerate(old_results)
            if (r.get("isi_pasal") or "").strip()
        ]

    if not clauses_to_check:
        raise HTTPException(
            status_code=400,
            detail="No contract clauses available to re-check.",
        )

    doc_type = summary.get("jenis_dokumen", "Kontrak")
    pihak_1 = summary.get("pihak_pertama", "Pihak Pertama")
    pihak_2 = summary.get("pihak_kedua", "Pihak Kedua")
    sektor = summary.get("sektor_bisnis", "umum")
    pokok = summary.get("pokok_perjanjian", "")
    excluded = get_user_exclusions(user_id)

    old_by_norm: dict[str, dict] = {}
    for r in old_results:
        norm = _norm_ws(r.get("isi_pasal") or "").lower()
        if norm:
            old_by_norm[norm] = r

    new_results: list[Optional[dict]] = [None] * len(clauses_to_check)
    to_eval: list[tuple[int, dict]] = []

    for idx, clause in enumerate(clauses_to_check):
        norm = _norm_ws(clause.get("isi") or "").lower()
        cached = old_by_norm.get(norm)
        # Re-evaluate if KB is stale, clause is new/edited, or previous status was risky/fatal
        if (
            not kb_stale
            and cached is not None
            and (cached.get("status") or "").upper() == "SESUAI"
        ):
            new_results[idx] = cached
        else:
            to_eval.append((idx, clause))

    if to_eval:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futs = {
                pool.submit(
                    process_single_pasal,
                    clause,
                    doc_type,
                    pihak_1,
                    pihak_2,
                    sektor,
                    pokok,
                    COSINE_THRESHOLD,
                    user_id,
                    excluded,
                    report_id,
                ): idx
                for idx, clause in to_eval
            }
            for fut in concurrent.futures.as_completed(futs):
                idx = futs[fut]
                new_results[idx] = fut.result()

    final_results = [r for r in new_results if r is not None]
    try:
        from services import favorability
        favorability.sweep(final_results, doc_type, summary.get("pihak_kita") or pihak_1, summary.get("peran_kita"))
    except Exception as sweep_err:
        print(f"[KBSync] Favorability sweep skipped: {sweep_err}")
    fatal_count = sum(1 for r in final_results if r.get("status") == "FATAL")
    beresiko_count = sum(1 for r in final_results if r.get("status") == "BERESIKO")
    sesuai_count = sum(1 for r in final_results if r.get("status") == "SESUAI")
    tidak_diatur_count = sum(1 for r in final_results if r.get("status") == "TIDAK DIATUR")

    regulated_count = len(final_results) - tidak_diatur_count
    new_score = round((sesuai_count / max(regulated_count, 1)) * 100) if regulated_count > 0 else 100

    old_risky_count = sum(1 for r in old_results if r.get("status") in ("BERESIKO", "FATAL"))
    new_risky_count = beresiko_count + fatal_count
    resolved_count = max(0, old_risky_count - new_risky_count)

    summary.update({
        "total_klausul": len(final_results),
        "sesuai": sesuai_count,
        "beresiko": beresiko_count,
        "fatal": fatal_count,
        "tidak_diatur": tidak_diatur_count,
        "skor_kepatuhan": new_score,
    })
    data["summary"] = summary
    data["results"] = final_results

    pg_service.execute(
        "UPDATE compliance_history "
        "SET results_json = %s::jsonb, kb_stale = FALSE, kb_stale_reason = NULL, kb_synced_at = NOW() "
        "WHERE id = %s",
        (json.dumps(data), report_id),
    )

    return {
        "previous_score": base_score,
        "updated_score": new_score,
        "resolved_risks": resolved_count,
        "total_risks": new_risky_count,
        "summary": summary,
        "pasalResults": final_results,
        "kb_stale": False,
    }
