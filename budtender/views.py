"""Budtender screen views — scan/lookup/profile/inventory/cart/submit + auth.

Function views, server-rendered + HTMX partials. Write paths are @login_required.
Dutchie calls are wrapped so missing creds/endpoints degrade to a visible error, never
a 500. Cart lives in the session. Public-ish endpoints are throttled. Lists paginate.
"""

from __future__ import annotations

import logging

from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from core.ratelimit import rate_limit
from customers.intelligence import load_customer_history, load_profile_full
from customers.services import record_write, upsert_customer
from dutchie.pos_register_client import PosRegisterClient
from dutchie.stores import load_stores

from . import catalog

logger = logging.getLogger(__name__)

MAX_LIST = 40  # cap any rendered list (pagination ceiling)
MENU_PAGE = 60  # products rendered per menu view


# ── helpers ──────────────────────────────────────────────────────────────────
def _stores():
    try:
        return load_stores()
    except Exception as exc:
        logger.warning("load_stores failed: %s", exc)
        return {}


def _active_store(request):
    stores = _stores()
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


# ── screen ───────────────────────────────────────────────────────────────────
@login_required
def screen(request):
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
            ctx["warn"] = f"guest lookup unavailable: {exc}"
    upsert_customer(scan_result, dutchie_acct_id=acct_id)
    if acct_id:
        request.session["acct_id"] = acct_id
        request.session["acct_name"] = scan_result.get("accts_name")
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
        ctx["guests"] = _parse_guests(_client(store).guest_search(q))
    except Exception as exc:
        ctx["error"] = f"lookup failed: {exc}"
    return render(request, "budtender/_guests.html", ctx)


@login_required
@require_http_methods(["GET"])
def profile(request):
    acct = request.GET.get("acct")
    name = request.GET.get("name")
    phone = request.GET.get("phone") or ""
    request.session["acct_id"] = acct
    request.session["acct_name"] = name
    request.session["acct_phone"] = phone
    if acct:  # persist the acct<->name mapping locally for future fast lookup
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
        "thc_min": _int("thc_min"),
        "in_stock": g.get("in_stock") == "1", "doh_only": g.get("doh_only") == "1",
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
        ctx["error"] = f"menu load failed: {exc}"
        return render(request, "budtender/_menu.html", ctx)
    results = catalog.query(items, profile, f)
    ctx.update(
        products=results[:MENU_PAGE], total=len(results),
        cats=catalog.categories(items), facets=catalog.facets(items), f=f,
        has_customer=bool(profile), acct_name=request.session.get("acct_name"),
        suggestions=catalog.suggestions(store.name, profile, 6) if profile else [],
    )
    return render(request, "budtender/_menu.html", ctx)


@login_required
@require_http_methods(["POST"])
def cart_add(request):
    cart = request.session.get("cart", [])
    item = {k: request.POST.get(k) for k in (
        "ProductId", "BatchId", "SerialNo", "RecUnitPrice", "UnitPrice",
        "ProductDesc", "CannbisProduct")}
    for k in ("ProductId", "BatchId"):
        item[k] = int(item[k]) if item.get(k) else None
    for k in ("RecUnitPrice", "UnitPrice"):
        item[k] = float(item[k]) if item.get(k) else 0
    try:
        item["Cnt"] = max(1, int(request.POST.get("Cnt") or 1))
    except ValueError:
        item["Cnt"] = 1
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
        request.session["cart"] = []
        ctx["result"] = result
    except Exception as exc:
        record_write(store.name, "submit", ok=False,
                     acct_id=int(acct_id) if str(acct_id).isdigit() else None,
                     summary=str(exc)[:200], username=getattr(request.user, "username", ""))
        ctx["error"] = f"submit failed: {exc}"
    return render(request, "budtender/_submit_result.html", ctx)
