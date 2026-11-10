-- 023: Workspace icon metadata for semantic Lucide icon representation.
-- Additive and idempotent.

ALTER TABLE notebooks ADD COLUMN IF NOT EXISTS icon VARCHAR(64) DEFAULT 'folder' NOT NULL;

-- Backfill existing workspaces based on canonical contract patterns
UPDATE notebooks SET icon = 'handshake' WHERE icon = 'folder' AND name ~* '(pks|perjanjian kerjas?ama|partnership)';
UPDATE notebooks SET icon = 'clipboard-check' WHERE icon = 'folder' AND name ~* '(bast|serah terima|handover)';
UPDATE notebooks SET icon = 'file-plus' WHERE icon = 'folder' AND name ~* '(addendum|amandemen|amendment)';
UPDATE notebooks SET icon = 'share-2' WHERE icon = 'folder' AND name ~* '(data sharing|dsa|berbagi data)';
UPDATE notebooks SET icon = 'shield-check' WHERE icon = 'folder' AND name ~* '(dpa|data processing|perlindungan data)';
UPDATE notebooks SET icon = 'lock' WHERE icon = 'folder' AND name ~* '(nda|non-disclosure|kerahasiaan|confidential)';
UPDATE notebooks SET icon = 'gauge' WHERE icon = 'folder' AND name ~* '(sla|service level|tingkat layanan)';
UPDATE notebooks SET icon = 'list-checks' WHERE icon = 'folder' AND name ~* '(sow|statement of work|ruang lingkup)';
UPDATE notebooks SET icon = 'file-signature' WHERE icon = 'folder' AND name ~* '(mou|nota kesepahaman|memorandum)';
UPDATE notebooks SET icon = 'shopping-cart' WHERE icon = 'folder' AND name ~* '(po|purchase order|pesanan pembelian|purchase agreement)';
UPDATE notebooks SET icon = 'file-text' WHERE icon = 'folder' AND name ~* '(spk|surat perintah kerja|work order)';
UPDATE notebooks SET icon = 'store' WHERE icon = 'folder' AND name ~* '(reseller|keagenan|distributor|mitra)';
