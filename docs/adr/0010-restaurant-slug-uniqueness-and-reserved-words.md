# 0010 — Restaurant slug uniqueness and reserved-word policy

- **Status:** Accepted
- **Date:** 2026-05-07

> Published copy, lightly edited for an outside reader: file and line references are summarized, links to other ADRs are plain text, and domains are placeholders. The decision and its reasoning are unchanged. Note: a later ADR (0016) moved published menus from a path (`/m/<slug>`) to a per-restaurant subdomain (`<slug>.<menu-domain>`). The uniqueness and reserved-word rules below carried over unchanged, and matter more there, because a slug is now a DNS label.

## Context

Every restaurant has a URL-safe identifier — the **slug** — that anchors its published-menu URL on the shared hosting site (`/m/<slug>`, per ADR-0006). Today the slug is generated from the restaurant name client-side at sign-up by a shared helper, validated for shape only by a shared schema, and written into `restaurants/{id}.slug` by the web app's create function. There is **no uniqueness check anywhere**: two restaurants with names that slugify to the same value will both land in Firestore, breaking path-based routing and the architectural guarantee in ADR-0006 that "a customer's content appears only under their assigned path."

A few constraints shape the answer:

- **Firestore has no native unique-on-field constraint.** Uniqueness is a pattern, not a feature. The standard pattern is a parallel collection (one doc per claimed slug) read-modified-written in a transaction.
- **Slug uniqueness is a security/correctness invariant**, not just a UX nicety. A duplicated slug creates a routing ambiguity that an unlucky or hostile tenant can exploit to surface their content under another restaurant's URL.
- **The slug is also a reserved-word problem.** Slugs that match existing or anticipated routes (`admin`, `api`, `sign-in`, `m`, …) corrupt navigation if path scoping ever changes, and surface confusing URLs (`/m/admin`) even when path scoping holds.
- **The slug is the restaurant's public identity.** Auto-suffixing collisions to `joes-diner-2` is technically harmless but reads like "second location" — confusing for a single-location brand.
- **Sign-up is currently a pure client-side flow.** The trial-seed call already hits the API service, so adding one more server hop on the sign-up critical path is incremental, not architectural.

## Decision

### Slug source and shape

- Slugs are **derived from the restaurant name** at sign-up by the existing `generateSlug()`. Owners can edit the slug from settings later (separate ADR/issue when that lands).
- Shape stays as today: lowercase a–z, 0–9, hyphens; no leading/trailing hyphen; max 60 characters. **Min length is 2** — one-character slugs are nearly always reserved-word collisions or accidental; two-character slugs are rare but plausible (e.g., a restaurant actually named "AZ" or "OX").
- If `generateSlug(name)` produces an empty string (e.g., emoji-only name), sign-up rejects with: *"We couldn't make a web address from that name. Please use letters or numbers."*

### Uniqueness enforcement

- Slug uniqueness is enforced by a **parallel `restaurant_slugs/{slug}` collection**, where the document ID is the slug and the body is `{ restaurantId, ownerId, claimedAt }`.
- A new restaurant is created in a single **server-side Firestore transaction** in the API service (`POST /restaurants`):
  1. Reject if the slug appears in the reserved-word list — return `409 slug-reserved`.
  2. Read `restaurant_slugs/{slug}` — if it exists, return `409 slug-taken`.
  3. Atomically write `restaurants/{restaurantId}`, the owner's `members/{uid}` subdoc, the `users/{uid}.restaurantId` pointer, and `restaurant_slugs/{slug}`.
- The sign-up page calls this endpoint **after** Firebase Auth account creation, replacing the current direct-from-client write. Firestore security rules block all client writes to `restaurant_slugs/*` and to creates on `restaurants/*`, so the API server is the only authoritative write path.
- Renames (when implemented) follow the same transaction shape: claim new, release old, trigger a rebuild for the URL change. Out of scope here; flagged as future work.

### Collision UX

- On `409 slug-taken` or `409 slug-reserved`, the sign-up form surfaces an inline error and **suggests a derived alternative** (e.g., `joes-diner-chicago`, `joes-diner-uptown`, ultimately `joes-diner-<random4>`) the user can accept with one click or edit before retrying.
- We do **not** silently auto-suffix. The user always sees and confirms their URL before commit.
- A best-effort availability pre-check (a read of `restaurant_slugs/{slug}`) gives real-time feedback as the user types the restaurant name, but is advisory only — the server transaction is the sole guarantee.

### Reserved-word list

