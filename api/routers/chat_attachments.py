"""Chat-attachment endpoints (2026-10-12).

Design: la-legpro-doc/plans/2026-10-12-chat-attachments-design.md §4.
All routes are JWT-authed, owner-scoped, and refuse admins with the same
403 grammar as /api/chat (FR-24). ValueError codes from the service map to
413 (oversize) / 415 (magic-byte mismatch) / 422 (conversation limit); the
FE translates the detail codes into chip errors.
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse

import auth
from services import attachment_service

router = APIRouter(prefix="/api", tags=["chat_attachments"])

# Same refusal text as chat.py's FR-24 guard.
_ADMIN_DETAIL = "Admin sistem tidak memiliki kewenangan untuk mengakses konten dokumen."

_STATUS_MAP = {"oversize": 413, "type": 415, "limit": 422}
_NOT_FOUND_DETAIL = "Lampiran tidak ditemukan."


def _guard_admin(current_user: dict):
    if current_user.get("role", "pengguna").lower() == "admin":
        raise HTTPException(status_code=403, detail=_ADMIN_DETAIL)


@router.post("/chat-attachments")
async def upload_attachment(
    conversation_id: str = Form(...),
    file: UploadFile = File(...),
    current_user: dict = Depends(auth.get_current_user),
):
    _guard_admin(current_user)
    data = await file.read()
    try:
        return attachment_service.save_upload(
            conversation_id, str(current_user["id"]),
            file.filename or "", file.content_type or "", data)
    except ValueError as e:
        raise HTTPException(status_code=_STATUS_MAP.get(str(e), 400), detail=str(e))


@router.get("/chat-attachments")
async def list_attachments(
    conversation_id: str,
    current_user: dict = Depends(auth.get_current_user),
):
    _guard_admin(current_user)
    return attachment_service.list_for_conversation(conversation_id, str(current_user["id"]))


@router.get("/chat-attachments/{attachment_id}/content")
async def attachment_content(
    attachment_id: str,
    current_user: dict = Depends(auth.get_current_user),
):
    _guard_admin(current_user)
    got = attachment_service.get_content(attachment_id, str(current_user["id"]))
    if not got:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL)
    path, mime, filename = got
    # inline disposition: <img> thumbs render and PDFs open in a tab; the FE
    # fetches via blob (auth header) so no token ever rides in a URL.
    return FileResponse(path, media_type=mime, filename=filename,
                        content_disposition_type="inline")


@router.delete("/chat-attachments/{attachment_id}")
async def delete_attachment(
    attachment_id: str,
    current_user: dict = Depends(auth.get_current_user),
):
    _guard_admin(current_user)
    if not attachment_service.delete_one(attachment_id, str(current_user["id"])):
        raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL)
    return {"status": "deleted"}


@router.delete("/chat-attachments")
async def delete_conversation_attachments(
    conversation_id: str,
    current_user: dict = Depends(auth.get_current_user),
):
    _guard_admin(current_user)
    count = attachment_service.delete_for_conversation(conversation_id, str(current_user["id"]))
    return {"status": "deleted", "count": count}
