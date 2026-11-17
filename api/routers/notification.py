"""Notification router for SSE streaming.

Auth hardening (2026-10-14, plan D4): the stream used to be open to anyone who
knew (or guessed) a user id, and a second POST route let anyone inject
notifications into any user's feed. Now:

  * GET requires a valid token and ``user_id`` must be the caller's own id
    (403 otherwise; there is no admin override, notifications are personal).
  * At most MAX_STREAMS_PER_USER concurrent streams per user; the next one
    gets 429 so a client cannot pin unlimited connections.
  * The stream ends as soon as the client disconnects, releasing its slot.
  * The HTTP POST route is removed. Nothing outside that handler ever called
    ``services.notification.notify_user`` and the frontend has no consumer, so
    background jobs will call ``notify_user`` in-process when this is wired up.

Browser EventSource cannot send an Authorization header, so a future frontend
consumer must use a fetch-based reader (see JobStatusContext.tsx).
"""

import asyncio
from typing import Dict, Literal

import auth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from services import doc_notifications

router = APIRouter()


class MuteToggleRequest(BaseModel):
    target_type: Literal["notebook", "document"]
    target_id: str
    muted: bool


@router.get("/notifications/unread-documents")
def get_unread_documents(current_user: dict = Depends(auth.get_current_user)):
    """Return unread edit/comment notifications grouped by document and notebook."""
    return doc_notifications.get_unread_summary(current_user["id"])


@router.put("/notifications/mute")
def toggle_notification_mute(
    req: MuteToggleRequest,
    current_user: dict = Depends(auth.get_current_user),
):
    """Mute or unmute notifications for a workspace ('notebook') or 'document'."""
    doc_notifications.set_mute(current_user["id"], req.target_type, req.target_id, req.muted)
    return {"status": "ok", "target_type": req.target_type, "target_id": req.target_id, "muted": req.muted}

MAX_STREAMS_PER_USER = 3
KEEPALIVE_SECONDS = 15

# user_id -> number of open streams. Single backend process / single event
# loop, so plain dict mutation between awaits is race-free.
_open_streams: Dict[str, int] = {}


@router.get("/notifications/{user_id}")
async def stream_notifications(
    user_id: str,
    request: Request,
    current_user: dict = Depends(auth.get_current_user),
):
    """
    SSE endpoint that streams notifications for the authenticated user.
    The client must send the Bearer token (fetch with headers; EventSource
    cannot).
    """
    if str(current_user["id"]) != user_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    if _open_streams.get(user_id, 0) >= MAX_STREAMS_PER_USER:
        raise HTTPException(
            status_code=429,
            detail="Too many open notification streams",
            headers={"Retry-After": "30"},
        )
    _open_streams[user_id] = _open_streams.get(user_id, 0) + 1

    async def event_stream():
        # The slot is taken above and released in this generator's finally.
        # Starlette always starts iterating the body once the response is
        # sent, so the finally is guaranteed to run for a real request.
        try:
            # Keep the connection alive; in production we would yield actual
            # payloads when `notify_user` is called.
            while not await request.is_disconnected():
                yield "data: keep-alive\n\n"
                await asyncio.sleep(KEEPALIVE_SECONDS)
        finally:
            # Runs on disconnect, cancellation and generator close alike.
            remaining = _open_streams.get(user_id, 1) - 1
            if remaining > 0:
                _open_streams[user_id] = remaining
            else:
                _open_streams.pop(user_id, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