- Single source of truth: one module in the shared TypeScript package, exported and imported by both the web client (advisory UX validation) and the API server (authoritative check).
- Categories included in v1:
  - **App routes (current and reserved):** `admin`, `api`, `app`, `auth`, `sign-in`, `sign-up`, `onboarding`, `settings`, `billing`, `account`, `dashboard`, `coming-soon`, `m`, `menu`, `health`, `status`.
  - **Marketing routes (anticipated):** `home`, `index`, `about`, `pricing`, `blog`, `docs`, `help`, `support`, `terms`, `privacy`, `legal`, `contact`.
  - **Brand:** the product and company names and their obvious variants.
  - **Web infrastructure conventions:** `www`, `mail`, `static`, `assets`, `cdn`, `public`, `private`, `internal`, `robots`, `sitemap`, `favicon`.
  - **Generic placeholders:** `new`, `restaurant`, `restaurants`, `test`, `example`.
- The list is intentionally a **denylist**, not an allowlist; updates ship as one-line PRs to the shared package. Profanity and impersonation filtering is out of scope for v1 (note as future work).

## Open questions resolved

Four questions were flagged during review:

1. **Server-side endpoint vs Firestore-rules-only enforcement.** Confirmed: server-side endpoint. Rules-only would be faster to ship but the rule logic ("write `restaurants/X` only if `restaurant_slugs/<slug>` already points at you") is hard to reason about and test, and runs against ADR-0008's direction (move authoritative writes to the FastAPI service). The endpoint is small, well-scoped, and we'd want it eventually anyway.

2. **Reject-with-suggestion vs silent auto-suffix.** Confirmed: reject with an explicit suggestion the user must accept. Silent suffixing avoids friction at sign-up but produces a URL the owner didn't choose; this becomes a support burden when they notice it later. Showing the proposed URL and requiring one-click confirmation preserves low friction while keeping the owner in control.

3. **Reserved-word list scope — `food`/`eat`/`order` too aggressive.** Resolved: removed from the list. Those words are legitimate restaurant name fragments (e.g., "Good Eat Co.") and colliding slugs would still be caught by the parallel-collection uniqueness check. Reserved for actual app routes and infrastructure terms only.

4. **Min length 3 cuts off short but legitimate names.** Resolved: min length lowered to **2**. One-character slugs remain banned (reserved-word or accidental), but two-character slugs like `ox` or `az` are plausible real names and should be claimable.

## Alternatives considered

- **Auto-suffix on collision** (`joes-diner-2`, `-3`, …). Rejected as the default. Restaurants are place-based brands; a numeric suffix reads like "second location" and surprises owners when they discover their URL on day two. Acceptable as a one-click suggestion behind an explicit error, not as silent behavior.
- **Random-suffix collision resolution** (`joes-diner-x7q4`). Rejected as the default for the same reason — ugly URLs the user never opted into. Used only as a last-resort suggestion when place-based suggestions don't disambiguate.
- **Client-side transactional claim, with Firestore rules enforcing the invariant.** Tempting because it avoids adding an endpoint. Rejected because the rule logic ("you may write `restaurants/X` only if `restaurant_slugs/<your-slug>` exists and points at you") is fiddly to express and audit, and runs against ADR-0008's direction of moving authoritative writes to FastAPI. `POST /restaurants` is a small, well-scoped endpoint we'd want eventually anyway.
- **Reserved-word allowlist instead of denylist.** Rejected — the universe of legitimate restaurant names is unbounded; an allowlist is unworkable.
- **No reserved-word list, only collision check.** Rejected. The first time someone names their place "Admin Cafe" and we route `/m/admin` to it, an internal deploy goes sideways.
- **Slug as a user-chosen field on the sign-up form** (rather than name-derived). Rejected for v1 — adds friction at the moment the user has the least patience. Owners can rename later via settings.

## Consequences

- The API service gains a `POST /restaurants` endpoint that owns the canonical create path. The current client-side create becomes a thin call into that endpoint; sign-up is split into "Auth user created → API call → routed to onboarding."
- `firestore.rules` gains: deny-all client writes to `restaurant_slugs/*` and creates on `restaurants/*`; authenticated read on `restaurant_slugs/{slug}` for the availability pre-check.
- A new shared module is the single source of truth for the denylist. Both client and server import it; updates ship in one place.
- End-to-end coverage extends beyond the sign-up happy path to include collision, reserved-word match, empty-after-slugify, and the suggestion-accept flow.
- Renames are unblocked architecturally but not implemented here. The transaction shape extends naturally: claim new, release old (or hold as a 301 source for some grace window), rebuild for the URL change.
- `firestore.indexes.json` stays empty for this — `restaurant_slugs/{slug}` is keyed by document ID, so existence checks are O(1) without a custom index.
- The product requirements doc gets a one-line note pointing to this ADR, so it stops being silent on how the public URL is allocated.
- Future work, captured here so it isn't lost:
  - Slug rename flow with grace-period redirect on the released slug.
  - Profanity / impersonation filtering (separate ADR if/when the operational cost becomes worth it).
  - Unicode transliteration (`café` → `cafe`) for non-ASCII restaurant names; today they slugify to `caf` and lose information.
  - Email-verification gate on slug claim, plus soft-delete of inactive trials, to reclaim squatted slugs.
