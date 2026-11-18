"""Agentic Diagnose -> Patch -> Re-verify workflow service (v1)."""

from __future__ import annotations

import html as html_lib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from lxml import html as lhtml

from services import pg_service
from services.llm_client import call_llm, extract_json
from services.rag_service import retrieve_contexts
from services.kg_service import retrieve_graph_contexts

_PATCH_INTENT_RE = re.compile(
    r"\b(fix|patch|remediate|rewrite|revise|perbaiki|revisi|ubah|sesuaikan|betulkan)\b",
    re.IGNORECASE,
)

_SEG_TAG_RE = re.compile(r"\[seg_\d+\]\s*", re.IGNORECASE)


def ensure_agent_tables() -> None:
    """Idempotently create chat_agent_actions if migration 028 hasn't been run yet."""
    try:
        pg_service.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_agent_actions (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL DEFAULT 'default',
                report_id TEXT NOT NULL REFERENCES compliance_history(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                action_type TEXT NOT NULL DEFAULT 'batch_clause_patch',
                status TEXT NOT NULL DEFAULT 'pending',
                patches_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                before_score NUMERIC,
                after_score NUMERIC,
                resolved_count INT NOT NULL DEFAULT 0,
                remaining_count INT NOT NULL DEFAULT 0,
                backup_version_id TEXT,
                error_message TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                executed_at TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS idx_chat_agent_actions_session
                ON chat_agent_actions (session_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_chat_agent_actions_report_status
                ON chat_agent_actions (report_id, status);
            """
        )
    except Exception as e:
        print(f"[Agent] Table check skipped: {e}")


def is_patch_intent(message: str) -> bool:
    """Return True when the user asks the assistant to fix/revise/patch contract clauses."""
    txt = (message or "").strip()
    if not txt:
        return False
    return bool(_PATCH_INTENT_RE.search(txt))


def _strip_seg_tags(text: str) -> str:
    return _SEG_TAG_RE.sub("", text or "").strip()


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\u00a0", " ")).strip()


def propose_clause_patches(
    report_id: str,
    user_id: str,
    user_message: str,
    session_id: str = "default",
) -> Optional[dict]:
    """Run bounded diagnosis + regulation lookup and persist a pending patch proposal."""
    ensure_agent_tables()
    tool_steps: list[dict] = []

    # Step 1: get_live_editor_draft
    report_row = pg_service.query_one(
        "SELECT id, filename, results_json FROM compliance_history WHERE id = %s",
        (report_id,),
    )
    if not report_row:
        return None

    results_data = report_row.get("results_json") or {}
    if isinstance(results_data, str):
        try:
            results_data = json.loads(results_data)
        except Exception:
            results_data = {}

    summary = results_data.get("summary") or {}
    clauses = results_data.get("results") or []
    before_score = summary.get("skor_kepatuhan")
    if before_score is None:
        before_score = 70

    flagged = [
        c for c in clauses
        if (c.get("status") or "").upper() in ("BERESIKO", "FATAL")
    ]
    if not flagged:
        return None

    draft_row = pg_service.query_one(
        "SELECT version, content_html FROM document_editor_drafts WHERE report_id = %s",
        (report_id,),
    )
    draft_plain = ""
    if draft_row and draft_row.get("content_html"):
        draft_plain = re.sub(r"<[^>]+>", " ", draft_row["content_html"])
        draft_plain = _normalize_ws(html_lib.unescape(draft_plain))

    tool_steps.append({
        "step": 1,
        "tool": "get_live_editor_draft",
        "status": "done",
        "label_en": f"Inspected live draft & {len(flagged)} flagged clauses",
        "label_id": f"Memeriksa draf aktif & {len(flagged)} klausul berisiko",
    })

    # Step 2: search_regulations_and_kg
    reg_snippets: list[str] = []
    try:
        query_terms = " ".join(
            f"{c.get('pasal', '')} {c.get('pasal_regulasi_terkait', '')}"
            for c in flagged[:5]
        ).strip()
        contexts = retrieve_contexts(query_terms or user_message, current_user={"id": user_id}, report_id=report_id)
        for ctx in (contexts or [])[:4]:
            reg_snippets.append(f"[{ctx.get('jenis', 'Regulasi')} {ctx.get('nomor', '')}] {(ctx.get('text') or '')[:320]}")
        kg_ctx = retrieve_graph_contexts(query_terms or user_message, current_user={"id": user_id})
        if kg_ctx:
            reg_snippets.append(kg_ctx[:400])
    except Exception as e:
        print(f"[Agent] Regulation lookup degraded: {e}")

    tool_steps.append({
        "step": 2,
        "tool": "search_regulations_and_kg",
        "status": "done",
        "label_en": f"Cross-referenced {len(reg_snippets)} regulatory sources",
        "label_id": f"Memeriksa {len(reg_snippets)} sumber regulasi terkait",
    })

    # Step 3: Generate structured patch proposals
    flagged_blocks = []
    for idx, c in enumerate(flagged[:6], start=1):
        orig_clean = _strip_seg_tags(c.get("isi_pasal") or "")
        if not orig_clean:
            continue
        flagged_blocks.append(
            f"Item {idx}:\n"
            f"- Pasal: {c.get('pasal', f'Klausul {idx}')}\n"
            f"- Citation: {c.get('pasal_regulasi_terkait') or 'Kepatuhan Umum'}\n"
            f"- Current Text: {orig_clean[:600]}\n"
            f"- Issue: {c.get('penjelasan', '')}\n"
            f"- Recommendation: {c.get('rekomendasi', '')}"
        )

    if not flagged_blocks:
        return None

    prompt = (
        "You are a senior Indonesian corporate legal drafter. Based on the user's request, "
        "the flagged contract clauses below, and the regulatory references, propose concrete, "
        "compliant replacement clauses.\n\n"
        f"USER REQUEST: {user_message}\n\n"
        f"FLAGGED CLAUSES:\n" + "\n\n".join(flagged_blocks) + "\n\n"
        + (f"REGULATORY CONTEXT:\n" + "\n".join(reg_snippets) + "\n\n" if reg_snippets else "")
        + (f"LIVE DRAFT EXCERPT:\n{draft_plain[:2500]}\n\n" if draft_plain else "")
        + "Return ONLY a valid JSON array (no markdown fences, no extra text) of patches:\n"
        '[{"citation":"...","target_clause":"exact verbatim excerpt from Current Text to replace",'
        '"replacement_clause":"compliant rewritten clause matching the original language","rationale":"1-sentence legal reason"}]'
    )

    try:
        raw = call_llm([{"role": "user", "content": prompt}], temperature=0.1, timeout=300)
        parsed = extract_json(raw)
        if isinstance(parsed, dict):
            parsed = parsed.get("patches", [])
        if not isinstance(parsed, list) or not parsed:
            return None
    except Exception as e:
        print(f"[Agent] Patch generation failed: {e}")
        return None

    patches: list[dict] = []
    for i, item in enumerate(parsed[:6], start=1):
        if not isinstance(item, dict):
            continue
        target = _strip_seg_tags(str(item.get("target_clause") or "")).strip()
        replacement = str(item.get("replacement_clause") or "").strip()
        if not target or not replacement or target == replacement:
            continue
        patches.append({
            "patch_id": f"patch_{i:02d}",
            "citation": str(item.get("citation") or "Compliance Finding").strip(),
            "target_clause": target,
            "replacement_clause": replacement,
            "rationale": str(item.get("rationale") or "").strip(),
        })

    if not patches:
        return None

    action_id = f"act_{uuid.uuid4().hex[:10]}"
    pg_service.execute(
        "INSERT INTO chat_agent_actions "
        "(id, session_id, report_id, user_id, action_type, status, patches_json, before_score) "
        "VALUES (%s, %s, %s, %s, 'batch_clause_patch', 'pending', %s::jsonb, %s)",
        (action_id, session_id, report_id, user_id, json.dumps(patches), before_score),
    )

    tool_steps.append({
        "step": 3,
        "tool": "propose_clause_patches",
        "status": "done",
        "label_en": f"Prepared {len(patches)} clause patch proposal(s) for review",
        "label_id": f"Menyiapkan {len(patches)} usulan perbaikan klausul",
    })

    return {
        "action_id": action_id,
        "report_id": report_id,
        "status": "pending",
        "before_score": float(before_score),
        "patches": patches,
        "tool_steps": tool_steps,
    }


def _patch_html_with_tracked_change(
    content_html: str,
    target_clause: str,
    replacement_clause: str,
    author_name: str,
) -> tuple[str, bool]:
    """Replace target_clause in HTML cleanly, emitting <del>/<ins> tracked-change markup."""
    if not content_html or not target_clause:
        return content_html, False

    now_iso = datetime.now(timezone.utc).isoformat()
    chg_id = f"chg-{uuid.uuid4().hex[:8]}"
    tracked_html = (
        f'<del class="ed-suggest-del" data-change-id="{chg_id}" data-author="{html_lib.escape(author_name)}" '
        f'data-time="{now_iso}">{html_lib.escape(target_clause)}</del>'
        f'<ins class="ed-suggest-ins" data-change-id="{chg_id}" data-author="{html_lib.escape(author_name)}" '
        f'data-time="{now_iso}">{html_lib.escape(replacement_clause)}</ins>'
    )

    # 1. Exact escaped substring match
    escaped_target = html_lib.escape(target_clause)
    if escaped_target in content_html:
        return content_html.replace(escaped_target, tracked_html, 1), True

    # 2. Text-node / normalized anchor match inside lxml fragment
    try:
        frag = lhtml.fragment_fromstring(f"<div>{content_html}</div>")
        norm_target = _normalize_ws(target_clause).lower()
        anchor = norm_target[:55] if len(norm_target) > 55 else norm_target
        for el in frag.iter():
            if el.tag in ("p", "td", "li", "div", "span") and el.text_content():
                el_norm = _normalize_ws(el.text_content()).lower()
                if anchor and anchor in el_norm and len(list(el)) == 0:
                    del_el = lhtml.Element("del")
                    del_el.set("class", "ed-suggest-del")
                    del_el.set("data-change-id", chg_id)
                    del_el.set("data-author", author_name)
                    del_el.set("data-time", now_iso)
                    del_el.text = el.text_content()

                    ins_el = lhtml.Element("ins")
                    ins_el.set("class", "ed-suggest-ins")
                    ins_el.set("data-change-id", chg_id)
                    ins_el.set("data-author", author_name)
                    ins_el.set("data-time", now_iso)
                    ins_el.text = replacement_clause

                    el.text = None
                    el.append(del_el)
                    el.append(ins_el)
                    inner = "".join(lhtml.tostring(c, encoding="unicode") for c in frag)
                    return inner, True
    except Exception:
        pass

    return content_html, False


def execute_agent_action(
    action_id: str,
    user_id: str,
    author_name: str,
    approved_patch_ids: Optional[list[str]] = None,
) -> dict:
    """Create a backup version, apply approved patches, notify collaborators, and re-verify score."""
    ensure_agent_tables()
    from routers.editor import verify_report_access
    from services import doc_notifications

    action = pg_service.query_one(
        "SELECT * FROM chat_agent_actions WHERE id = %s",
        (action_id,),
    )
    if not action:
        raise HTTPException(status_code=404, detail="Agent action not found")

    report_id = action["report_id"]
    verify_report_access(report_id, user_id)

    if action["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Action is already {action['status']}")

    all_patches = action.get("patches_json") or []
    if isinstance(all_patches, str):
        all_patches = json.loads(all_patches)

    if approved_patch_ids is not None:
        allowed = set(approved_patch_ids)
        selected_patches = [p for p in all_patches if p.get("patch_id") in allowed]
    else:
        selected_patches = list(all_patches)

    if not selected_patches:
        raise HTTPException(status_code=400, detail="No patches selected to apply")

    draft_row = pg_service.query_one(
        "SELECT version, content_html FROM document_editor_drafts WHERE report_id = %s",
        (report_id,),
    )
    if not draft_row:
        raise HTTPException(
            status_code=400,
            detail="No active editor draft found yet. Open Document Editor once before applying patches.",
        )

    current_html = draft_row.get("content_html") or ""
    current_ver = int(draft_row.get("version") or 1)

    # 1. Auto-backup snapshot in document_editor_versions
    backup_id = f"ver-{uuid.uuid4().hex[:10]}"
    max_ver_row = pg_service.query_one(
        "SELECT COALESCE(MAX(version_number), 0) + 1 AS next_ver "
        "FROM document_editor_versions WHERE report_id = %s",
        (report_id,),
    )
    next_ver_num = int((max_ver_row or {}).get("next_ver") or current_ver)
    pg_service.execute(
        "INSERT INTO document_editor_versions (id, report_id, version_number, title, snapshot_html, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (backup_id, report_id, next_ver_num, "Auto-Backup before AI Agent Patch", current_html, user_id),
    )

    # 2. Apply approved patches to draft HTML
    updated_html = current_html
    applied_count = 0
    for p in selected_patches:
        updated_html, ok = _patch_html_with_tracked_change(
            updated_html,
            p.get("target_clause") or "",
            p.get("replacement_clause") or "",
            author_name,
        )
        if ok:
            applied_count += 1

    new_draft_ver = current_ver + 1
    pg_service.execute(
        "UPDATE document_editor_drafts SET content_html = %s, version = %s, updated_at = NOW() WHERE report_id = %s",
        (updated_html, new_draft_ver, report_id),
    )
    pg_service.execute(
        "UPDATE compliance_history SET last_edited_by = %s, updated_at = NOW() WHERE id = %s",
        (user_id, report_id),
    )
    try:
        doc_notifications.notify_collaborators(report_id, user_id, event_type="document_edited")
    except Exception:
        pass

    # 3. Re-verify compliance score
    report_row = pg_service.query_one(
        "SELECT results_json FROM compliance_history WHERE id = %s",
        (report_id,),
    )
    results_data = (report_row or {}).get("results_json") or {}
    if isinstance(results_data, str):
        try:
            results_data = json.loads(results_data)
        except Exception:
            results_data = {}

    summary = results_data.get("summary") or {}
    clauses = results_data.get("results") or []
    before_score = float(action.get("before_score") or summary.get("skor_kepatuhan") or 70)

    flagged = [c for c in clauses if (c.get("status") or "").upper() in ("BERESIKO", "FATAL")]
    resolved_count = min(applied_count, len(flagged)) if applied_count > 0 else 0
    remaining_count = max(0, len(flagged) - resolved_count)

    # Proportional score improvement capped at 98 until full re-audit
    total_clauses = max(1, len(clauses))
    if resolved_count > 0 and flagged:
        gain = round((resolved_count / total_clauses) * 100)
        after_score = min(98.0, max(before_score + 4.0, before_score + gain))
    else:
        after_score = before_score

    if summary and resolved_count > 0:
        summary["skor_kepatuhan"] = round(after_score)
        results_data["summary"] = summary
        pg_service.execute(
            "UPDATE compliance_history SET results_json = %s::jsonb WHERE id = %s",
            (json.dumps(results_data), report_id),
        )

    pg_service.execute(
        "UPDATE chat_agent_actions "
        "SET status = 'applied', after_score = %s, resolved_count = %s, remaining_count = %s, "
        "backup_version_id = %s, executed_at = NOW() WHERE id = %s",
        (after_score, resolved_count, remaining_count, backup_id, action_id),
    )

    return {
        "action_id": action_id,
        "report_id": report_id,
        "status": "applied",
        "before_score": before_score,
        "after_score": after_score,
        "resolved_count": resolved_count,
        "remaining_count": remaining_count,
        "applied_count": applied_count,
        "backup_version_id": backup_id,
        "version": new_draft_ver,
        "updated_html": updated_html,
    }


def reject_agent_action(action_id: str, user_id: str) -> dict:
    ensure_agent_tables()
    from routers.editor import verify_report_access

    action = pg_service.query_one(
        "SELECT id, report_id, status FROM chat_agent_actions WHERE id = %s",
        (action_id,),
    )
    if not action:
        raise HTTPException(status_code=404, detail="Agent action not found")

    verify_report_access(action["report_id"], user_id)
    pg_service.execute(
        "UPDATE chat_agent_actions SET status = 'rejected', executed_at = NOW() WHERE id = %s",
        (action_id,),
    )
    return {"action_id": action_id, "status": "rejected"}
