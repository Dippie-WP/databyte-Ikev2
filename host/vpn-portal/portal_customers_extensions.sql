-- VPN Portal — customer table extensions (v1.3.1+)
--
-- Idempotent. Safe to re-run.
-- Adds columns referenced by app.py that are NOT in strongSwan's base schema.
--
-- Background: app.py /api/customers SELECTs `billing_id` and `email` (for a
-- planned 5E billing feature). strongSwan's customers schema does not have
-- these columns. The portal must add them itself to keep SELECT queries valid.
--
-- If you ALTER customers here, also keep app.py in sync (do not reference
-- columns this file doesn't add).

ALTER TABLE customers ADD COLUMN billing_id TEXT;
ALTER TABLE customers ADD COLUMN email TEXT;
-- v1.3.2 — EAP credential rotation (Bug #4 fix)
-- Added 2026-06-25: track when EAP creds were last rotated for a customer.
-- Used by /api/customers/{id}/rotate_eap. NULL = never rotated.
ALTER TABLE customers ADD COLUMN eap_rotated_at INTEGER;
