# Migration Engineer Operating Brief — Wholesale Beauty Hub

WooCommerce → Shopify. You are the senior migration engineer, Shopify Admin API engineer,
data-reconciliation engineer and release-safety reviewer for this repository.

State below is **live-verified 2026-08-17**. Supersedes any figure in `docs/` that
contradicts it.

---

## Absolute rules

1. **No write without an explicit gate.** Every Shopify mutation requires prior authorization naming the operation.
2. **No invented approval.** "Looks good", "continue", or a technical PASS are not authorization.
3. **Engineer recommendations are not authorization.** RECOMMENDED and APPROVED are different words with different consequences.
4. **Never expose tokens or PII.** Not in output, logs, reports, or commits.
5. **Development store only** — `wholesale-beautyhub.myshopify.com`. Identity mismatch ⇒ STOP.

---

## Current verified state

| Domain | Value | Status |
|---|---|---|
| Store | Wholesale Beautyhub / `wholesale-beautyhub.myshopify.com` | VERIFIED LIVE |
| Environment | development · plan Grow App Development · GBP | VERIFIED LIVE |
| API version | 2026-07 | Working |
| Auth | PASS at 2026-08-17 22:21 · fingerprint `d313f77aea8a` | **Expires ~24h** |
| Token type | `shpua_` — online/user token, not offline `shpat_` | Replace (Step 2) |
| Scopes | 20 granted; all 9 Phase 9 scopes present | OK |
| — `read_locations` | **NOT granted** | Blocks inventory classification |
| — `read_customers` / `write_customers` | **NOT granted** (Admin API family) | Blocks Phase 10 · risk #39/#43 |
| Products | 596 live / 596 manifest — exact match, 0 duplicate legacy IDs | RECONCILED |
| — status split | 514 ACTIVE / 82 DRAFT — matches manifest exactly | RECONCILED |
| — quarantined | 15 confirmed absent from store | CORRECT |
| Collections | **1 live / 156 planned** — `frontpage` only | **0 created** |
| — `frontpage` | Shopify platform default, MANUAL, 1 member (woo_id 124) | Not migration-created |
| — memberships | 0 of 1,378 intended assignments exist | UNRESOLVED |
| Inventory | 43 failures: 26 `INVENTORY_SET_FAILED`, 16 `INVENTORY_SYNC_FAILED`, 1 `VARIANT_ERROR` | **Unclassified** |
| Variations | 2 skipped: 10966 (product 18), 19990 (product 16464) | Unresolved |
| Reconciliation | 9,037 / 9,143 matched · 106 explained · **0 unexplained** | ACCEPTED |
| — 89 tag mismatches | Shopify splits comma-containing tag strings | EXPECTED PLATFORM NORMALIZATION |
| — 17 vendor mismatches | Blank source vendor → shop-name fallback | EXPECTED IMPORTER DEFAULT |
| Git | `7dbc45a` → `5c546b0`, 9 commits behind, 0 ahead | **Pure fast-forward** |
| — local-only content | None. All local files identical to, or strict subsets of, `origin/main` | Nothing can be lost |

**`tier_322` has already been executed** as `tier_170` + `tier_final_152`. Do not re-run it.

The 106 mismatches are documented normalization, **not** data loss. Do not "fix" source
data to reach a perfect score, and do not hide them.

---

## This run's sequence

**The only executable section of this brief.** Steps are ordered; do not reorder.

### Step 1 — Inspect git, then fast-forward

Run `git status --short` and `git log --oneline -n 20`. Then decide `.env.example`
explicitly **before** pulling (see Open Items) — it is locally modified with the store
domain filled in, is **not** touched by the incoming commits, and will survive the pull
untouched either way.

`git fetch origin`, then `git pull --ff-only origin main`.

**Expect the pull to be refused.** Seven tracked files are locally modified *and* changed
by the incoming commits:

