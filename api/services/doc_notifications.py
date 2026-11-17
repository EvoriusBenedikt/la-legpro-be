"""Collaborative document edit & comment notification service."""

from __future__ import annotations

import uuid
from typing import Iterable, Optional

from services import pg_service

_TABLES_ENSURED = False

_EVENT_PRIORITY = {
    "comment_reply": 4,
    "comment_resolved": 3,
    "comment_added": 2,
    "document_edited": 1,
}


def ensure_notification_tables() -> None:
    """Idempotently ensure notification tables exist even if migration 025 hasn't run."""
    global _TABLES_ENSURED
    if _TABLES_ENSURED:
        return
    try:
        pg_service.execute(
            """
            ALTER TABLE compliance_history
                ADD COLUMN IF NOT EXISTS last_edited_by TEXT,
                ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

            CREATE TABLE IF NOT EXISTS document_user_states (
                report_id      TEXT NOT NULL REFERENCES compliance_history(id) ON DELETE CASCADE,
                user_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                last_viewed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (report_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS document_notifications (
                id           TEXT PRIMARY KEY,
                recipient_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                actor_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                report_id    TEXT NOT NULL REFERENCES compliance_history(id) ON DELETE CASCADE,
                event_type   TEXT NOT NULL DEFAULT 'document_edited',
                reference_id TEXT,
                edit_count   INT NOT NULL DEFAULT 1,
                is_read      BOOLEAN NOT NULL DEFAULT false,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE INDEX IF NOT EXISTS idx_doc_notif_recipient_unread
                ON document_notifications (recipient_id, is_read, updated_at DESC);

            CREATE INDEX IF NOT EXISTS idx_doc_notif_dedup
                ON document_notifications (report_id, actor_id, recipient_id, event_type, is_read, updated_at DESC);

            CREATE TABLE IF NOT EXISTS notification_mutes (
                user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                target_type TEXT NOT NULL CHECK (target_type IN ('notebook', 'document')),
                target_id   TEXT NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id, target_type, target_id)
            );

            CREATE INDEX IF NOT EXISTS idx_notif_mutes_user
                ON notification_mutes (user_id, target_type);
            """
        )
        _TABLES_ENSURED = True
    except Exception as e:
        print(f"[DocNotifications] Table initialization warning: {e}")


def notify_collaborators(
    report_id: str,
    actor_id: str,
    event_type: str = "document_edited",
    target_user_ids: Optional[Iterable[str]] = None,
    reference_id: Optional[str] = None,
) -> None:
    """Record a grouped edit/comment notification for eligible collaborators.

    Non-blocking: errors are logged and swallowed so document mutations never fail.
    - When ``target_user_ids`` is None, broadcasts to workspace owner + all
      ``workspace_collaborators`` for the document's notebook (excluding ``actor_id``).
    - When ``target_user_ids`` is provided, notifies only those users (still
      verifying they have workspace access and excluding ``actor_id``).
    """
    try:
        ensure_notification_tables()
        doc = pg_service.query_one(
            "SELECT id, user_id, notebook_id FROM compliance_history WHERE id = %s",
            (report_id,),
        )
        if not doc:
            return

        notebook_id = doc.get("notebook_id") or doc["id"]

        pg_service.execute(
            "UPDATE compliance_history SET last_edited_by = %s, updated_at = now() WHERE id = %s",
            (actor_id, report_id),
        )

        pg_service.execute(
            """
            INSERT INTO document_user_states (report_id, user_id, last_viewed_at)
            VALUES (%s, %s, now())
            ON CONFLICT (report_id, user_id) DO UPDATE SET last_viewed_at = now()
            """,
            (report_id, actor_id),
        )

        collab_rows = pg_service.query(
            "SELECT user_id FROM workspace_collaborators WHERE notebook_id = %s",
            (notebook_id,),
        )
        accessible_users = {r["user_id"] for r in (collab_rows or []) if r.get("user_id")}
        if doc.get("user_id"):
            accessible_users.add(doc["user_id"])

        if target_user_ids is not None:
            recipients = {uid for uid in target_user_ids if uid and uid in accessible_users and uid != actor_id}
        else:
            recipients = {uid for uid in accessible_users if uid != actor_id}

        if not recipients:
            return

        mute_rows = pg_service.query(
            """
            SELECT user_id FROM notification_mutes
            WHERE user_id = ANY(%s)
              AND (
                    (target_type = 'document' AND target_id = %s)
                 OR (target_type = 'notebook' AND target_id = %s)
              )
            """,
            (list(recipients), report_id, notebook_id),
        )
        muted_users = {r["user_id"] for r in (mute_rows or [])}
        recipients -= muted_users

        for recipient_id in recipients:
            existing = pg_service.query_one(
                """
                SELECT id FROM document_notifications
                WHERE report_id = %s
                  AND actor_id = %s
                  AND recipient_id = %s
                  AND event_type = %s
                  AND is_read = false
                  AND updated_at >= now() - interval '15 minutes'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (report_id, actor_id, recipient_id, event_type),
            )
            if existing:
                pg_service.execute(
                    """
                    UPDATE document_notifications
                    SET edit_count = edit_count + 1,
                        reference_id = COALESCE(%s, reference_id),
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (reference_id, existing["id"]),
                )
            else:
                pg_service.execute(
                    """
                    INSERT INTO document_notifications
                        (id, recipient_id, actor_id, report_id, event_type, reference_id, edit_count, is_read)
                    VALUES (%s, %s, %s, %s, %s, %s, 1, false)
                    """,
                    (
                        f"dnotif-{uuid.uuid4().hex[:12]}",
                        recipient_id,
                        actor_id,
                        report_id,
                        event_type,
                        reference_id,
                    ),
                )
    except Exception as e:
        print(f"[DocNotifications] notify_collaborators warning: {e}")


