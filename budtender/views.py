"""Budtender screen views — scan/lookup/profile/inventory/cart/submit + auth.

Function views, server-rendered + HTMX partials. Write paths are @login_required.
Dutchie calls are wrapped so missing creds/endpoints degrade to a visible error, never
a 500. Cart lives in the session. Public-ish endpoints are throttled. Lists paginate.
"""

from __future__ import annotations

import logging
import os

from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from core.ratelimit import rate_limit
from customers.intelligence import load_customer_history, load_profile_full
from customers.services import record_write, upsert_customer
from dutchie.pos_register_client import PosRegisterClient
from dutchie.stores import load_stores

from . import catalog, education

logger = logging.getLogger(__name__)

MAX_LIST = 40  # cap any rendered list (pagination ceiling)
MENU_PAGE = 24  # products per menu page (paginated)


# ── helpers ──────────────────────────────────────────────────────────────────
def _stores():
    try:
        return load_stores()
    except Exception as exc:
        logger.warning("load_stores failed: %s", exc)
        return {}


def _active_store(request):
    stores = _stores()
    # Per-instance lock: set BUDTENDER_LOCK_STORE to pin this deployment to one
    # store and ignore any client-supplied store (store-isolation hardening).
    lock = os.environ.get("BUDTENDER_LOCK_STORE", "").strip()
    if lock and lock in stores:
        request.session["store"] = lock
        return stores[lock]
    name = request.POST.get("store") or request.GET.get("store") or request.session.get("store")
    if name and name in stores:
        request.session["store"] = name
        return stores[name]
    if stores:
        first = next(iter(stores))
        request.session["store"] = first
        return stores[first]
    return None


def _client(store):
    return PosRegisterClient(store)


def _parse_guests(raw) -> list[dict]:
    """checkin_search_by_string Data -> [{acct_id, name, phone, patient_type, last}]."""
    rows = raw.get("Data") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows[:MAX_LIST]:
        if not isinstance(r, dict):
            continue
        acct = r.get("Guest_id") or r.get("AcctId") or r.get("CustomerId") or r.get("Id")
        name = (r.get("Name") or f"{r.get('FirstName','')} {r.get('LastName','')}").strip()
        out.append({"acct_id": acct, "name": name or "(unknown)",
                    "phone": r.get("PhoneNo") or r.get("Phone") or r.get("CellPhone") or "",
                    "patient_type": r.get("PatientType") or "",
                    "last": (r.get("LastTransaction") or "")[:10]})
    return out


# ── auth (budtender-facing login, mobile) ────────────────────────────────────
@rate_limit("login", limit=10, window=300)
def login_view(request):
    if request.method == "POST":
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            auth_login(request, form.get_user())
            return redirect("screen")
    else:
        form = AuthenticationForm(request)
    return render(request, "budtender/login.html", {"form": form})


def logout_view(request):
    auth_logout(request)
    return redirect("login")


# ── begin session (start gate: scan ID + phone -> 21 check -> find/create) ─────
@login_required
def begin(request):
    return render(request, "budtender/begin.html", {
        "stores": list(_stores().keys()), "active": request.session.get("store"),
    })


@login_required
def end_session(request):
    for k in ("acct_id", "acct_name", "acct_phone", "cart"):
        request.session.pop(k, None)
    return redirect("begin")


def _start_session(request, acct_id, name, phone):
    request.session["acct_id"] = acct_id
    request.session["acct_name"] = name
    request.session["acct_phone"] = phone
    request.session["cart"] = []
    allowed = request.session.get("guests") or {}
    allowed[str(acct_id)] = {"name": name or "", "phone": phone or ""}
    request.session["guests"] = allowed
    return redirect("screen")


def _resolve_or_create(client, scan, phone):
    """Look up by PHONE then by NAME (phone wins when both match); create from the
    scan if neither exists. Returns (acct_id, name, how)."""
    name = (scan.get("accts_name") or "").strip()
    if phone:
        g = _parse_guests(client.guest_search(phone))
        if g:
            return g[0]["acct_id"], g[0]["name"], "phone"
    if name:
        g = _parse_guests(client.guest_search(name))
        if g:
            return g[0]["acct_id"], g[0]["name"], "name"
    if scan.get("first_name") and scan.get("birth_date"):
        gid = client.create_guest(
            first_name=scan["first_name"], last_name=scan.get("last_name", ""),
            dob=scan["birth_date"], phone=phone or scan.get("phone", ""),
            email=scan.get("email", ""), mj_state_id=scan.get("mjstateidno", ""))
        if gid:
            disp = scan.get("accts_name") or f"{scan['first_name']} {scan.get('last_name', '')}".strip()
            return gid, disp, "created"
    return None, None, "none"