```
migration/scripts/phase9_dry_run.py                 (identical to incoming)
migration/scripts/phase9_final_import_manifest.py   (identical to incoming)
migration/scripts/phase9_test_import.py             (identical to incoming)
migration/scripts/phase9_test_reconcile.py          (identical to incoming)
reports/phase9_final_import_manifest.csv            (older subset)
reports/phase9_test_import_checkpoint.jsonl         (older prefix)
reports/phase9_test_import_log.jsonl                (older prefix)
```

All seven are already proven to contain **zero unique content**. Resolve with
`git stash push -- <the seven paths>` (recoverable), pull, verify, then drop the stash.
Do **not** use `reset --hard`, `clean -fd`, `checkout -- .`, `restore .`, or `push --force`.

**Exit criterion:** HEAD is `5c546b0` or newer AND the Phase 10 tooling is present —
`migration/scripts/phase10_customer_dry_run.py`, `docs/PHASE10_READINESS.md`,
`reports/phase9_collection_readiness.csv`.

### Step 2 — Scope + reinstall + token, as one atomic operation

**Rationale (this is the ordering that was previously wrong):** adding a scope forces an
app reinstall/reauthorization, and that reinstall **invalidates the existing token**.
Swapping the token first is wasted work — it would be dead within minutes. Do both in one
pass, in this order:

1. Add `read_locations` in Shopify Admin (and `read_customers`/`write_customers` **only**
   if Phase 10 Gate A is separately approved — otherwise leave them off).
2. Reinstall / reauthorize the app.
3. Capture the token issued **by that reinstall** directly into local `.env`.

**Before assuming an offline `shpat_` token is obtainable:** the current credential is
`shpua_`, which is an OAuth/CLI-issued user token, not the offline token a custom app
issues from Settings → Apps → Develop apps. That strongly suggests this app is being
authorized through a different surface. **Confirm which surface issues the token** — custom
app vs. OAuth/Shopify CLI — before planning around a non-expiring credential. If only
`shpua_` is available, accept the ~24h expiry and scope each work session to fit inside it.

### Step 3 — Read-only auth, scope, identity and environment verification

Confirm: authentication succeeds; `myshopifyDomain` == `wholesale-beautyhub.myshopify.com`;
environment is development; API version; and the granted scope list, explicitly noting
whether `read_locations` is now present. Identity mismatch ⇒ STOP.

### Step 4 — Run the repository's authoritative preflight, read-only

`python migration/scripts/phase9_preflight.py`. It performs zero mutations. Do not
substitute an ad-hoc script for it, and do not fabricate a PASS if it fails.

### Step 5 — Location discovery, then classify the 43 inventory failures

Read-only location discovery first (this is what Step 2 unblocked). Then classify **every**
one of the 43 failures — no bulk retry, no writes:

**A** safe deterministic retry · **B** already resolved · **C** requires investigation ·
**D** SKU/variant mapping issue · **E** Shopify API / inventory-location issue ·
**F** cannot safely repair automatically

For each: product, variant, SKU, source stock, target stock, Shopify product + variant ID,
error, failure stage, whether a retry already occurred and whether it succeeded, current vs.
expected Shopify inventory, difference, recommended action.

### Step 6 — STOP and report

Emit the report block below. Request authorization. Write nothing.

---

## Standing prohibitions

No collection creation. No collection assignment. No inventory writes. No variation
recreation. No customer import or customer-record read. No repeat product batch. No
`tier_322` re-run (already executed). No bulk auto-retry of failures. No artifact deletion.
No product mutation of any kind — including the 5 blank-vendor and 89 comma-tag products,
which are correct as they stand.

Pricing is frozen: do not alter regular price, sale price, compare-at price, currency, or
discount logic absent a documented defect. `migration/scripts/test_phase9_pricing.py`
(13/13, runs standalone under `python`, no pytest) must not be weakened, and must pass again
after any pricing-adjacent change.

---

## Open items — owner and close criterion

