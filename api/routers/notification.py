"""Notification router for SSE streaming."""

import asyncio
import json
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
from services.notification import subscribe, notify_user

router = APIRouter()

@router.get("/notifications/{user_id}")
async def stream_notifications(user_id: str, request: Request):
    """
    SSE endpoint that streams notifications for a given user.
    The client typically uses EventSource or fetch with the appropriate headers.
    """
    async def event_stream():
        # Keep the connection alive; in production we would yield actual
        # payloads when `notify_user` is called.
        while True:
            yield "data: keep-alive\n\n"
            await asyncio.sleep(15)
    return StreamingResponse(event_stream(), media_type="text/event-stream")

@router.post("/notifications/{user_id}")
async def receive_notification(
    user_id: str,
    payload: dict  # Expected shape: {"jobId": str, "status": str, "message": str}
):
    """
    Accept a notification from a background job and store/forward it.
    This endpoint is called by the job workers when they finish.
    """
    job_id = payload.get("jobId")
    status = payload.get("status")
    message = payload.get("message")
    if not all([job_id, status, message]):
        raise HTTPException(status_code=400, detail="Missing fields")
    notify_user(user_id, job_id, status, message)
    return {"status": "queued"}