def mark_document_as_read(report_id: str, user_id: str) -> None:
    """Mark all unread notifications on ``report_id`` for ``user_id`` as read."""
    ensure_notification_tables()
    pg_service.execute(
        """
        INSERT INTO document_user_states (report_id, user_id, last_viewed_at)
        VALUES (%s, %s, now())
        ON CONFLICT (report_id, user_id) DO UPDATE SET last_viewed_at = now()
        """,
        (report_id, user_id),
    )
    pg_service.execute(
        """
        UPDATE document_notifications
        SET is_read = true
        WHERE report_id = %s AND recipient_id = %s AND is_read = false
        """,
        (report_id, user_id),
    )


def set_mute(user_id: str, target_type: str, target_id: str, muted: bool) -> None:
    """Mute or unmute notifications for a workspace ('notebook') or 'document'."""
    ensure_notification_tables()
    if target_type not in ("notebook", "document"):
        raise ValueError("Invalid target_type")
    if muted:
        pg_service.execute(
            """
            INSERT INTO notification_mutes (user_id, target_type, target_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, target_type, target_id) DO NOTHING
            """,
            (user_id, target_type, target_id),
        )
    else:
        pg_service.execute(
            "DELETE FROM notification_mutes WHERE user_id = %s AND target_type = %s AND target_id = %s",
            (user_id, target_type, target_id),
        )


def _aggregate_doc_rows(rows: list[dict]) -> dict[str, dict]:
    """Group raw unread notification rows by report_id."""
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["report_id"], []).append(r)

    result: dict[str, dict] = {}
    for report_id, items in grouped.items():
        # Sort by priority desc, then updated_at desc so targeted replies surface first
        items_sorted = sorted(
            items,
            key=lambda x: (
                _EVENT_PRIORITY.get(x.get("event_type") or "document_edited", 0),
                x.get("updated_at_iso") or "",
            ),
            reverse=True,
        )
        headline = items_sorted[0]
        latest_by_time = max(items, key=lambda x: x.get("updated_at_iso") or "")
        distinct_actors = {i["actor_id"] for i in items if i.get("actor_id")}
        unread_editor_count = len(distinct_actors)
        result[report_id] = {
            "has_unread_edits": True,
            "event_type": headline.get("event_type") or "document_edited",
            "reference_id": headline.get("reference_id"),
            "unread_editor_count": unread_editor_count,
            "other_editors_count": max(0, unread_editor_count - 1),
            "latest_editor": {
                "id": headline.get("actor_id"),
                "name": headline.get("actor_name") or "Collaborator",
            },
            "latest_editor_name": headline.get("actor_name") or "Collaborator",
            "last_edited_at": latest_by_time.get("updated_at_iso"),
            "notebook_id": headline.get("notebook_id") or report_id,
        }
    return result


