# Security audit — budtender-pos (2026-06-28)

Multi-agent audit (10 dimensions, each finding adversarially verified). Initial posture:
**CONDITIONAL FAIL**. The high/medium findings below are now **fixed**; remaining items
are tracked. Threat model: internet-facing (`checkout.3dpresence.com`), login-gated,
writes real Dutchie register orders, public source repo.

## Fixed in this pass

| Sev | Issue | Fix |
|----|-------|-----|
| HIGH | **Client-trusted cart line** — `cart_add` built the line from POST `UnitPrice/SerialNo/ProductId/BatchId` and forwarded it to the live register write (`RunAutoPrice:False`), so a logged-in user could ring $0.01 / forge a serial. | `cart_add` now **re-resolves every line server-side** from the cached inventory by `ProductId` (`catalog.find_item`); only `Cnt` comes from the client (clamped 1–99); unknown product → rejected. The product card no longer even posts a price. `views.py` + `catalog.py`. Tests: `test_cart_add_uses_server_price_not_client`, `_rejects_unknown_product`, `_clamps_qty`. |
| HIGH | **Customer-PII enumeration (IDOR)** — `profile?phone=ANY` returned any customer's profile from the happytime DB, unthrottled. | `profile` is now **POST + `@rate_limit` + anchored to a session allow-map** populated only by a prior lookup/scan; the phone/name are taken from the server-side map, never the request. Tests: `test_profile_post_anchored_to_session`, `_rejects_unlisted_acct_idor`. |
| MED | **State-mutating GET** — `profile` (GET) rewrote `session.acct_id` + did a DB write → CSRF retarget via a link. | `profile` is now **POST-only** (CSRF-protected); the Select control is `hx-post`. |
| MED | **No CSP; htmx from CDN without SRI.** | **Self-hosted htmx** + an external `app.js`; added a **`Content-Security-Policy`** middleware (`script-src 'self'`). `core/security.py`. |
| MED | **Store client-selectable** (a budtender could write to another store). | `BUDTENDER_LOCK_STORE` env pins a deployment to one store and ignores any client-supplied store. |
| MED | **Info disclosure** — raw exceptions (`f"...{exc}"`) rendered to users (could leak DSN/host/URLs). | All user-facing errors are now **generic**; details go to the log only. |
| MED | **Weak password policy** (length-only). | Full validator set + min length 10 (`AUTH_PASSWORD_VALIDATORS`). |
| LOW | **Rate-limit bypass via spoofed `X-Forwarded-For`.** | Use the **last** XFF hop (Traefik-appended), not the client-controlled first. `core/ratelimit.py`. |
| LOW | CSRF cookie not HttpOnly / no SameSite. | `CSRF_COOKIE_HTTPONLY=True` (token injected via template, not JS) + `SameSite=Lax` on both cookies. |
| LOW | Unbounded cart quantity. | `Cnt` clamped 1–99. |

Login brute-force is throttled (`@rate_limit("login", 10/300)`); every Dutchie write is
audited (`DutchieWriteAudit`); uploads validated (`core/uploads.validate_image_upload`);
secrets `enc:v1:`-decrypted at read and masked in `Store.__repr__`; `manage.py check
--deploy` is clean (SSL redirect, HSTS+preload, secure cookies).

## Tracked / recommendations (not blocking, do next)
- **Per-user store RBAC** — only the env lock is in; a `BudtenderProfile(user→store)` is the full fix.
- **Rotate the Dutchie password + 3 API keys** (they appeared in a support chat; repo is public) and consider making the repo private.
- **Admin site** (`/admin`) is staff-only but internet-exposed — IP-allowlist or remove in prod.
- **Redis** has no password (reachable only on the internal docker network) — add `requirepass` if ever exposed.
- **Dutchie price enforcement** ultimately depends on Dutchie honoring `RunAutoPrice:False`; our server-side re-resolution removes the client vector regardless.
- **ID-scan 21+ gate** trusts OCR output — a forged ID image could pass; budtender visual confirmation remains the backstop.
- Add dependency-CVE monitoring (Dependabot) on the repo.

Full machine-readable findings: the audit workflow result (run `wf_f7631b6f-abd`).
