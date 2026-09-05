# Phase 10 — Customer Migration Decision Matrix

**Last updated: 2026-08-22 (Gates 1–5 signed; Gate 7 requested, unsigned) · Repository HEAD at time of writing: `d603a23`**

---

## Three things this document keeps apart

These are routinely conflated, and conflating them is how an unapproved
migration gets run by accident.

| Term | Means | Who grants it | Current state |
|---|---|---|---|
| **TECHNICAL RECOMMENDATION** | Engineering has investigated and proposes an approach. Carries no permission whatsoever. | Migration engineer | Given for 4 of 10 decisions |
| **ARCHITECTURE RATIFICATION** | A technical recommendation accepted as the settled design. Closes the engineering question. Still carries **no** permission to write to Shopify. | Tech lead | **Granted for Decision #1 on 2026-08-21** |
| **BUSINESS APPROVAL** | The business owner has accepted a recommendation as policy. Still carries no permission to write to Shopify. | Store owner / data controller | **Granted 2026-08-22 for all five blocking decisions** (ADR-014 Gates 1–5) |
| **LIVE EXECUTION AUTHORIZATION** | Explicit, separate sign-off to run a specific operation against a specific store on a specific date. | Store owner, per run | **Granted once** — Gate 6, the 10-customer test, executed and reverted 2026-08-22. **Gate 7 (bulk) requested 2026-08-22, NOT granted** |

**Every blocking business decision is now made. No bulk write is authorized.**
The five decisions that governed *what gets sent* closed on 2026-08-22. What
they do not do is authorize sending it: that is ADR-014 Gate 7, which is
requested and unsigned. A signed Gate 1–5 and an unsigned Gate 7 is exactly the
state this table exists to keep legible.

**No customer has been migrated.** Nine were created and deleted under Gate 6's
test authorization on 2026-08-22; the store holds 0 customers, verified by
listing records.

---

## How a gate is closed