@login_required
@rate_limit("start", limit=30, window=60)
@require_http_methods(["POST"])
def start(request):
    from core.uploads import collect_id_images

    store = _active_store(request)
    phone = "".join(c for c in (request.POST.get("phone") or "") if c.isdigit())
    ctx = {"stores": list(_stores().keys()), "active": request.session.get("store"), "phone": phone}

    # "Continue as guest" — quick anonymous Dutchie guest, no profile needed.
    if request.POST.get("guest"):
        if not store:
            ctx["error"] = "no store configured"
            return render(request, "budtender/begin.html", ctx)
        try:
            gid = _client(store).create_guest(first_name="Guest", last_name="", dob="", phone="")
        except Exception as exc:
            logger.warning("guest start failed: %s", exc)
            ctx["error"] = "Could not start a guest session — try again."
            return render(request, "budtender/begin.html", ctx)
        if not gid:
            ctx["error"] = "could not start a guest session"
            return render(request, "budtender/begin.html", ctx)
        return _start_session(request, gid, "Guest", "")

    scan = {}
    files = request.FILES.getlist("images")
    if files:
        try:
            images = collect_id_images(files)
        except Exception as exc:
            ctx["error"] = f"upload rejected: {exc}"
            return render(request, "budtender/begin.html", ctx)
        from idscan.pipeline import run_id_scan
        scan = run_id_scan(images)
        if scan.get("error"):
            ctx["error"] = f"scan failed: {scan['error']}"
            return render(request, "budtender/begin.html", ctx)
        ctx["scan"] = scan
        if scan.get("over_21") is False:    # HARD age flag — do not start a session
            ctx["under21"] = True
            return render(request, "budtender/begin.html", ctx)
    if not store:
        ctx["error"] = "no store configured"
        return render(request, "budtender/begin.html", ctx)
    if not (phone or scan):
        ctx["error"] = "Scan an ID or enter a phone number to begin."
        return render(request, "budtender/begin.html", ctx)

    phone = phone or "".join(c for c in (scan.get("phone") or "") if c.isdigit())
    try:
        acct_id, name, how = _resolve_or_create(_client(store), scan, phone)
    except Exception as exc:
        logger.warning("start lookup failed: %s", exc)
        ctx["error"] = "Lookup failed — try again."
        return render(request, "budtender/begin.html", ctx)
    if not acct_id:
        # No match + nothing to create from → offer Create-profile (scan) or Guest.
        ctx["no_account"] = True
        return render(request, "budtender/begin.html", ctx)

    if scan:
        upsert_customer({**scan, "phone": phone}, dutchie_acct_id=acct_id)
    return _start_session(request, acct_id, name, phone)


# ── POS screen (requires an active session) ────────────────────────────────────
@login_required
def screen(request):
    if not request.session.get("acct_id"):
        return redirect("begin")
    return render(request, "budtender/screen.html", {
        "stores": list(_stores().keys()),
        "active": request.session.get("store"),
        "cart": request.session.get("cart", []),
        "acct_id": request.session.get("acct_id"),
        "acct_name": request.session.get("acct_name"),
    })


@login_required
@rate_limit("scan", limit=20, window=60)
@require_http_methods(["POST"])
def scan(request):
    from core.uploads import collect_id_images

    store = _active_store(request)
    ctx = {"store": store}
    try:
        images = collect_id_images(request.FILES.getlist("images"))
    except Exception as exc:
        ctx["error"] = f"upload rejected: {exc}"
        return render(request, "budtender/_profile.html", ctx)

    from idscan.pipeline import run_id_scan

    scan_result = run_id_scan(images)
    if scan_result.get("error"):
        ctx["error"] = f"scan failed: {scan_result['error']}"
        return render(request, "budtender/_profile.html", ctx)

    acct_id = None
    if store:
        try:
            q = scan_result.get("phone") or scan_result.get("accts_name") or ""
            guests = _parse_guests(_client(store).guest_search(q))
            if guests:
                acct_id = guests[0]["acct_id"]
        except Exception as exc:
            logger.warning("scan guest lookup unavailable: %s", exc)
            ctx["warn"] = "Customer lookup unavailable."
    upsert_customer(scan_result, dutchie_acct_id=acct_id)
    if acct_id:
        request.session["acct_id"] = acct_id
        request.session["acct_name"] = scan_result.get("accts_name")
        allowed = request.session.get("guests") or {}
        allowed[str(acct_id)] = {"name": scan_result.get("accts_name", ""),
                                 "phone": scan_result.get("phone", "")}
        request.session["guests"] = allowed
    request.session["acct_phone"] = scan_result.get("phone") or ""
    ctx.update({"scan": scan_result, "acct_id": acct_id,
                "history": load_customer_history(acct_id=acct_id, phone=scan_result.get("phone"),
                                                 name=scan_result.get("accts_name"))})
    resp = render(request, "budtender/_profile.html", ctx)
    resp["HX-Trigger"] = "customerChanged"
    return resp


