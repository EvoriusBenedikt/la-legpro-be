-- 027: Account preferences & collaborator avatar color on users.
-- Additive and idempotent: safe to run more than once; never drops data.
-- Fresh installs: applied automatically by postgres docker-entrypoint-initdb.d.
-- Existing installs:
--   Get-Content migrations\027_account_preferences.sql | docker exec -i legpro-postgres psql -U legpro -d legpro

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS display_name TEXT,
    ADD COLUMN IF NOT EXISTS avatar_color_index INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS preferred_governing_lang VARCHAR(8) NOT NULL DEFAULT 'id';