def get_unread_summary(user_id: str) -> dict:
    """Return per-document unread badges, per-notebook rollups, and active mutes."""
    ensure_notification_tables()
    rows = pg_service.query(
        """
        SELECT dn.report_id,
               dn.actor_id,
               dn.event_type,
               dn.reference_id,
               COALESCE(u.username, 'Collaborator') AS actor_name,
               COALESCE(ch.notebook_id, ch.id) AS notebook_id,
               to_char(dn.updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS updated_at_iso
        FROM document_notifications dn
        JOIN compliance_history ch ON ch.id = dn.report_id
        LEFT JOIN users u ON u.id = dn.actor_id
        WHERE dn.recipient_id = %s
          AND dn.is_read = false
          AND (
                ch.user_id = %s
             OR EXISTS (
                    SELECT 1 FROM workspace_collaborators wc
                    WHERE wc.notebook_id = COALESCE(ch.notebook_id, ch.id)
                      AND wc.user_id = %s
                )
          )
          AND NOT EXISTS (
                SELECT 1 FROM notification_mutes nm
                WHERE nm.user_id = %s
                  AND (
                        (nm.target_type = 'document' AND nm.target_id = dn.report_id)
                     OR (nm.target_type = 'notebook' AND nm.target_id = COALESCE(ch.notebook_id, ch.id))
                  )
          )
        ORDER BY dn.updated_at DESC
        """,
        (user_id, user_id, user_id, user_id),
    )

    unread_by_document = _aggregate_doc_rows(rows or [])

    unread_by_notebook: dict[str, dict] = {}
    for report_id, info in unread_by_document.items():
        nb_id = info["notebook_id"]
        entry = unread_by_notebook.get(nb_id)
        if not entry:
            unread_by_notebook[nb_id] = {
                "unread_doc_count": 1,
                "latest_editor_name": info["latest_editor_name"],
                "last_edited_at": info["last_edited_at"],
            }
        else:
            entry["unread_doc_count"] += 1
            if (info["last_edited_at"] or "") > (entry["last_edited_at"] or ""):
                entry["latest_editor_name"] = info["latest_editor_name"]
                entry["last_edited_at"] = info["last_edited_at"]

    mute_rows = pg_service.query(
        "SELECT target_type, target_id FROM notification_mutes WHERE user_id = %s",
        (user_id,),
    )
    muted_notebooks = [r["target_id"] for r in (mute_rows or []) if r["target_type"] == "notebook"]
    muted_documents = [r["target_id"] for r in (mute_rows or []) if r["target_type"] == "document"]

    return {
        "unread_by_document": unread_by_document,
        "unread_by_notebook": unread_by_notebook,
        "mutes": {
            "notebooks": muted_notebooks,
            "documents": muted_documents,
        },
    }


def get_notebook_docs_notification_map(user_id: str, report_ids: list[str], notebook_id: str) -> tuple[dict[str, dict], set[str], bool]:
    """Return (notification_state_by_doc, muted_doc_ids, is_notebook_muted) for a notebook."""
    ensure_notification_tables()
    if not report_ids:
        mute_nb = pg_service.query_one(
            "SELECT 1 FROM notification_mutes WHERE user_id = %s AND target_type = 'notebook' AND target_id = %s",
            (user_id, notebook_id),
        )
        return {}, set(), bool(mute_nb)

    mute_rows = pg_service.query(
        """
        SELECT target_type, target_id FROM notification_mutes
        WHERE user_id = %s
          AND (
                (target_type = 'notebook' AND target_id = %s)
             OR (target_type = 'document' AND target_id = ANY(%s))
          )
        """,
        (user_id, notebook_id, report_ids),
    )
    is_nb_muted = any(r["target_type"] == "notebook" for r in (mute_rows or []))
    muted_docs = {r["target_id"] for r in (mute_rows or []) if r["target_type"] == "document"}

    if is_nb_muted:
        return {}, muted_docs, True

    rows = pg_service.query(
        """
        SELECT dn.report_id,
               dn.actor_id,
               dn.event_type,
               dn.reference_id,
               COALESCE(u.username, 'Collaborator') AS actor_name,
               %s AS notebook_id,
               to_char(dn.updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS updated_at_iso
        FROM document_notifications dn
        LEFT JOIN users u ON u.id = dn.actor_id
        WHERE dn.recipient_id = %s
          AND dn.is_read = false
          AND dn.report_id = ANY(%s)
          AND NOT (dn.report_id = ANY(%s))
        ORDER BY dn.updated_at DESC
        """,
        (notebook_id, user_id, report_ids, list(muted_docs) if muted_docs else [""]),
    )
    return _aggregate_doc_rows(rows or []), muted_docs, False
