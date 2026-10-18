-- 011: Auth hardening (2026-10-14): per-account lockout, unknown-username
-- lockout table, JWT revocation version, case-insensitive uniqueness.
--   users.failed_attempts / locked_until / last_failed_at: lockout state
--       (auth.py /login; escalating temporary locks, see policy in the plan).
--   users.token_version: bumped on password change/reset, force-logout,
--       deactivation and role change; JWT claim `tv` must match.
--   login_attempts: same counters keyed (username_lower, ip) so non-existent
--       usernames get the identical lockout and no existence oracle.
--   Unique indexes on lower(username) / lower(email): created ONLY when no
--       case-variant duplicates exist; otherwise a NOTICE is raised and the
--       index is skipped (this script never aborts on legacy data).
-- Additive and idempotent: safe to run more than once.
-- Fresh installs: applied automatically (postgres mounts ./migrations at
--   /docker-entrypoint-initdb.d and runs files in lexicographic order).
-- Existing installs (REQUIRED before deploying the new backend):
--   psql "$DATABASE_URL" -f migrations/011_auth_hardening.sql

ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_attempts INT NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until    TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_failed_at  TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version   INT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS login_attempts (
    id             BIGSERIAL PRIMARY KEY,
    username_lower TEXT        NOT NULL,
    ip             TEXT        NOT NULL,
    failed_count   INT         NOT NULL DEFAULT 0,
    locked_until   TIMESTAMPTZ,
    last_failed_at TIMESTAMPTZ,
    UNIQUE (username_lower, ip)
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_last_failed ON login_attempts (last_failed_at);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM users GROUP BY lower(username) HAVING count(*) > 1) THEN
        RAISE NOTICE '011: case-variant duplicate usernames exist; skipping users_username_lower_uidx';
    ELSE
        BEGIN
            CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower_uidx ON users (lower(username));
        EXCEPTION WHEN unique_violation THEN
            RAISE NOTICE '011: users_username_lower_uidx skipped (concurrent duplicate)';
        END;
    END IF;

    IF EXISTS (SELECT 1 FROM users WHERE email IS NOT NULL AND email <> ''
               GROUP BY lower(email) HAVING count(*) > 1) THEN
        RAISE NOTICE '011: duplicate emails exist; skipping users_email_lower_uidx';
    ELSE
        BEGIN
            CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_uidx
                ON users (lower(email)) WHERE email IS NOT NULL AND email <> '';
        EXCEPTION WHEN unique_violation THEN
            RAISE NOTICE '011: users_email_lower_uidx skipped (concurrent duplicate)';
        END;
    END IF;
END $$;
