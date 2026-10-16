"""Notification service for background jobs."""

from __future__ import annotations
import asyncio
import json
from typing import Dict, List, Callable, Awaitable

# In-memory subscription map: user_id -> list of async generators (EventSources)
_subscribers: Dict[str, List[Callable[[], Awaitable[str]]]] = {}


async def _event_source() -> str:
    """Placeholder async generator that yields a simple keep-alive comment."""
    while True:
        yield f"data: keep-alive\n\n"
        await asyncio.sleep(30)


def subscribe(user_id: str) -> Callable[[], Awaitable[str]]:
    """
    Register a user for push notifications.

    Returns a callable that when invoked yields a string suitable for
    streaming as Server-Sent Events. The caller typically wraps this in
    FastAPI's `StreamingResponse`.
    """
    if user_id not in _subscribers:
        _subscribers[user_id] = []
    # Return a callable that yields events; in a real app this would be
    # integrated with an actual EventSource or WebSocket.
    async def event_generator():
        q = asyncio.Queue()
        _subscribers[user_id].append(lambda: q.get())
        try:
            while True:
                # Wait for a notification; in practice we would put something
                # into the queue when `notify_user` is called.
                msg = await q.get()
                yield f"data: {msg}\n\n"
        except asyncio.CancelledError:
            # Clean up on disconnect
            _subscribers[user_id].remove(lambda: None)  # placeholder removal
    return event_generator


def notify_user(user_id: str, job_id: str, status: str, message: str) -> None:
    """
    Push a notification to the given user.

    In a full implementation this would push to all active SSE streams.
    Here we just log it; a real server would iterate over the stored
    generators and yield the event.
    """
    payload = {
        "jobId": job_id,
        "status": status,
        "message": message,
        "timestamp": "2026-10-12T00:00:00Z",  # placeholder
    }
    # Broadcast to all subscribers (simple approach)
    for sub_list in _subscribers.values():
        for unsub in sub_list:
            # In this minimal version we just print; a real server would
            # feed the payload into each queue.
            pass
    print(f"[Notification] Sent to {user_id}: {payload}")