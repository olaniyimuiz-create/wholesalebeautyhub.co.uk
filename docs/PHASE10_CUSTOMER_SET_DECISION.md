# Phase 10 — `customerSet` vs `customerCreate` + `customerAddressCreate`

> ## DECIDED — 2026-08-21
>
> **`customerCreate` + `customerAddressCreate` is ratified as the Phase 10
> customer migration architecture.** `customerSet` is **out of scope** and will
> not be investigated further unless a change request explicitly reopens this
> decision.
>
> This document is retained as the **evidence record** for that decision, not
> as a live comparison. Do not treat the trade-off table below as an open
> question, and do not re-cost `customerSet` from it.
>
> The four items in "What would need live testing" that concern `customerSet`
> (§5 metafield write, §2 definition requirement, §9 bulk support) are now
> **moot** — they were only ever needed to make `customerSet` viable, and no
> live test should be scheduled for them. Item 4, the actual mutation point
> cost, remains open and belongs to the ratified path.
>
> **Ratification settles architecture only.** It is not business approval and
> not authorization to write. No customer has been migrated; the live store
> still holds 0 customers.

**Status of the original investigation: RECOMMENDATION ONLY. No mutation of any
kind was executed to produce this document. No metafield definition was
created. Nothing below was, or is, authorized for execution.**

Every claim is labelled:

| Label | Meaning |
|---|---|
| **VERIFIED** | Confirmed by live read-only introspection of the store's own API 2026-07 schema, or by an explicit statement in Shopify's official documentation. The evidence is quoted or reproduced. |
| **INFERRED** | A reasonable deduction from verified facts, but not directly stated anywhere. Could be wrong. |
| **UNKNOWN** | Not established. Would require a live test to settle, which is out of scope. |
| **REQUIRES BUSINESS APPROVAL** | A decision that is not ours to make. |

Schema snapshot backing every VERIFIED schema claim:
[`migration/schema/shopify_2026_07_contract.json`](../migration/schema/shopify_2026_07_contract.json),
captured 2026-08-19 from `wholesale-beautyhub.myshopify.com` at API version
`2026-07`.

---

## 1. Does `customerSet` support `customId` using `custom.legacy_woo_customer_id`?

**VERIFIED — the mechanism exists.**

```
customerSet(input: CustomerSetInput!, identifier: CustomerSetIdentifiers) : CustomerSetPayload

CustomerSetIdentifiers {
  id: ID
  email: String
  phone: String
  customId: UniqueMetafieldValueInput      <-- this
}

UniqueMetafieldValueInput {
  namespace: String
  key: String!
  value: String!
}
```

So `customId: { namespace: "custom", key: "legacy_woo_customer_id", value: "1234" }`
is structurally expressible.

**VERIFIED — but not with the metafield type Phase 10 currently plans to use.**
Shopify's [Working with custom IDs](https://shopify.dev/docs/apps/build/metafields/working-with-custom-ids)
guide uses the `id` metafield type throughout, and states that

> "ID metafield types are automatically configured to have unique values."

The `id` type is real in this store's API version — `metafieldDefinitionTypes`
returns it with `category: ID` (one of 118 declared types).

**VERIFIED — the existing product precedent is *not* compatible.** The 596 live
products carry `custom.legacy_woo_id` as:

```json
{ "type": "single_line_text_field", "definition": null }
```

An unstructured `single_line_text_field` with no definition. That is the pattern
Phase 10 proposed to copy for customers, and it **cannot serve as a `customId`**.
Adopting `customerSet` therefore means deviating from the established in-repo
pattern, not extending it.

---

## 2. Does `customId` require a unique-values metafield definition?

**VERIFIED — the capability exists in the schema.**

```
MetafieldDefinitionInput { ..., capabilities: MetafieldCapabilityCreateInput }
MetafieldCapabilityUniqueValuesInput { enabled: Boolean! }
```

