-- 009: Chat attachments — images + PDFs attached to Legal Opinion chat
-- messages (2026-10-12). Design: la-legpro-doc/plans/2026-10-12-chat-
-- attachments-design.md §4. Applied manually on the live volume (initdb.d
-- only runs on first init):
--   Get-Content migrations\009_chat_attachments.sql | docker exec -i legpro-postgres psql -U legpro -d legpro
--
-- chat_attachments: one row per uploaded file, keyed to the FE-side
-- conversation id. Lifecycle: images go 'ready' at insert; PDFs insert as
-- 'processing' and a background task flips them to 'ready' (storage_mode
-- whole|chunked decided by extracted length vs WHOLE_TEXT_BUDGET) or
-- 'failed'. Owner-only access enforced API-side; admins are refused (FR-24
-- parity). Files live on disk under data/chat_attachments/<id>/ — this table
-- never touches the corpus pipeline (chunks/data/pdfs untouched).
--
-- chat_attachment_chunks: page-text chunks + 384-dim MiniLM embeddings
-- (same space as corpus chunks, embed_service) for scoped top-k recall of
-- big/older attachments. Cascades on attachment delete.

CREATE TABLE IF NOT EXISTS chat_attachments (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    owner TEXT NOT NULL,                   -- users.id
    kind TEXT NOT NULL CHECK (kind IN ('image','pdf')),
    filename TEXT NOT NULL,                -- sanitized basename
    mime TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'processing'
        CHECK (status IN ('processing','ready','failed')),
    error TEXT,                            -- human-readable, Indonesian
    storage_mode TEXT CHECK (storage_mode IN ('whole','chunked')),
    extracted_text TEXT,                   -- whole mode only (<= WHOLE_TEXT_BUDGET)
    extracted_chars INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chat_attachments_conv ON chat_attachments (conversation_id);

CREATE TABLE IF NOT EXISTS chat_attachment_chunks (
    id SERIAL PRIMARY KEY,
    attachment_id TEXT NOT NULL REFERENCES chat_attachments(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    text TEXT NOT NULL,
    embedding vector(384)
);

CREATE INDEX IF NOT EXISTS idx_chat_attachment_chunks_att ON chat_attachment_chunks (attachment_id);
