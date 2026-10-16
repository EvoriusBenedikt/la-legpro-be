-- 010: Last-login tracking + email verification status (2026-10-13).
-- Adds two columns to the users table:
--   last_login:      updated by auth.py /login on every successful sign-in.
--   email_verified:  defaults FALSE; set TRUE by a future verification flow
--                    (or manually via admin panel). The admin dashboard reads
--                    both fields for the user management table and detail drawer.
-- Additive and idempotent — no existing data is dropped or rewritten.
-- Fresh installs: applied automatically (postgres mounts ./migrations at
--   /docker-entrypoint-initdb.d and runs files in lexicographic order).
-- Existing installs:
--   psql "$DATABASE_URL" -f migrations/010_users_last_login_email_verified.sql

ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login     TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE;