**VERIFIED — uniqueness is intrinsic to the `id` type.** Per the custom IDs
guide, `id`-type metafields are automatically configured for unique values;
uniqueness is not an optional toggle you enable on an arbitrary type.

**INFERRED — a definition is required.** The guide's Step 1 is creating an ID
metafield definition, and it is presented as the foundational prerequisite for
everything that follows. Uniqueness has to be enforced somewhere, and an
unstructured metafield has no definition on which to enforce it. This is a
strong inference, but Shopify does not state "a definition is mandatory" in
those words on the page reviewed.

**UNKNOWN — whether `customerSet` rejects a `customId` pointing at a
non-existent or non-unique definition, or silently degrades.** Settling this
requires executing `customerSet`, which is out of scope.

---

## 3. Is the definition mandatory before `customerSet` can be used?

**Split answer — this distinction matters and is easy to get wrong.**

* **VERIFIED — no, not for `customerSet` as such.** The `identifier` argument
  is nullable (`CustomerSetIdentifiers`, not `CustomerSetIdentifiers!`), and
  Shopify's [customerSet](https://shopify.dev/docs/api/admin-graphql/latest/mutations/customerSet)
  documentation states: *"To create a new customer omit the `identifier`
  argument."* `customerSet` is usable today, with zero definitions in the store,
  as a plain create.

* **INFERRED — yes, for the *only* form of `customerSet` that Phase 10 would
  want.** Without `customId`, `customerSet` gives no idempotency benefit over
  `customerCreate` and would still leave the legacy ID unwritten. The reason to
  adopt it at all is the upsert, and the upsert needs the definition.

**Practical consequence:** the store currently has **zero** customer metafield
definitions (verified live). Choosing `customerSet` adds a mandatory
prerequisite step — creating an `id`-type definition — that
`customerCreate` does not have. **That step is explicitly not authorized and
was not performed.**

---

## 4. Can `customerSet` create addresses atomically?

**VERIFIED — yes.**

```
CustomerSetInput {
  addresses: [MailingAddressInput!]     <-- present
  email, firstName, lastName, locale, note, phone, tags, taxExempt, taxExemptions
}
```

Compare `CustomerInput`, which has **no** `addresses` field (verified — this is
the defect corrected in Priority 4). A customer and all their addresses in one
call is a genuine and material advantage: it removes the "customer created,
address failed" partial state entirely for the 4,745 customers with at least
one address.

**VERIFIED — with a destructive caveat that must not be overlooked.** The
customerSet documentation states:

> "Any list field... will be updated so that all included entries are either
> created or updated, and all existing entries not included will be deleted"

`addresses` is a list field. On any **re-run**, omitting addresses from the
input **deletes the customer's existing addresses**. For a first import into an
empty store this is harmless. For the resume-after-interruption behaviour
Phase 10 requires, it is a live hazard: a resumed run that re-touches an already
imported customer without re-sending their addresses will silently destroy them.
`customerCreate` + `customerAddressCreate` has no equivalent failure mode.

---

## 5. How is the legacy Woo ID retained, given `CustomerSetInput` has no `metafields`?

**VERIFIED — `CustomerSetInput` has no `metafields` field.** Its complete field
list is: `addresses, email, firstName, lastName, locale, note, phone, tags,
taxExempt, taxExemptions`. Explicitly re-checked; the absence is real.

**INFERRED — the `customId` metafield is written by the upsert itself.** That is
the entire premise of a custom ID: you identify a record by a value that Shopify
persists on the record. If `customerSet` did not write it, an upsert could never
match on a second run. Strong inference, not directly stated on the pages
reviewed.

