-- 018: Seed accounts for every role (2026-11-02).
-- Ensures that on clean/purged databases or fresh volumes, every role in the
-- system has an active account with known credentials.
-- Password for all seeded accounts: DemoAcc!2026
--
-- Roles:
--   Level 1: pengguna (User)
--   Level 2: manajer (Manager)
--   Level 3: direktur (Director)
--   Level 4: admin (Admin)
--   Level 5: sekretaris perusahaan (Corporate Secretary)
--   Level 6: insinyur ti (IT Engineer)
--   Level 7: dewa (Developer)
-- Plus legacy demo accounts: demoacc, demoaccc

INSERT INTO users (id, username, password_hash, email, role, is_active, email_verified, token_version)
VALUES
    ('u-pengguna-001',                      'pengguna',   '$2b$12$N89NTUZLyK5Q8vSb0ePwxOBliZBf.tf7DLGM5zXAIzkOh41hYs8vu', 'pengguna@lintasarta.co.id',   'pengguna',              TRUE, TRUE, 0),
    ('u-manajer-001',                       'manajer',    '$2b$12$N89NTUZLyK5Q8vSb0ePwxOBliZBf.tf7DLGM5zXAIzkOh41hYs8vu', 'manajer@lintasarta.co.id',    'manajer',               TRUE, TRUE, 0),
    ('u-direktur-001',                      'direktur',   '$2b$12$N89NTUZLyK5Q8vSb0ePwxOBliZBf.tf7DLGM5zXAIzkOh41hYs8vu', 'direktur@lintasarta.co.id',   'direktur',              TRUE, TRUE, 0),
    ('670bfd75-6296-4f59-b66d-24788f92edb4', 'admin',      '$2b$12$N89NTUZLyK5Q8vSb0ePwxOBliZBf.tf7DLGM5zXAIzkOh41hYs8vu', 'admin@lintasarta.co.id',      'admin',                 TRUE, TRUE, 0),
    ('u-sekretaris-001',                    'sekretaris', '$2b$12$N89NTUZLyK5Q8vSb0ePwxOBliZBf.tf7DLGM5zXAIzkOh41hYs8vu', 'sekretaris@lintasarta.co.id', 'sekretaris perusahaan', TRUE, TRUE, 0),
    ('u-insinyur-001',                      'insinyur',   '$2b$12$N89NTUZLyK5Q8vSb0ePwxOBliZBf.tf7DLGM5zXAIzkOh41hYs8vu', 'insinyur@lintasarta.co.id',   'insinyur ti',           TRUE, TRUE, 0),
    ('u-dewa-001',                          'dewa',       '$2b$12$N89NTUZLyK5Q8vSb0ePwxOBliZBf.tf7DLGM5zXAIzkOh41hYs8vu', 'dewa@lintasarta.co.id',       'dewa',                  TRUE, TRUE, 0),
    ('9812b510-b810-4b7f-a6e0-a8ae651357c5', 'demoacc',    '$2b$12$N89NTUZLyK5Q8vSb0ePwxOBliZBf.tf7DLGM5zXAIzkOh41hYs8vu', 'demoacc@lintasarta.co.id',    'sekretaris perusahaan', TRUE, TRUE, 0),
    ('1264f516-88be-4c79-b0bc-d8262199e218', 'demoaccc',   '$2b$12$N89NTUZLyK5Q8vSb0ePwxOBliZBf.tf7DLGM5zXAIzkOh41hYs8vu', 'demoaccc@lintasarta.co.id',   'sekretaris perusahaan', TRUE, TRUE, 0),
    ('98a4bb48-439f-465b-a030-454722d66659', 'ben',        '$2b$12$N89NTUZLyK5Q8vSb0ePwxOBliZBf.tf7DLGM5zXAIzkOh41hYs8vu', 'ben@lintasarta.co.id',        'pengguna',              TRUE, TRUE, 0)
ON CONFLICT (username) DO UPDATE SET
    password_hash  = EXCLUDED.password_hash,
    role           = EXCLUDED.role,
    is_active      = TRUE,
    email_verified = TRUE;