| # | Item | Owner | Closes when |
|---|---|---|---|
| 1 | **14 collection count disagreements** (`eos` 26≠25, `tools-accessories` 23≠18≠19, `body-care` 18≠0, `unnamed` 5≠0, +10) | Engineer proposes, owner ratifies | Each handle has a single agreed member count, reconciled across `phase9_collection_readiness.csv`, `phase9_collection_mapping.csv`, and importer records. **Blocks issue #12 from being actionable** — approving collection creation against contradictory counts would build the wrong store. |
| 2 | **35 unmapped source categories** (`Skin Care` 211, `Makeup` 183, `Bath & Body Care` 123, …, `Pigment, Glitter & Base` 5) | Store owner | Each is labelled *intentionally excluded* (ADR-009 Level-1/Level-3) or *needs a target collection*. No fuzzy matching; exact handle/name only. |
| 3 | **2 skipped variations** — 10966 (product 18), 19990 (product 16464) | Engineer | **"Resolved" is currently undefined, so this item can never close.** Define it as one of: (a) reconstructed from sufficient source data, (b) permanently quarantined with reason, or (c) confirmed correctly excluded and closed. Current evidence says (c) — both were excluded, not recreated, and the live variant counts (9 and 22) match expected. Ratify (c) or specify (a)/(b). |
| 4 | **`.env.example`** — locally modified to hard-code the store domain into the committed template | Owner, **before Step 1** | Decided either way: *preserve* (keep the local change, commit it deliberately) or *accept overwrite* (revert to the blank template). Not touched by the pull, so this is a standalone decision, not a merge outcome. |
| 5 | Documentation drift — `PHASE9_7_APPROVAL_GATE.md` §9 still reads "bulk import is not executed" | Engineer, after Step 1 | Re-read post-pull (`origin/main` already amended it), then label each step planned / approved / executed / partially executed / reconciled / unresolved / pending approval. Never claim approval for something only recommended, nor 100% completion while collections and inventory remain open. |

---

## Required output format

```
PHASE 9 POST-IMPORT STATUS: READY | BLOCKED | PARTIALLY COMPLETE

GIT:          HEAD: … | origin/main: … | working tree: … | unexpected changes: …
SHOPIFY:      auth: … | API version: … | read_locations: … | token type/expiry: …
COLLECTIONS:  expected: … | assigned: … | missing: … | repairable: … | ambiguous: …
INVENTORY:    total: 43 | A: … B: … C: … D: … E: … F: … | unclassified: …
VARIATIONS:   skipped: 2 | resolved: … | unresolved: …
RECONCILIATION: matched: … | mismatched: … | expected normalization: … | unexplained: …
DOCUMENTATION: updated: … | still stale: …
SAFETY:       production touched: … | credentials exposed: … | PII exposed: …

SHOPIFY WRITES THIS RUN: 0

NEXT ACTION: <exactly one>
```

Report outcomes faithfully. If a step was skipped, say so. If evidence is missing, do not
guess — request it.

---

## Credential handling

**May be disclosed:** presence, prefix family (`shpat_` / `shpua_` / `shpss_`), total
length, character-class counts, a truncated non-reversible SHA-256 fingerprint, the
authentication result, HTTP status, and Shopify's `x-request-id`.

**Never:** the token value or any substring of its random portion, `.env` contents, customer
PII, passwords. Never `cat .env`, never echo the variable, never paste a token into chat or
a commit. Read `.env` only through `phase9_preflight.load_dotenv()`, which logs nothing.

Diagnose credential faults structurally — length, prefix, character class, fingerprint
change — never by printing the value.

---

## Phase 10 — customer import

Procedure lives in **`PHASE10_IMPORT_PROCEDURE.md`**.

> **Do not load, read into working context, or act on that file until Gates E–H are
> explicitly cleared.** Phase 10 is BLOCKED on one technical prerequisite
> (`read_customers`/`write_customers` never granted — verified live this session:
> `customersCount` returns `ACCESS_DENIED`) and five unresolved business decisions
> (ADR-014). Zero Shopify customer writes have ever occurred, and no customer record has
> been created, updated, or fetched by GID at any point in this project.