**VERIFIED — but no *other* metafield can accompany it.** Shopify staff
(Liam-Shopify) confirmed on the developer forum that combining `customId` with a
`metafields` array in a Set mutation
[fails by design](https://community.shopify.dev/t/productset-mutation-metafields-failing-when-upserting-with-customid-but-works-with-handle/27893) —
the guard prevents accidentally deleting the identifying metafield, because the
custom ID *is* a metafield. The stated workaround is exactly the split described
in §6.

**Consequence:** with `customerSet`, `custom.woo_registered_at` (the proposed
registration-date metafield) cannot be written in the same call. It needs a
separate operation, or it must be dropped.

---

## 6. Would a separate `metafieldsSet` be required?

**Depends on a decision that is not ours:**

| Scenario | Extra calls | Basis |
|---|---|---|
| Legacy ID only, `woo_registered_at` dropped | **0** | INFERRED — customId written by the upsert |
| Legacy ID + `woo_registered_at` retained | **12,096** | VERIFIED — the forum guard forces a second call |

`metafieldsSet(metafields: [MetafieldsSetInput!]!)` is declared and accepts
`{ ownerId, namespace, key, value, type, compareDigest }`, so the follow-up is
straightforward — but it costs one extra call per customer, which **erases the
entire call-volume advantage** that motivated looking at `customerSet`:

| Path | Calls (Option A addressing) |
|---|---|
| `customerCreate` + `customerAddressCreate` | 12,096 + 4,729 = **16,825** |
| `customerSet`, registration date dropped | **12,096** |
| `customerSet` + `metafieldsSet` for registration date | 12,096 + 12,096 = **24,192** |

**REQUIRES BUSINESS APPROVAL — is `custom.woo_registered_at` worth keeping?**
It is informational only. Dropping it makes `customerSet` decisively cheaper;
keeping it makes `customerSet` the *most* expensive of the three options.

---

## 7. Does `customerSet` provide the restart/idempotency guarantees Phase 10 requires?

**Better in one dimension, worse in another. This is the crux of the decision
and it does not reduce to a call count.**

**Better — VERIFIED.** Idempotency becomes server-side. Shopify states:

> "If a customer with the specified unique key exists, it will be updated. If
> not, a new customer will be created with that unique key"

A re-run cannot create a duplicate, regardless of what the client believes. With
`customerCreate`, duplicate prevention depends on the client correctly
consulting its legacy map first — a client bug can create duplicates.

**Worse — VERIFIED.** The list-field deletion semantics from §4 mean a resumed
run is *destructive by default* toward addresses. Phase 10's resume design
(rebuild the legacy map, skip what exists) is safe with `customerCreate` because
skipping is a genuine no-op. With `customerSet`, any re-touch must carry the
complete address set or destroy what is there. Safe, but only if implemented
exactly right — and it converts a harmless class of client bug into a
data-destroying one.

**INFERRED — both meet the stated requirement**, provided the address semantics
are handled correctly. Neither is disqualified.

---

## 8. Is `customerSet` suitable for this migration volume?

**VERIFIED — nothing in the schema or documentation limits it by volume.** No
per-mutation record cap was found; the constraint is the shared rate-limit
bucket (2,000 points, 100/s restore — measured live), which applies identically
to all three paths.

**INFERRED — the volume argument is weaker than it appears.** At a measured
restore rate of 100 points/s and an assumed 10-point mutation cost, the
difference between 16,825 and 12,096 calls is roughly **8 minutes** on a run
already estimated at ~28 minutes. Both complete inside half an hour. Volume is
not a real differentiator at this scale, and — per the explicit instruction in
the brief — is not a sufficient reason to choose `customerSet`.

---

## 9. Are customer mutations supported by bulk operations?

**VERIFIED — no.**

* The complete list of `customer*` mutations declared by API 2026-07 contains
  **no** bulk or batch variant (30 mutations, checked exhaustively).
* Shopify's [bulk import documentation](https://shopify.dev/docs/api/usage/bulk-operations/imports)
  enumerates `productCreate`, `collectionCreate`, `productUpdate`, and
  `productUpdateMedia` as its valid values. No customer mutation appears.

**UNKNOWN — whether `bulkOperationRunMutation` would in practice accept
`customerCreate` or `customerSet`.** The same page also contains the broader
claim that "you can supply any GraphQL Admin API mutation... except
`bulkOperationRunMutation` and `bulkOperationRunQuery` themselves", which
contradicts its own enumerated list. That contradiction is not ours to resolve,
and resolving it experimentally would require submitting a bulk mutation.

**Planning position: assume no bulk support.** One call per customer,
concurrency 1. This is the conservative assumption and it is what the throttle
framework implements.

---

## Comparison

| Dimension | `customerCreate` + `customerAddressCreate` | `customerSet` |
|---|---|---|
| Calls (Option A) | 16,825 | 12,096 — or 24,192 if `woo_registered_at` is kept |
| Prerequisite | none | `id`-type metafield definition (**not authorized**) |
| Legacy ID | written inline via `metafields` — **VERIFIED supported** | written by the upsert — **INFERRED** |
| Additional metafields | supported inline | **blocked** alongside `customId` — VERIFIED |
| Addresses | separate call each | atomic — VERIFIED |
| Partial-address failure state | possible | eliminated |
| Idempotency | client-side, via legacy map | server-side — stronger |
| Resume hazard | none | **address deletion on re-touch** — VERIFIED |
| Matches in-repo precedent | yes (596 products) | no — type and pattern both differ |
| Est. runtime | ~28 min | ~20 min |

---

## Recommendation

**Recommend `customerCreate` + `customerAddressCreate` for the initial import,
and defer `customerSet` — but not on the call count.**

The reasoning, in order of weight:

1. **`customerCreate` writes the legacy ID with a VERIFIED mechanism.**
   `customerSet` writes it by INFERENCE. For the field that the entire
   reconciliation, resume, and rollback design depends on, a verified mechanism
   beats an inferred one.
2. **`customerSet` requires creating a metafield definition first** — explicitly
   outside what is authorized, and a new store-level object with its own
   approval implications.
3. **The address-deletion semantics turn a benign resume bug into a destructive
   one.** Phase 10's whole posture has been to prefer loud failure over silent
   damage; this cuts against that.
4. **It abandons the working in-repo precedent.** 596 products were imported
   successfully with unstructured `single_line_text_field` legacy IDs. Customers
   would use a different type, a definition, and a different mutation — more new
   surface, on the higher-risk dataset.
5. **The call-count advantage is ~8 minutes** and inverts to a 44% *penalty* if
   `woo_registered_at` is retained.

**What would have changed this recommendation** (recorded for the historical
record — this is no longer a live route): if the business had decided
`woo_registered_at` was worthless, making `customerSet` a clean 12,096 calls,
and had accepted creating the definition, then `customerSet`'s atomic addresses
and server-side idempotency would have been genuinely attractive.

That path was considered and closed on 2026-08-21. `woo_registered_at` is now
independent of this choice: under the ratified architecture it rides inline in
`CustomerInput.metafields` at no additional call, so retaining or dropping it is
a pure cost/value question with no architectural consequence.

**Decision status:**

1. ~~Adopt `customerCreate` + `customerAddressCreate`, or `customerSet`?~~
   **RESOLVED 2026-08-21 — `customerCreate` + `customerAddressCreate`.**
2. ~~If `customerSet` — authorize creating an `id`-type metafield definition?~~
   **MOOT.** No metafield definition is required by the ratified path, and none
   should be created.
3. Retain or drop `custom.woo_registered_at`? **Still open**, but now purely a
   cost/value question — see Decision #8 in
   [PHASE10_DECISION_MATRIX.md](PHASE10_DECISION_MATRIX.md).

---

## What would need live testing to move UNKNOWN → VERIFIED

Listed for completeness. **None of this is authorized and none was performed.**

1. Does `customerSet` write the `customId` metafield when creating? (§5)
2. Does it reject a `customId` with no definition, or degrade silently? (§2)
3. Does `bulkOperationRunMutation` accept customer mutations? (§9)
4. Actual mutation point cost — assumed 10, never measured.

Each requires exactly one write against the development store, and each belongs
to a Tier-3 test plan that has not been approved.
