"""B2: per-user reference-base exclusion set (feature B2).

Three endpoints: the reference list the notebook checklist renders from, and a
GET/POST pair over the caller's exclusion set. Every query is scoped to
current_user["id"] — a user can only ever read or mutate their own set. The
shared GET /api/repository is left byte-for-byte unchanged; this router reuses
repository.get_repository for the public-reg ACL (klasifikasi + access_grants +
admin FR-24 block) so that ACL stays defined in exactly one place.
"""
import re
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import auth
from services import pg_service
from routers import repository

router = APIRouter(prefix="/api", tags=["rag_sources"])

# These shapes are produced by internal_docs.py (upload_id = str(uuid.uuid4())[:8],
# 8 lowercase hex) and regulations ids — keep in lockstep with that module and
# with chunk_source_ref / the /rag-sources/list split_part derivation.
_REG_RE = re.compile(r"^reg:[1-9][0-9]*$")
_INT_RE = re.compile(r"^internal:[0-9a-f]{8}$")   # internal_docs.py: str(uuid.uuid4())[:8]


def _valid_source_ref(ref: str) -> bool:
    return bool(_REG_RE.match(ref) or _INT_RE.match(ref))


class ExclusionUpdate(BaseModel):
    source_ref: str | None = None          # single toggle
    excluded: bool | None = None           # desired state for the single ref
    set_excluded: list[str] | None = None  # bulk: ADD these refs to the set
    set_included: list[str] | None = None  # bulk: REMOVE these refs from the set


@router.get("/rag-sources/list")   # router prefix="/api" -> /api/rag-sources/list
async def list_sources(current_user: dict = Depends(auth.get_current_user)):
    base = await repository.get_repository(current_user)   # same role ACL, id already str
    uid = str(current_user["id"])
    excluded = {r["source_ref"] for r in pg_service.query(
        "SELECT source_ref FROM rag_source_exclusions WHERE user_id = %s", (uid,))}

    docs = []
    for row in base["documents"]:
        ref = f"reg:{row['id']}"
        docs.append({**row, "source_ref": ref, "active": ref not in excluded})

    # Internal uploads: no regulations row, discovered by grouping the user's
    # private internal_* chunks. split_part(id,'_',2) == the upload_id; it MUST
    # map a chunk to the SAME upload_id as chunk_source_ref's split('_')[1]
    # (both derive from internal_docs.py's internal_<upload_id>_<i> id shape).
    internal = pg_service.query(
        "SELECT split_part(id, '_', 2) AS upload_id, min(judul) AS fname "
        "FROM chunks "
        "WHERE visibility = 'private' AND user_id = %s::text "
        "AND doc_id IS NULL AND id LIKE 'internal\\_%%' "
        "GROUP BY split_part(id, '_', 2)",
        (uid,))
    for row in internal:
        ref = f"internal:{row['upload_id']}"
        fname = row["fname"] or "Dokumen Internal"
        docs.append({
            "id": ref,                    # string, like every other row's id
            "source_ref": ref,
            "judul": fname,
            "nomor": fname,               # mirrors internal_docs.py (filename in nomor)
            "jenis": "Dokumen Internal",
            "sektor": "Internal",
            "status": "Internal",
            "klasifikasi": "Internal",    # synthetic; FE facets on jenis, not klasifikasi
            "filename": fname,
            "ai_confidence": None,
            "approval_mode": None,
            "active": ref not in excluded,
        })
    return {"documents": docs}


@router.get("/rag-sources/exclusions")   # -> /api/rag-sources/exclusions
async def get_exclusions(current_user: dict = Depends(auth.get_current_user)):
    rows = pg_service.query(
        "SELECT source_ref FROM rag_source_exclusions WHERE user_id = %s",
        (str(current_user["id"]),))
    return {"excluded": [r["source_ref"] for r in rows]}


@router.post("/rag-sources/exclusions")   # -> /api/rag-sources/exclusions
async def update_exclusions(body: ExclusionUpdate,
                            current_user: dict = Depends(auth.get_current_user)):
    uid = str(current_user["id"])
    to_add, to_remove = [], []
    if body.source_ref is not None:
        if body.excluded is None:
            raise HTTPException(422, "excluded is required with source_ref")
        (to_add if body.excluded else to_remove).append(body.source_ref)
    to_add += (body.set_excluded or [])
    to_remove += (body.set_included or [])
    if not to_add and not to_remove:
        raise HTTPException(422, "no change requested")
    for ref in to_add + to_remove:
        if not _valid_source_ref(ref):
            raise HTTPException(422, f"invalid source_ref: {ref}")
    # Single transaction so a partial failure rolls the whole update back
    # (the pool context manager commits on clean exit, rolls back on exception).
    with pg_service.get_conn() as conn, conn.cursor() as cur:
        if to_add:
            cur.executemany(
                "INSERT INTO rag_source_exclusions (user_id, source_ref) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                [(uid, r) for r in to_add])
        if to_remove:
            cur.executemany(
                "DELETE FROM rag_source_exclusions WHERE user_id = %s AND source_ref = %s",
                [(uid, r) for r in to_remove])
    return {"status": "ok"}