@login_required
@rate_limit("lookup", limit=40, window=60)
@require_http_methods(["POST"])
def lookup(request):
    store = _active_store(request)
    q = (request.POST.get("phone") or request.POST.get("name") or "").strip()
    ctx = {"store": store, "query": q}
    if not store:
        ctx["error"] = "no store configured (create stores.json)"
        return render(request, "budtender/_guests.html", ctx)
    if not q:
        ctx["guests"] = []
        return render(request, "budtender/_guests.html", ctx)
    try:
        guests = _parse_guests(_client(store).guest_search(q))
    except Exception as exc:
        logger.warning("lookup failed: %s", exc)
        ctx["error"] = "Lookup failed — try again."
        return render(request, "budtender/_guests.html", ctx)
    ctx["guests"] = guests
    # Record which accounts THIS budtender is allowed to open (anchors `profile`
    # so it can't be used to enumerate arbitrary customers' PII).
    allowed = request.session.get("guests") or {}
    for g in guests:
        if g.get("acct_id") is not None:
            allowed[str(g["acct_id"])] = {"name": g.get("name", ""), "phone": g.get("phone", "")}
    request.session["guests"] = allowed
    return render(request, "budtender/_guests.html", ctx)


@login_required
@rate_limit("profile", limit=40, window=60)
@require_http_methods(["POST"])
def profile(request):
    """POST-only (was a state-mutating GET — CSRF retarget). The customer's phone/
    name are taken from the SESSION allow-map populated by a prior lookup/scan, never
    from the request — so this can't be used to pull arbitrary customers' PII (IDOR)."""
    acct = request.POST.get("acct")
    allowed = (request.session.get("guests") or {}).get(str(acct))
    if not allowed:
        return render(request, "budtender/_profile.html",
                      {"error": "Select a customer from a lookup first."})
    name, phone = allowed.get("name", ""), allowed.get("phone", "")
    request.session["acct_id"] = acct
    request.session["acct_name"] = name
    request.session["acct_phone"] = phone
    upsert_customer({"accts_name": name, "phone": phone},
                    dutchie_acct_id=int(acct) if str(acct).isdigit() else None)
    resp = render(request, "budtender/_profile.html", {
        "acct_id": acct, "scan": {"accts_name": name, "phone": phone},
        "history": load_customer_history(acct_id=acct, phone=phone, name=name),
    })
    resp["HX-Trigger"] = "customerChanged"  # re-rank the menu For-You
    return resp


def _filters(request):
    g = request.GET

    def _int(k):
        v = (g.get(k) or "").strip()
        return int(v) if v.lstrip("-").isdigit() else None

    return {
        "q": g.get("q", ""), "cat": g.get("cat", ""), "brand": g.get("brand", ""),
        "strain_type": g.get("strain_type", ""), "effect": g.get("effect", ""),
        "sort": g.get("sort", "foryou"),
        "price_min": _int("price_min"), "price_max": _int("price_max"),
        "thc_min": _int("thc_min"), "doh_only": g.get("doh_only") == "1",
        "page": max(1, _int("page") or 1),
    }