A gate is closed when its `Decision:` field in **[ADR-014](DECISIONS.md#adr-014-phase-10-customer-import--business-decisions-required)**
is filled in and dated — not when a task list mentions it, and not when the
technical work behind it is finished.

ADR-014 was rebuilt on 2026-08-22 as a formal sign-off instrument: seven gates,
each with the question, the prepared evidence, the recommendation where one can
honestly be made, and a `Decision:` field. **Six of the seven are now filled in
and dated — Gates 1, 2, 3, 4, 5 and 6. Gate 7 is blank.** Two gates were added
that did not exist when the ADR was drafted — Gate 6 (test cohort) and Gate 7
(bulk import) — because authorizing a 10-customer test is a different decision
from authorizing 11,849, and neither is implied by Gates 1–5.

`phase10_preflight.py` reads the **Blocking?** column below and will not report
READY while any gate is open. Marking a gate closed here without a recorded
decision in ADR-014 would defeat that check, so the two must be updated
together.

**One limit of that check, stated plainly**: it reads this table, and this table
covers the ten *policy* decisions. Gates 6 and 7 are execution authorizations
and have no row here, so **a READY pre-flight does not mean the bulk import is
authorized** — it means every technical precondition passes and every policy
question is settled. Gate 7 is the only thing that authorizes a run, and
`phase10_preflight.py` says so in its own output.

## Decision matrix

| # | Decision | Status | Owner | Evidence | Recommendation | Blocking? | Next action |
|---|---|---|---|---|---|---|---|
| 1 | **`customerSet` vs `customerCreate` + `customerAddressCreate`** | **RATIFIED 2026-08-21 — CLOSED** | Tech lead | [PHASE10_CUSTOMER_SET_DECISION.md](PHASE10_CUSTOMER_SET_DECISION.md); live introspection of API 2026-07; Shopify custom-ID docs; Shopify staff forum confirmation | **`customerCreate` + `customerAddressCreate` is the Phase 10 customer migration architecture.** `customerSet` is not deferred pending more evidence — it is out of scope. No further investigation unless a change request explicitly reopens this | **No** — closed | None. Reopening requires an explicit change request |
| 2 | **Phone collisions** | **CLOSED 2026-08-22 — ADR-014 Gate 1 signed** | Store owner / reviewer | [phase10_phone_collision_summary.json](../reports/phase10_phone_collision_summary.json) (tracked, aggregate); `phase10_phone_collision_review.csv` (gitignored, 578 rows, per customer) | Approved rule applied: keep the number only where evidence shows individual ownership; omit where it reads shared/business/placeholder. **231 of 240 groups now have a concrete recommendation** — 76 `KEEP_ONE`, 155 `OMIT_FROM_ALL`, 9 `MANUAL_REVIEW_REQUIRED`. Per customer: 76 send, 422 omit, 19 held. The 27-customer group is `OMIT_FROM_ALL` | **No** — closed | None for the run. 76 send, 441 omit. The 9 contested groups (19 customers) stay unadjudicated by choice; a number can be added later with `customerUpdate`, no re-import |
| 3 | **Address policy (A: billing only / B: billing + shipping)** | **CLOSED 2026-08-22 — `A_PLUS` selected (ADR-014 Gate 2)** | Store owner | Measured post-ratification: **Option A = 4,713 address calls · Option B = 5,922**. 4,730 customers end with at least one address; 7,351 have none in source | **Selected: `A_PLUS` — billing, falling back to shipping only where there is no billing.** 4,730 calls, measured. Neither offered option was free: A left **17** customers with no address while their data sat unused, B gave **1,192** customers a second address Shopify shows unlabelled. A_PLUS pays neither price and sends at most one address each | **No** — closed | None. Implemented as `ADDRESS_POLICY_BILLING_ELSE_SHIPPING`; A and B remain available |
| 4 | **Marketing consent** | **CLOSED 2026-08-22 — APPROVED (ADR-014 Gate 3)** | Store owner / data controller (or DPO) | [PHASE10_GDPR_CONSENT.md](PHASE10_GDPR_CONSENT.md) §4–5; FluentCRM subscribed 6,295 · unsubscribed 229 · pending 21 · no signal 5,551 | **Approved: carry FluentCRM `subscribed` forward** as `emailMarketingConsent.marketingState = SUBSCRIBED` for the 6,295. Unchanged and never in question: 229 `unsubscribed` stay UNSUBSCRIBED, 21 `pending` and 5,551 no-signal are omitted. Recorded with its limits — this pipeline never saw how FluentCRM originally collected consent | **No** — closed | None. Applicable post-import via `customerEmailMarketingConsentUpdate`, so it does not gate import order |
| 5 | **292 missing-email records** | **CLOSED 2026-08-22 — PERMANENT_EXCLUSION confirmed (ADR-014 Gate 4)** | Store owner | All 292 are guests: no user account, no phone, no billing address, 291 have only a name. The guest fallback reads `wp_wc_order_addresses` **keyed by email**, so it cannot recover one | **PERMANENT_EXCLUSION — confirmed.** No email is fabricated, synthesised or derived. Already outside the 12,096, so this confirms the classification rather than changing a number. Their orders remain migratable separately as guest orders | **No** — closed | None. Order-side migration is a separate question |
| 6 | **247 conflicting-name records** | **CLOSED 2026-08-22 — `EXCLUDE_AFFECTED_CUSTOMERS` (ADR-014 Gate 5)** | Store owner / reviewer | [phase10_name_conflict_summary.json](../reports/phase10_name_conflict_summary.json) (tracked); `phase10_name_conflict_review.csv` (gitignored, 247 rows, every `chosen_name` empty) | **Nothing guessed, nothing pre-selected.** Reviewer writes one of `IMPORT_NAME` / `ALTERNATE_NAME` / `MANUAL_REVIEW`; an unrecognised token raises rather than being interpreted. Triage: **94 genuinely ambiguous, 153 near-mechanical**. **Policy selected: `EXCLUDE_AFFECTED_CUSTOMERS`** — import **11,849** now, the 247 follow once confirmed. No customer is created under an unconfirmed name | **No** — closed | None for the run. The 247 name decisions are now follow-up work, not a gate — still owed to those customers, 16 of whom would otherwise import nameless |
| 7 | **`custom.legacy_woo_customer_id`** | **RATIFIED 2026-08-21 — YES, MANDATORY** | Tech lead | [phase10_metafield_readiness.json](../reports/phase10_metafield_readiness.json) (tracked) | **Every created customer receives it**, inline in the same `customerCreate` call. Verified across the full population: 12,096/12,096 carry it, 12,096 distinct values, all plain integers, 0 missing. No parameter exists to omit it | **No** — closed | None |
| 8 | **`custom.woo_registered_at`** | **RATIFIED 2026-08-21 — RETAIN** | Store owner | [phase10_metafield_readiness.json](../reports/phase10_metafield_readiness.json); dates verified authentic against `wp_users.user_registered` (7,073 rows, 100% same-day match) | **Retained as a customer metafield.** Present for the 6,649 registered accounts; omitted — never invented — for the 5,447 guests, who have no registration event. `Customer.createdAt` is never written, manipulated, or approximated | **No** — closed | None |
| 9 | **16 addresses with no country** | **RATIFIED 2026-08-21 — CLOSED** | Store owner | [phase10_address_readiness.json](../reports/phase10_address_readiness.json) (tracked); `phase10_address_exceptions.csv` (gitignored, 16 rows) | **`Customer = IMPORT`, `Address = SKIPPED_INVALID_COUNTRY`. GB is never assumed.** Measured across all 12,096: 16 skips, every one `MISSING_COUNTRY`, zero invalid or unknown-region codes. 0 customers blocked | **No** — closed | None. The 16 remain importable; supplying countries in WooCommerce would recover their addresses |
| 10 | **Non-GB `provinceCode` mapping** | **CLOSED 2026-08-22 — validated against Shopify's own codes; live cross-check an accepted limitation** | Migration engineer | [migration/schema/shopify_province_codes.json](../migration/schema/shopify_province_codes.json) — 756 codes across 22 countries, from `Shopify/worldwide` region data | Hand-written allowlist **replaced** by value-level validation: GB omits (ratified override); a country with provinces validates the code, sending valid and omitting+flagging invalid; a country without provinces omits+flags. Measured: **0 invalid non-GB values in the data** | **No** — closed | None. Live cross-check recorded as an **accepted limitation** (risk #43); no scope change requested |

---

## Blocking summary

**Nothing blocks any more.** All five decisions that blocked a live customer
write — phone collisions (#2), address policy (#3), consent (#4), missing email
(#5), conflicting names (#6) — were signed on **2026-08-22** as ADR-014 Gates
1–5. Decisions #1, #7, #8, #9 and #10 closed earlier and never blocked.

| ADR-014 gate | Matrix decision | Outcome |
|---|---|---|
| Gate 1 | #2 phone collisions | 231 recommendations confirmed; the 9 contested groups omit the phone. 76 send, 441 omit |
| Gate 2 | #3 address policy | `A_PLUS` — billing, else shipping. **4,730 calls** |
| Gate 3 | #4 marketing consent | Approved: 6,295 `subscribed` carried forward |
| Gate 4 | #5 292 missing emails | `PERMANENT_EXCLUSION` confirmed |
| Gate 5 | #6 247 conflicting names | `EXCLUDE_AFFECTED_CUSTOMERS` — **import population 11,849** |
| Gate 6 | — | Executed and reverted 2026-08-22. Store back to 0 |
| **Gate 7** | — | **Requested 2026-08-22. NOT GRANTED.** Nothing may be written until it is |

**The import population is 11,849, not 12,096.** Gate 5 holds back the 247
name-conflict customers until a reviewer confirms their names. They are held,
not dropped, and import later with no rework.

**Two things are settled but not done**, and neither blocks:

* The **9 contested phone groups** (19 customers) were not adjudicated — the
  owner took the safe default rather than guess whose number it is. Reviewable
  any time; a number can be added post-import with `customerUpdate`.
* The **247 name decisions** are still every one of them blank. Under the
  selected policy they are follow-up work rather than a precondition, but they
  remain genuinely owed to those customers — 16 would otherwise import with no
  name at all.

---

## Conflicting identities — policy (Decision #6)

Every one of the 247 conflicting emails **already has an IMPORT row**. The
customer is created either way. The real question is not whether they migrate
but whether they migrate carrying a name nobody has confirmed.

Three policies are implemented; the runtime enforces whichever is selected:

| Policy | Effect | Trade-off |
|---|---|---|
| `BLOCK_ENTIRE_MIGRATION` | No customer imports until all 247 are confirmed | Holds 12,096 customers hostage to 247 name variants. Raises rather than returning a population, so a caller cannot ignore it |
| **`EXCLUDE_AFFECTED_CUSTOMERS`** *(recommended, and the code default)* | Import 11,849 now; the 247 follow once confirmed | No customer is ever created with an unconfirmed name, and nothing is lost — the 247 import later with no rework |
| `PROCEED_WITH_IMPORT_NAME` | Import all 12,096; correct names afterwards | Up to 247 customers temporarily carry a possibly-wrong name. A customer-facing error — wrong name on receipts and email — for a window of unknown length |

**Recommendation: `EXCLUDE_AFFECTED_CUSTOMERS`.** Blocking all 12,096 on 247
name variants is disproportionate, and creating a customer under a name no
human has confirmed is the one outcome with a real-world cost. Excluding avoids
both: 98% of the migration proceeds, and the remaining 2% waits for a decision
that is genuinely owed to those customers.

**APPROVED 2026-08-22 — `EXCLUDE_AFFECTED_CUSTOMERS`** (ADR-014 Gate 5,
project/store owner). The import population is therefore **11,849**. The code
default already matched, so an unconfigured run fails safe rather than fast.

The 247 `chosen_name` cells remain blank and no longer block the run. That is
the policy working as intended, not the review being abandoned: nobody is
created under an unconfirmed name, and the 247 import unchanged once someone
works the file.

### Triage (classification only — never a selection)

| Class | Count | What the reviewer is looking at |
|---|---:|---|
| `GENUINELY_DIFFERENT_NAMES` | **94** | Two different people's names, or one person under two identities. Real judgement needed |
| `ALTERNATE_BLANK_IMPORT_HAS_NAME` | 87 | The duplicate row carries no name at all |
| `DIFFERS_ONLY_BY_CASE_OR_WHITESPACE` | 39 | e.g. `ADA LOVELACE` vs `Ada Lovelace` |
| `IMPORT_BLANK_ALTERNATE_HAS_NAME` | 16 | The customer would be created **nameless** while a name exists on the duplicate |
| `IDENTICAL_AS_DISPLAYED_FIELD_SPLIT_DIFFERS` | 11 | Same name, split differently across first/last |

153 of 247 are near-mechanical; **94 need real thought.** The class column
exists so a reviewer can sort the work — it never fills `chosen_name`, not even
where the answer looks obvious. The 16 `IMPORT_BLANK` rows are worth doing
first: those customers currently import with no name at all.

---

**Decision #1 is closed and no longer appears in any pending list.**

---

## Settled architecture (Decision #1, ratified 2026-08-21)

The Phase 10 customer migration will use:

```
Stage 1   customerCreate(input: CustomerInput!)
            └─ metafields: [{ custom.legacy_woo_customer_id }]   inline, same call
Stage 2+  customerAddressCreate(customerId:, address: MailingAddressInput!, setAsDefault:)
            └─ one call per address, only after Stage 1 returns a customer id
```

`customerSet` is **out of scope**, not deferred. The distinction matters: this
is not a question awaiting more evidence, and it should not be re-investigated,
re-costed, or re-proposed. The four UNKNOWN items listed in
[PHASE10_CUSTOMER_SET_DECISION.md](PHASE10_CUSTOMER_SET_DECISION.md) that
concerned `customerSet` are now moot and require no live testing.

Reopening requires an explicit change request. Plausible triggers, recorded so
that reopening is a deliberate act rather than a drift: Shopify deprecating
`customerAddressCreate`, a future API version adding `metafields` to
`CustomerSetInput`, or the address volume growing far beyond the current 4,729.

This ratifies **which mutations the importer will call**. It does not authorize
calling them.

---

## Phone collisions — detail (Decision #2)

**Approved rule, applied 2026-08-21:** keep the number only on the customer with
the strongest evidence it is genuinely their individual number; where it reads
as shared, business, or placeholder, omit it from Shopify rather than inventing
an owner. Never attempt to make Shopify accept a duplicate.

**No phone number is deleted or altered.** The source data is untouched. The
only question is whether a number is *sent* to Shopify for a given customer, and
a customer whose phone is omitted is still created in full.

| Measure | Value |
|---|---|
| Collision groups | 240 |
| IMPORT customers affected | 517 |
| Non-IMPORT rows listed as reviewer context | 61 |
| Largest group | 27 |
| HIGH RISK groups (≥10 members) | 1 |

**Recommendations by group:** 155 `OMIT_FROM_ALL` · 76 `KEEP_ONE` · 9
`MANUAL_REVIEW_REQUIRED`

**Resulting per-customer actions:** 76 `SEND_PHONE` · 422 `OMIT_PHONE` · 19
`OMIT_PHONE_PENDING_REVIEW` (= 517 ✓)

### How ownership is scored

Deterministic, explainable, and visible in the review sheet so a reviewer can
disagree on the evidence rather than on a verdict:

| Signal | Weight | Why |
|---|---|---|
| Phone is on the customer's **own** `wp_usermeta` profile | **+3** | The strongest available signal. `build_candidate()` falls back to an *order's* billing phone when a customer has no address of their own, and an order phone could belong to whoever placed it |
| Registered WP account | +2 | A real account, not a one-off guest checkout |
| Company present on the record | **−2** | Makes a business switchboard the likelier explanation |

A member must reach **3** to be called an owner at all — so a bare registered
account (2) does not qualify, because having an account says nothing about
whose phone it is.

* **`KEEP_ONE`** — exactly one member clears the threshold and is strictly ahead.
* **`MANUAL_REVIEW_REQUIRED`** — two or more members each have genuine
  individual evidence. A person must choose; guessing would hand one customer's
  phone to a record that is arguably someone else's.
* **`OMIT_FROM_ALL`** — the group is ≥10, or nobody clears the threshold.
  "We cannot tell whose this is" resolves to *send it for nobody*, never to
  *send it for whichever row we read first*.

### The 27-customer group

`OMIT_FROM_ALL`. The size rule fires before any scoring, so it would hold even
if all 27 had perfect ownership evidence — 27 people do not share a household
line, and the number reads as a shop counter, switchboard, or form default. All
27 are guest checkout rows with **zero** registered accounts, which is
consistent with that reading.

### Reviewer workflow

`reports/phase10_phone_collision_review.csv` — gitignored, one row per affected
customer, carrying `phone_number`, `customer_email`, `customer_name`,
`is_import`, the evidence signals, the recommendation and its rationale, and
three empty columns: `reviewer_decision`,
`reviewer_chosen_owner_woo_customer_id`, `reviewer_note`.

A reviewer decision always overrides the recommendation. `KEEP_ONE` rejects an
owner from outside the group. Under every combination of recommendation and
reviewer decision, **at most one customer per group is ever sent the number** —
asserted by test across five group shapes and all three decision types.

The tracked summary carries counts only. Verified: no phone number, email, or
name from the 578-row review set appears in **any** tracked file in the
repository.

---

## Addresses with no country — ratified (Decision #9)

**`Customer = IMPORT` · `Address = SKIPPED_INVALID_COUNTRY` · GB is never assumed.**

A country cannot be inferred. Defaulting to GB would invent a delivery
destination for a real person, and a wrong address is worse than no address —
it is wrong in a way that looks right, which is the failure mode that survives
review.

### Measured across all 12,096 IMPORT customers

| | Billing | Shipping |
|---|---:|---:|
| `ADDRESS_PLANNED` | 4,713 | 1,209 |
| `SKIPPED_INVALID_COUNTRY` | **16** | 0 |
| `SKIPPED_NO_ADDRESS1` (no address in source) | 7,367 | 10,887 |

**Customers blocked by an address problem: 0.**

### One thing the ratification was made without

The figure of 16 came from a *presence* check — `billing_country` being empty.
It never covered addresses where a country **is** recorded but is not a value
Shopify's `CountryCode` enum accepts: free text like "United Kingdom", or the
`ZZ` "Unknown Region" placeholder. Those skip for the same reason and had never
been counted.

They have now been measured. **All 16 skips are `MISSING_COUNTRY`. Zero
`INVALID_COUNTRY_CODE`, zero `COUNTRY_CODE_IS_UNKNOWN_REGION`.** Every country
value present in the source is a valid enum code, so 16 is the complete figure,
not a floor. The ratified decision stands on a verified number.

### Knock-on effect on Decision #3

Skipping the 16 reduces the address call counts by 16:

| Policy | Before | After |
|---|---:|---:|
| Option A — billing only | 4,729 | **4,713** |
| Option B — billing + shipping | 5,938 | **5,922** |

15 customers lose their *only* address (one of the 16 has a surviving shipping
address). All 15 still import, without an address.

### Vocabulary

The **status** is the outcome; the **reason** is why. Three source problems
collapse to one status but keep distinct reasons, because "no country recorded"
and "country recorded as something we cannot map" need different fixes from
whoever cleans the source:

| Reason | Status |
|---|---|
| `MISSING_COUNTRY` | `SKIPPED_INVALID_COUNTRY` |
| `INVALID_COUNTRY_CODE` | `SKIPPED_INVALID_COUNTRY` |
| `COUNTRY_CODE_IS_UNKNOWN_REGION` | `SKIPPED_INVALID_COUNTRY` |

`reports/phase10_address_exceptions.csv` (gitignored) carries the 16 rows with
their source country value, so a reviewer can supply the missing countries in
WooCommerce and recover those addresses on a later run. Nothing is lost —
the addresses are skipped, not discarded from the source.

---

## Customer metafields — ratified (Decisions #7 and #8)

### `custom.legacy_woo_customer_id` — MANDATORY

| | |
|---|---|
| namespace | `custom` |
| key | `legacy_woo_customer_id` |
| type | `single_line_text_field` |
| value | `wp_wc_customer_lookup.customer_id`, as a string |
| written | inline in the same `customerCreate` call, never a follow-up |

```
Woo ID  ->  custom.legacy_woo_customer_id  ->  Shopify Customer GID
```

**Verified across the whole population:** 12,096 of 12,096 carry it · 12,096
distinct values · 0 missing · 0 values that are not plain integers · unique
across the population.

This is an invariant, not a default. `build_customer_input()` has **no
parameter that could omit it**, and `assert_legacy_metafield_present()` raises
before any create if a payload arrives without one — including the near-misses:
wrong namespace, wrong key, wrong type, empty value. A customer created without
it is unidentifiable — not matchable back to WooCommerce, not skippable on a
re-run, not findable for rollback — and the failure is silent until
reconciliation, by which point the customer exists and cannot be fixed.

### `custom.woo_registered_at` — RETAINED

| | |
|---|---|
| namespace | `custom` |
| key | `woo_registered_at` |
| type | `single_line_text_field` |
| value | `wp_wc_customer_lookup.date_registered`, verbatim |

Present for **6,649** customers; absent for **5,447** (6,649 + 5,447 = 12,096).
The split is exactly registered accounts versus guests — every registered
account has a date, no guest does, which is what you would expect since a guest
checkout has no registration event. Where the source has no date, the metafield
is **omitted, never invented**: a fabricated date is indistinguishable from a
real one at the point it matters.

**The dates are authentic.** The 2024–2026 range looked narrow enough to suspect
a lookup-table backfill rather than true registration dates, which would have
made the field close to worthless. Checked against `wp_users.user_registered`,
WordPress's own authoritative record: **7,073 rows carry both dates, and 100%
match to the same calendar day.** The range is simply the store's lifetime.

### `Customer.createdAt` — never touched

Server-controlled and read-only. It is never written, manipulated, or
approximated, and it is not settable on `CustomerInput` in any case (verified
against the schema contract). `assert_no_server_controlled_fields()` rejects any
payload attempting `createdAt`, `created_at`, `updatedAt`, `updated_at`, `id`,
or `legacyResourceId` — listed explicitly because "set the created date to match
WooCommerce" is a request that sounds reasonable and cannot be honoured, and
code that appears to try implies to a reader that it works.

---

## Province codes — resolved (Decision #10)

```
GB                       ->  omit provinceCode              (ratified override)
country with provinces   ->  validate the VALUE
                                valid   -> send
                                invalid -> omit + flag
country without provinces->  omit + flag
```

### What was wrong

`PROVINCE_CODE_COUNTRIES` was a hand-written allowlist of *countries*. It never
looked at the value, so it was wrong in both directions:

* **It excluded Ireland.** Shopify accepts all 26 Irish counties as province
  codes. 32 Irish billing customers carried valid ones (`D` for Dublin, `CW`,
  `LH`, `LK`…) and every one was being silently dropped.
* **It would have sent anything.** For a listed country it passed the raw
  `billing_state` through unchecked. Had a US record carried "California"
  rather than "CA", it would have been sent and rejected at write time.

### The dataset

756 codes across 22 countries, from **Shopify's own region data**
(`github.com/Shopify/worldwide`, `data/regions/<CC>.yml`, the `zones:` list) —
not a third-party ISO table and not recalled from memory. Shopify's province
codes follow ISO 3166-2, but the accepted *set* per country is Shopify's to
define, so it is taken from Shopify.

Spot-checked against known counts: US 62 · IT 110 · ES 52 · JP 47 · NG 37 ·
IE 26 · CA 13 · AU 8 · GB 5.

### Live cross-check — ACCEPTED LIMITATION (recorded 2026-08-22)

**Shopify's published region dataset is the source of truth for this project.**

A live cross-check against this store is **unavailable**, and that is accepted
and documented rather than outstanding. Re-checked on 2026-08-22 with a working
credential: authentication succeeds and 24 scopes are confirmed, but province
lists require `read_shipping` (`deliveryProfiles`) or `read_markets`
(`markets`), and the app holds neither. Every other query-root field was
checked; none exposes province data.

**No scope change is being requested, and none should be added at this stage.**

Three things this limitation is not:

* It is **not** evidence the dataset is incorrect. It is a gap in *independent
  confirmation* of data that already comes from Shopify itself, and it must not
  be inferred as a defect.
* It is **not** a reason to change the transformation rules. They stay exactly
  as ratified.
* It is **not** an open action item. Risk register #43 records it as accepted.

`phase10_verify_province_codes.py` is kept and reports the limitation plainly —
it never claims a pass it cannot support, and never suggests a scope change. If
the app ever holds `read_shipping` for an unrelated reason, it runs unchanged
and closes the gap.

### Rules — unchanged and locked

* **GB omits `provinceCode` unconditionally. Mandatory.**
* Non-GB is treated **conservatively**: the value is validated against the
  dataset; anything unrecognised is omitted and flagged, never coerced, never
  guessed at, never partially matched.

A regression test asserts both, so neither can drift without a test failing.

### The GB rule is right, but not for the reason previously documented

Earlier notes said Shopify "does not use provinces for the United Kingdom".
That is false. Shopify defines five GB zones: `ENG`, `NIR`, `SCT`, `WLS`,
`BFP`. The rule is correct because WooCommerce's `billing_state` holds
*counties* — "Surrey", "West Midlands" — which are not those zones and never
will be. The general validator would reject all 2,483 of them anyway; the GB
entry is kept as an **explicit override** so the ratified rule holds even for a
GB record that happens to carry a literal "ENG".

### Measured across the population

| | Billing | Shipping |
|---|---:|---:|
| Non-GB values now **sent** | **73** | **26** |
| Dropped — country has no provinces (DE, FR, LT) | 15 | 5 |
| Dropped — GB ratified rule | 2,483 | 607 |
| Dropped — **invalid code** | **0** | **0** |

**Zero invalid values.** Every non-GB province code in the WooCommerce data is
one Shopify actually accepts, so validation costs nothing and recovers
everything. Against the old allowlist, this sends **+33 billing and +9 shipping**
province codes that were previously dropped — almost all Irish counties.

`DE-BE`, `DE-NW` and similar are ISO 3166-2 full-form codes on German
addresses. Germany has no Shopify provinces at all, so they are dropped for
that reason rather than for being malformed.

---

## Verified population — do not restate without reproducing

| Measure | Value |
|---|---|
| Source rows | 13,043 |
| IMPORT | 12,096 |
| Duplicate SKIP | 407 |
| QUARANTINE | 539 |
| EXCLUDE | 1 |
| UPDATE | 0 |
| Identity gate | PASS |

12,096 + 407 + 539 + 1 = 13,043 ✓ · Reproduced byte-identically on 2026-08-19.

---

## What is NOT true

Stated explicitly, because absence of a caveat reads as approval. Rewritten
2026-08-22 — several bullets here were true when written and are not any more,
and a stale honesty section is worse than none.

* Phase 10 is **not authorized to run**. Every policy decision is made; ADR-014
  Gate 7 is blank, and it is the only thing that authorizes a bulk write.
* **The bulk importer does not exist yet.** What exists is an offline runtime
  (throttling, backoff, resume, transformation, phone fallback, ledger) that
  refuses mutation documents by construction, plus `phase10_test_import.py`,
  which does send mutations and is **hard-capped at 10 records**. Signing Gate 7
  authorizes a run that still has to be built, and the build should be reviewed
  before it is used.
* No customer, address, or metafield exists in Shopify **now**. Nine customers
  and six addresses were created under Gate 6 on 2026-08-22 and deleted the same
  run; the store was verified back to 0 by listing records, not by trusting a
  count.
* No customer metafield **definition** has been created. None is required —
  metafields are written inline with values and no definition.
* Marketing consent has been **approved but never applied**. No customer carries
  it, because no customer exists.
* The 247 name conflicts are **unresolved**, and the 9 contested phone groups
  are **unadjudicated**. Both are settled as policy, neither is settled as fact.
* "Tests pass" means the offline suite passes. It does not mean ready to
  migrate, and a READY pre-flight does not mean authorized to migrate.
