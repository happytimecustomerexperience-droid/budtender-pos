# Budtender POS

Scan a customer ID (OCR → structured fields) or look them up by phone, see their full
profile + purchase history, browse live inventory, build a cart, and submit it **for that
specific customer** straight into the Dutchie POS register. Django, secure, **no n8n**.

It clones the employee login, retains the session cookie, and replays the register API
calls — the same `{SessionId, LspId, LocId, OrgId, UserId}` + `Register` block the Dutchie
POS UI uses.

## The Dutchie flow it automates

```
guest_search(phone|name)                 -> AcctId
start_visit(acct)                        -> ShipmentId, ScheduleId
POST /api/v2/guest/checkin_guest
POST /api/v2/guest/Select_Guest_To_Register
POST /api/v2/cart/add_item_to_shopping_cart   (one per item; live ids from product_search)
POST /api/posv3/maintenance/UpdateTransactionStatus  -> {"Result":true}  ("Ready for pickup")
```

## Layout

| Path | What |
|---|---|
| `dutchie/` | Vendored client. `transport` (curl_cffi Chrome impersonation → beats Cloudflare), `login` (EmployeeLogin), `session` (cookie cache + 401 re-login + session block), `pos_register_client` (**the 7 calls**), `pos_read` (REST customer/inventory/product), `stores` (config), `secrets` (Fernet). |
| `idscan/` | ID-scan OCR pipeline (Mistral OCR → OpenAI structured extract). `run_id_scan(images) -> {name, dob, over_21, ...}`. |
| `core/uploads.py` | Upload guard (size cap + magic-byte + Pillow verify), 2-image cap. |
| `customers/` | `Customer` cache + immutable `DutchieWriteAudit`; `intelligence.load_customer_history` (read-only over the dashboard's Postgres `_log`); `services` (upsert + audit). |
| `budtender/` | The web screen (views + mobile-first HTMX templates), `InventoryItem` browse cache, budtender login, throttling, `refresh_inventory` command. |
| `scripts/` | `login_smoke.py`, `pos_smoke.py` — live smoke tests (also `manage.py login_smoke`). |

Mobile-first responsive UI (single column on phones, 2-column ≥900px, 44px touch
targets, camera capture). Static served by WhiteNoise; cache via LocMem (set `REDIS_URL`
for multi-worker). Public endpoints are rate-limited; lists paginate (cap 40).

## Setup

```bash
pip install -e ".[dev]"          # Django, curl_cffi, requests, cryptography, Pillow, pytest(-django)
cp stores.example.json stores.json     # fill in creds (password may be plaintext or enc:v1:)
cp .env.example .env                   # set BUDTENDER_SECRET_KEY, MISTRAL_API_KEY, OPEN_AI_KEY, etc.
python manage.py migrate
python manage.py createsuperuser       # budtender login (the write paths are @login_required)
python manage.py runserver             # http://localhost:8000/
```

Customer purchase history is optional: point `DASHBOARD_DB_DSN` + `DASHBOARD_TENANT_SCHEMA`
at the marketing-dashboard Postgres (read-only) to light up the history panel; without it the
panel degrades to "history unavailable".

## Verify

```bash
pytest                                 # 24 tests: register shapes, session/secrets, views (auth/cart/cache/submit), customer svc, idscan
python manage.py collectstatic --noinput
python manage.py refresh_inventory     # fill the browse cache from the REST read key (cron this)
python manage.py login_smoke yakima    # Smoke #1: login + cookie + cross-subdomain probe
python scripts/pos_smoke.py yakima --acct <id> --product <pid> --batch <bid> \
    --serial <sn> --price 1 --avail 1 --desc "SMOKE"   # Smoke #2: real cart on a TEST register, then VOID it
```

## Endpoints — CONFIRMED from a live HAR (2026-06-26)

The whole cart path is now wired to real shapes in `dutchie/pos_register_client.py`:

| Step | Endpoint | Notes |
|---|---|---|
| login | `POST /api/posv3/user/EmployeeLogin` | served on **ash.pos** (the POS host) — register client logs in there directly |
| find guest | `POST /api/v2/guest/checkin_search_by_string` | `{SearchString}` (phone or name) → `Data:[{Guest_id, Name, PhoneNo, PatientType, DOB, LastTransaction}]` |
| check in | `POST /api/v2/guest/checkin_guest` | response `Data[0].ShipmentId` + `ScanResult` (== ScheduleId) — **no separate start-visit call needed** |
| guest detail | `POST /api/v2/guest/details` | `{Guest_id}` → `Allotment` (== `AvailOz`), ShipmentId, ScheduleId |
| select register | `POST /api/v2/guest/Select_Guest_To_Register` | `RegisterId` = store register |
| inventory | `POST /api/v2/product/product_SearchV2` | takes only session+Register; returns the **full live list** (filter locally); each row has ProductId/BatchId/SerialNo/price |
| price check | `POST /api/v2/inventory/price-check` | `{PackageSerialNumber}` |
| add item | `POST /api/v2/cart/add_item_to_shopping_cart` | `AvailOz` = guest Allotment |
| save | `POST /api/posv3/maintenance/UpdateTransactionStatus` | `TransId` = ShipmentId, "Ready for pickup" |

**All endpoints confirmed (2026-06-26).** The whole budtender flow — search by phone → select →
check in → browse → add → save — is wired to real shapes and unit-tested (29 tests). The only
thing left is operational: real credentials in `stores.json`, then run the two live smokes.

Also confirm on first run: the backoffice-login cookie is accepted on `ash.pos.dutchie.com`
(cross-subdomain). If the POS UI uses a distinct login, capture it and add a `pos_login()`.

## Security

- Every Dutchie WRITE is logged to `DutchieWriteAudit` (PII-scrubbed summary).
- Uploads validated (size + magic-byte + Pillow) before the OCR pipeline; capped to 2 images.
- Dutchie passwords support `enc:v1:` at-rest encryption (`dutchie/secrets.py`).
- Write paths are `@login_required`; prod settings enable SSL redirect + secure cookies + HSTS.

## Run in production

```bash
pip install -e ".[prod]"               # adds gunicorn
python manage.py collectstatic --noinput
BUDTENDER_DEBUG=0 BUDTENDER_SECRET_KEY=<64-char> BUDTENDER_ALLOWED_HOSTS=pos.example.com \
  REDIS_URL=redis://… gunicorn -c gunicorn.conf.py budtender_pos.wsgi
# Procfile: web = gunicorn, inventory = refresh_inventory (cron/Task Scheduler)
```
`manage.py check --deploy` is clean with a strong key (SSL redirect, secure cookies, HSTS+preload).
Windows host: use `runserver` for dev (gunicorn is Linux-only) or `waitress`.

## Status — DONE

All phases built, tested, mobile-verified: P0 scaffold · P1 auth · P2 register client (4 captured
+ 3 gap calls) · P3 OCR · P4 customer-360 · **P5 inventory cache** · P6 mobile screen ·
**P7 harden** (budtender login, upload guard, secret encryption, write-audit, throttle,
`check --deploy` clean). 24 tests green, ruff clean. Responsive UI verified at 375px + 1280px.

**Only thing needing you:** one HAR capture to confirm the 3 gap endpoint paths + real creds in
`stores.json`, then run the two live smokes. Everything else is wired and degrades safely until then.