@login_required
@rate_limit("menu", limit=180, window=60)
@require_http_methods(["GET"])
def menu(request):
    store = _active_store(request)
    ctx = {"store": store}
    if not store:
        ctx["error"] = "no store configured"
        return render(request, "budtender/_menu.html", ctx)
    phone = request.session.get("acct_phone") or ""
    profile = load_profile_full(phone) if phone else None
    f = _filters(request)
    try:
        items = catalog.get_inventory(store.name)
    except Exception as exc:
        logger.warning("menu load failed: %s", exc)
        ctx["error"] = "Menu unavailable — refresh in a moment."
        return render(request, "budtender/_menu.html", ctx)
    results = catalog.query(items, profile, f)
    total = len(results)
    pages = max(1, (total + MENU_PAGE - 1) // MENU_PAGE)
    page = min(f["page"], pages)
    start_i = (page - 1) * MENU_PAGE
    ctx.update(
        products=results[start_i:start_i + MENU_PAGE], total=total,
        page=page, pages=pages, has_prev=page > 1, has_next=page < pages,
        cats=catalog.categories(items), facets=catalog.facets(items), f=f,
        has_customer=bool(profile), acct_name=request.session.get("acct_name"),
        suggestions=catalog.suggestions(store.name, profile, 6) if profile else [],
    )
    return render(request, "budtender/_menu.html", ctx)


@login_required
@rate_limit("product", limit=240, window=60)
@require_http_methods(["GET"])
def product(request, product_id):
    """Full product detail page — lab data, terpene + effect explanations (Dutchie/
    happytimeweed style). Reads the trusted cached inventory row by ProductId."""
    if not request.session.get("acct_id"):
        return redirect("begin")
    store = _active_store(request)
    p = catalog.find_item(store.name, product_id=product_id) if store else None
    if not p:
        return render(request, "budtender/product.html",
                      {"missing": True, "acct_name": request.session.get("acct_name")})
    effects = [(e, education.effect_info(e)) for e in (p.get("effects") or [])]
    terp_aroma_effect = education.terpene_info(p.get("terpene"))
    similar = [s for s in catalog.query(catalog.get_inventory(store.name), None,
                                        {"cat": p["cat_key"], "sort": "popular"})
               if str(s.get("product_id")) != str(p.get("product_id"))][:6]
    return render(request, "budtender/product.html", {
        "p": p, "effects": effects, "terp": terp_aroma_effect,
        "strain_blurb": education.strain_type_info(p.get("strain_type")),
        "similar": similar, "cart": request.session.get("cart", []),
        "acct_name": request.session.get("acct_name"),
    })


_TRUSTED_ITEM_KEYS = ("ProductId", "BatchId", "SerialNo", "UnitPrice",
                      "RecUnitPrice", "ProductDesc", "CannbisProduct")


@login_required
@require_http_methods(["POST"])
def cart_add(request):
    """SECURITY: the price/serial/batch are NEVER taken from the client. We re-resolve
    the line from the server's cached inventory by ProductId; only quantity is trusted
    from the request. (Audit finding: client-trusted cart line -> live register write.)"""
    store = _active_store(request)
    cart = request.session.get("cart", [])
    try:
        cnt = max(1, min(99, int(request.POST.get("Cnt") or 1)))
    except (TypeError, ValueError):
        cnt = 1
    p = catalog.find_item(store.name, product_id=request.POST.get("ProductId")) if store else None
    if not p:
        ctx = _cart_ctx(cart)
        ctx["add_error"] = "Item unavailable — refresh the menu."
        return render(request, "budtender/_cart.html", ctx)
    item = {k: p.get(k) for k in _TRUSTED_ITEM_KEYS}
    item["Cnt"] = cnt
    cart.append(item)
    request.session["cart"] = cart
    return render(request, "budtender/_cart.html", _cart_ctx(cart))


def _cart_ctx(cart):
    total = sum(float(it.get("UnitPrice") or 0) * int(it.get("Cnt") or 1) for it in cart)
    return {"cart": cart, "cart_total": total}


@login_required
@require_http_methods(["POST"])
def cart_remove(request):
    idx = int(request.POST.get("idx", -1))
    cart = request.session.get("cart", [])
    if 0 <= idx < len(cart):
        cart.pop(idx)
    request.session["cart"] = cart
    return render(request, "budtender/_cart.html", _cart_ctx(cart))


@login_required
@require_http_methods(["POST"])
def cart_submit(request):
    store = _active_store(request)
    cart = request.session.get("cart", [])
    acct_id = request.session.get("acct_id")
    ctx = {"store": store}
    if not (store and acct_id and cart):
        ctx["error"] = "need a store, a selected customer (AcctId), and at least one item"
        return render(request, "budtender/_submit_result.html", ctx)
    try:
        result = _client(store).submit_cart(int(acct_id), cart)
        record_write(store.name, "submit", ok=True, acct_id=int(acct_id),
                     shipment_id=result["shipment_id"],
                     summary=f"{len(cart)} items -> Ready for pickup",
                     username=getattr(request.user, "username", ""))
        # Checkout done → clear the session and bounce to the start page for the
        # next customer (HX-Redirect makes htmx do a full client-side navigation).
        for k in ("acct_id", "acct_name", "acct_phone", "cart"):
            request.session.pop(k, None)
        ctx["result"] = result
        resp = render(request, "budtender/_submit_result.html", ctx)
        resp["HX-Redirect"] = reverse("begin")
        return resp
    except Exception as exc:
        record_write(store.name, "submit", ok=False,
                     acct_id=int(acct_id) if str(acct_id).isdigit() else None,
                     summary=str(exc)[:200], username=getattr(request.user, "username", ""))
        logger.warning("cart submit failed: %s", exc)
        ctx["error"] = "Submit failed — please try again."
    return render(request, "budtender/_submit_result.html", ctx)
