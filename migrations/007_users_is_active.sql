-- 007: User deactivation (2026-10-06, admin-dashboard critique P0).
-- users.is_active backs the admin user-management panel: exact-role admins
-- can deactivate/reactivate accounts strictly below their own role level.
-- Additive and idempotent — every existing row becomes active, no data is
-- rewritten or dropped.
-- Enforcement lives in api/auth.py: /login rejects inactive accounts (after
-- password verification, so probing cannot reveal disabled usernames), and
-- get_current_user re-reads role + is_active on EVERY authenticated request,
-- so deactivation and role changes take effect on the target's next API call
-- instead of at JWT expiry (24 h).
-- Fresh installs: applied automatically by the postgres container
-- (./migrations is mounted at /docker-entrypoint-initdb.d).
-- Existing installs: psql "$DATABASE_URL" -f migrations/007_users_is_active.sql

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
