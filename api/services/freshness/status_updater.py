"""Shared database helpers for applying regulation status transitions and relations."""
from __future__ import annotations

import uuid
from typing import Optional

from services.scraper.canonicalizer import normalize_legal_status

ACTIVE_LEGAL_STATUSES = ("berlaku", "diubah", "dicabut_sebagian")

_RELATION_TO_STATUS = {
    "REPEALED_BY": "dicabut",
    "PARTIALLY_REPEALED_BY": "dicabut_sebagian",
    "AMENDED_BY": "diubah",
}


def apply_status_transition(
    conn,
    regulation_id: int,
    new_status: str,
    trigger_source: str,
    *,
    related_canonical_key: Optional[str] = None,
    note: Optional[str] = None,
    verification_result: Optional[str] = None,
) -> bool:
    """Update a regulation's legal_status, sync chunks.is_active_law, and record audit history.

    Returns True if legal_status actually changed, False otherwise.
    """
    norm_new = normalize_legal_status(new_status)
    if norm_new == "unknown":
        norm_new = "berlaku"

    row = conn.execute(
        "SELECT id, legal_status, status_note FROM regulations WHERE id = %s",
        (regulation_id,),
    ).fetchone()
    if not row:
        return False

    prev_status = (row.get("legal_status") or "berlaku").strip().lower()
    changed = prev_status != norm_new
    effective_result = verification_result or ("status_changed" if changed else "verified_ok")
    is_active = norm_new in ACTIVE_LEGAL_STATUSES

    conn.execute(
        "UPDATE regulations "
        "SET legal_status = %s, "
        "    status_note = COALESCE(%s, status_note), "
        "    last_verified_at = NOW(), "
        "    last_verification_result = %s "
        "WHERE id = %s",
        (norm_new, note, effective_result, regulation_id),
    )
    conn.execute(
        "UPDATE chunks SET is_active_law = %s WHERE doc_id = %s",
        (is_active, str(regulation_id)),
    )

    if changed:
        conn.execute(
            "INSERT INTO regulation_status_history "
            "(id, regulation_id, previous_status, new_status, trigger_source, related_canonical_key, note) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                uuid.uuid4().hex,
                regulation_id,
                prev_status,
                norm_new,
                trigger_source,
                related_canonical_key,
                note,
            ),
        )
    return changed


def record_regulation_relation(
    conn,
    source_canonical_key: str,
    target_canonical_key: str,
    relation_type: str,
    *,
    source_regulation_id: Optional[int] = None,
    target_regulation_id: Optional[int] = None,
    note: Optional[str] = None,
) -> None:
    """Idempotently record a directed amendment/repeal relation between two regulations."""
    if not source_canonical_key or not target_canonical_key or source_canonical_key == target_canonical_key:
        return
    conn.execute(
        "INSERT INTO regulation_relations "
        "(id, source_regulation_id, source_canonical_key, target_regulation_id, target_canonical_key, relation_type, note) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (source_canonical_key, target_canonical_key, relation_type) DO UPDATE "
        "SET source_regulation_id = COALESCE(EXCLUDED.source_regulation_id, regulation_relations.source_regulation_id), "
        "    target_regulation_id = COALESCE(EXCLUDED.target_regulation_id, regulation_relations.target_regulation_id), "
        "    note = COALESCE(EXCLUDED.note, regulation_relations.note)",
        (
            uuid.uuid4().hex,
            source_regulation_id,
            source_canonical_key,
            target_regulation_id,
            target_canonical_key,
            relation_type,
            note,
        ),
    )


def cascade_relation_to_target(
    conn,
    source_canonical_key: str,
    source_regulation_id: Optional[int],
    target_canonical_key: str,
    relation_type: str,
    trigger_source: str,
    note: Optional[str] = None,
) -> bool:
    """Apply an outgoing Mencabut/Mengubah relation onto an existing target regulation if present."""
    new_status = _RELATION_TO_STATUS.get(relation_type)
    if not new_status:
        return False

    target = conn.execute(
        "SELECT id, legal_status FROM regulations WHERE canonical_key = %s LIMIT 1",
        (target_canonical_key,),
    ).fetchone()
    target_id = target["id"] if target else None

    record_regulation_relation(
        conn,
        source_canonical_key=source_canonical_key,
        target_canonical_key=target_canonical_key,
        relation_type=relation_type,
        source_regulation_id=source_regulation_id,
        target_regulation_id=target_id,
        note=note,
    )

    if not target_id:
        return False

    # Never downgrade 'dicabut' back to 'diubah' if an older amendment is processed later
    if target.get("legal_status") == "dicabut" and new_status != "dicabut":
        return False

    return apply_status_transition(
        conn,
        regulation_id=target_id,
        new_status=new_status,
        trigger_source=trigger_source,
        related_canonical_key=source_canonical_key,
        note=note or f"{relation_type} {source_canonical_key}",
    )
