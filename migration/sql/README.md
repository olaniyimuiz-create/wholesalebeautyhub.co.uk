# migration/sql/

Drop the Adminer MySQL export of the WooCommerce database here as `dump.sql`
before running the pipeline (see repo root README).

This directory's contents are git-ignored — the dump contains full customer
PII (names, emails, phone numbers, addresses) and must never be committed.
