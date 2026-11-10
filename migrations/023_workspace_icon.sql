-- 023: Workspace icon metadata for semantic Lucide icon representation.
-- Additive and idempotent.

ALTER TABLE notebooks ADD COLUMN IF NOT EXISTS icon VARCHAR(64) DEFAULT 'folder' NOT NULL;

-- Backfill existing workspaces based on canonical contract patterns with word boundaries
UPDATE notebooks SET icon = 'handshake' WHERE icon = 'folder' AND name ~* '\y(pks|perjanjian kerjas?ama|partnership)\y';
UPDATE notebooks SET icon = 'clipboard-check' WHERE icon = 'folder' AND name ~* '\y(bast|serah terima|handover)\y';
UPDATE notebooks SET icon = 'file-plus' WHERE icon = 'folder' AND name ~* '\y(addendum|amandemen|amendment)\y';
UPDATE notebooks SET icon = 'share-2' WHERE icon = 'folder' AND name ~* '\y(data sharing|dsa|berbagi data)\y';
UPDATE notebooks SET icon = 'shield-check' WHERE icon = 'folder' AND name ~* '\y(dpa|data processing|perlindungan data)\y';
UPDATE notebooks SET icon = 'lock' WHERE icon = 'folder' AND name ~* '\y(nda|non-disclosure|kerahasiaan|confidential(ity)?)\y';
UPDATE notebooks SET icon = 'gauge' WHERE icon = 'folder' AND name ~* '\y(sla|service level|tingkat layanan)\y';
UPDATE notebooks SET icon = 'list-checks' WHERE icon = 'folder' AND name ~* '\y(sow|statement of work|ruang lingkup)\y';
UPDATE notebooks SET icon = 'file-signature' WHERE icon = 'folder' AND name ~* '\y(mou|nota kesepahaman|memorandum)\y';
UPDATE notebooks SET icon = 'shopping-cart' WHERE icon = 'folder' AND name ~* '\y(po|purchase order|pesanan pembelian|purchase agreement)\y';
UPDATE notebooks SET icon = 'file-text' WHERE icon = 'folder' AND name ~* '\y(spk|surat perintah kerja|work order)\y';
UPDATE notebooks SET icon = 'store' WHERE icon = 'folder' AND name ~* '\y(reseller|keagenan|distributor|mitra)\y';
