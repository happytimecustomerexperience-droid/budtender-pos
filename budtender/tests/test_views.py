"""View tests — auth gate, graceful degradation, cart flow, cache, submit orchestration.

Dutchie network is never hit: store/client are monkeypatched. Run with pytest-django.
"""

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from budtender import views as V
from customers.models import DutchieWriteAudit
from dutchie.session import Store

pytestmark = pytest.mark.django_db

STORE = Store(name="yakima", base_url="https://bo", pos_base_url="https://pos",
              org_id=700002, lsp_id=700045, loc_id=700498, register_id=700318,
              username="u", password="p", api_key="k")


@pytest.fixture
def user(db):
    return User.objects.create_user("bud", password="pw12345!")


@pytest.fixture
def auth(client, user):
    client.force_login(user)
    return client


class FakeClient:
    def __init__(self, **resp):
        self.resp = resp

    def guest_search(self, query=""):
        return self.resp.get("guests", {"Data": []})

    def submit_cart(self, acct_id, items, **kw):
        return {"shipment_id": 999, "schedule_id": 111, "allotment": 2530.1,
                "added": items, "saved": {"Result": True}}


def _use_store(monkeypatch, store=STORE):
    monkeypatch.setattr(V, "load_stores", lambda: {store.name: store} if store else {})


def test_login_required(client):
    r = client.get(reverse("screen"))
    assert r.status_code == 302 and "/login/" in r.url


def test_begin_renders(auth, monkeypatch):
    _use_store(monkeypatch)
    r = auth.get(reverse("begin"), SERVER_NAME="localhost")
    assert r.status_code == 200 and b"Begin a new session" in r.content


def test_screen_requires_session(auth, monkeypatch):
    _use_store(monkeypatch)
    r = auth.get(reverse("screen"), SERVER_NAME="localhost")
    assert r.status_code == 302 and r.url == reverse("begin")  # no customer -> start gate


def test_screen_ok_with_session(auth, monkeypatch):
    _use_store(monkeypatch)
    s = auth.session
    s["acct_id"] = 1
    s["acct_name"] = "Jane"
    s.save()
    r = auth.get(reverse("screen"), SERVER_NAME="localhost")
    assert r.status_code == 200 and b"Budtender POS" in r.content


def test_start_phone_match_sets_session_and_redirects(auth, monkeypatch):
    _use_store(monkeypatch)
    monkeypatch.setattr(V, "_client", lambda s: FakeClient(guests={"Data": [
        {"Guest_id": 710000001, "Name": "Jane Doe", "PhoneNo": "5095550100"}]}))
    r = auth.post(reverse("start"), {"phone": "5095550100", "store": "yakima"}, SERVER_NAME="localhost")
    assert r.status_code == 302 and r.url == reverse("screen")
    assert auth.session["acct_id"] == 710000001 and auth.session["acct_phone"] == "5095550100"


def test_start_under21_blocks(auth, monkeypatch):
    from django.core.files.uploadedfile import SimpleUploadedFile
    _use_store(monkeypatch)
    import core.uploads as up
    import idscan.pipeline as ip
    monkeypatch.setattr(up, "collect_id_images", lambda files: [b"img"])
    monkeypatch.setattr(ip, "run_id_scan", lambda imgs: {"over_21": False, "accts_name": "Kid", "age": 18})
    img = SimpleUploadedFile("id.jpg", b"img", content_type="image/jpeg")
    r = auth.post(reverse("start"), {"store": "yakima", "images": img}, SERVER_NAME="localhost")
    assert r.status_code == 200 and b"UNDER 21" in r.content
    assert "acct_id" not in auth.session  # no session started


def test_start_needs_input(auth, monkeypatch):
    _use_store(monkeypatch)
    r = auth.post(reverse("start"), {"store": "yakima"}, SERVER_NAME="localhost")
    assert r.status_code == 200 and b"Scan an ID or enter a phone" in r.content


def test_profile_post_anchored_to_session(auth, monkeypatch):
    _use_store(monkeypatch)
    s = auth.session
    s["guests"] = {"710000001": {"name": "Jane Doe", "phone": "5095550100"}}
    s.save()
    r = auth.post(reverse("profile"), {"acct": "710000001"}, SERVER_NAME="localhost")
    assert r.status_code == 200 and r["HX-Trigger"] == "customerChanged"
    assert auth.session["acct_id"] == "710000001"


def test_profile_rejects_unlisted_acct_idor(auth, monkeypatch):
    _use_store(monkeypatch)
    # No prior lookup -> acct not in the session allow-map -> refused (IDOR guard).
    r = auth.post(reverse("profile"), {"acct": "99999999"}, SERVER_NAME="localhost")
    assert r.status_code == 200 and b"Select a customer" in r.content
    assert "acct_id" not in auth.session


def test_end_session_clears_and_redirects(auth, monkeypatch):
    _use_store(monkeypatch)
    s = auth.session
    s["acct_id"] = 5
    s["cart"] = [{"x": 1}]
    s.save()
    r = auth.get(reverse("end"), SERVER_NAME="localhost")
    assert r.status_code == 302 and r.url == reverse("begin")
    assert "acct_id" not in auth.session


def test_all_get_pages_no_500(auth, monkeypatch):
    _use_store(monkeypatch)
    monkeypatch.setattr(V.catalog, "get_inventory", lambda s: [])
    for name in ("begin", "menu"):
        assert auth.get(reverse(name), SERVER_NAME="localhost").status_code == 200
    # login page is unauthenticated-friendly
    assert auth.get(reverse("login"), SERVER_NAME="localhost").status_code == 200


def test_resolve_prefers_phone(monkeypatch):
    from budtender.views import _resolve_or_create

    class FC:
        def guest_search(self, query):
            if query == "5095551234":
                return {"Data": [{"Guest_id": 111, "Name": "Phone Match", "PhoneNo": "5095551234"}]}
            return {"Data": [{"Guest_id": 222, "Name": "Name Match"}]}

    acct, name, how = _resolve_or_create(FC(), {"accts_name": "John Name"}, "5095551234")
    assert acct == 111 and how == "phone"


def test_resolve_creates_when_none(monkeypatch):
    from budtender.views import _resolve_or_create

    class FC:
        def guest_search(self, query):
            return {"Data": []}

        def create_guest(self, **kw):
            return 999

    scan = {"accts_name": "New Guy", "first_name": "New", "last_name": "Guy", "birth_date": "1990-01-01"}
    acct, name, how = _resolve_or_create(FC(), scan, "5090000000")
    assert acct == 999 and how == "created"


def test_lookup_degrades_without_store(auth, monkeypatch):
    _use_store(monkeypatch, store=None)
    r = auth.post(reverse("lookup"), {"phone": "509"}, SERVER_NAME="localhost")
    assert r.status_code == 200 and b"no store configured" in r.content


def test_lookup_lists_guests(auth, monkeypatch):
    _use_store(monkeypatch)
    monkeypatch.setattr(V, "_client", lambda s: FakeClient(guests={"Data": [
        {"Guest_id": 710000002, "Name": "Test Customer", "PhoneNo": "5095550100",
         "PatientType": "Recreational", "LastTransaction": "2026-05-23T19:27:38"}]}))
    r = auth.post(reverse("lookup"), {"phone": "5095550100"}, SERVER_NAME="localhost")
    assert r.status_code == 200 and b"Test Customer" in r.content and b"710000002" in r.content


def test_menu_renders_products(auth, monkeypatch):
    _use_store(monkeypatch)
    items = [{
        "product_id": "1", "name": "1UP Cartridge", "brand": "1UP", "category": "Vaporizer",
        "raw_category": "Vaporizer", "cat_key": "vapes", "cat_label": "Vapes",
        "strain": "", "thc": 80, "price": 25.0, "qty": 10,
        "image": "", "img": None, "img_static": True, "price_was": 0, "effects": [],
        "strain_type": "", "terpene": "", "bucket": "", "velocity": 0, "margin_pct": 0,
        "price_z": 0, "subcategory": "", "received_date": None,
        "ProductId": 750000001, "BatchId": 760000001, "SerialNo": "178", "UnitPrice": 25.0,
        "RecUnitPrice": 25.0, "ProductDesc": "1UP Cartridge", "CannbisProduct": "Yes",
    }]
    monkeypatch.setattr(V.catalog, "get_inventory", lambda s: items)
    r = auth.get(reverse("menu"), {"q": "1up"}, SERVER_NAME="localhost")
    assert r.status_code == 200 and b"1UP Cartridge" in r.content


_SERVER_ROW = {"ProductId": 1, "BatchId": 2, "SerialNo": "S1", "UnitPrice": 25.0,
               "RecUnitPrice": 25.0, "ProductDesc": "Real Product", "CannbisProduct": "Yes"}


def test_product_detail_renders(auth, monkeypatch):
    _use_store(monkeypatch)
    s = auth.session
    s["acct_id"] = 1
    s.save()
    p = {"product_id": "750000001", "ProductId": 750000001, "name": "1UP Cartridge",
         "brand": "1UP", "cat_key": "vapes", "cat_label": "Vapes", "strain": "Black Cherry",
         "strain_type": "hybrid", "thc": 80.0, "cbd": 0.0, "total_terpenes": None,
         "terpene": "myrcene", "effects": ["relaxed", "happy"], "flavors": ["cherry"],
         "potency_mg": None, "price": 25.0, "price_was": 0, "qty": 10, "img": None,
         "subcategory": "", "received_date": "2026-06-01T00:00:00", "vendor": "1UP",
         "unit_grams": 1, "velocity": 0, "margin_pct": 0, "bucket": ""}
    monkeypatch.setattr(V.catalog, "find_item", lambda store, product_id=None, serial=None: dict(p))
    monkeypatch.setattr(V.catalog, "get_inventory", lambda s: [dict(p)])
    r = auth.get(reverse("product", args=["750000001"]), SERVER_NAME="localhost")
    body = r.content
    assert r.status_code == 200
    assert b"1UP Cartridge" in body and b"Lab data" in body
    assert b"Myrcene" in body and b"Earthy" in body          # terpene + explanation
    assert b"Relaxed" in body and b"winding down" in body     # effect + explanation
    assert b"Hybrid" in body                                  # strain type


def test_product_detail_requires_session(auth, monkeypatch):
    _use_store(monkeypatch)
    r = auth.get(reverse("product", args=["1"]), SERVER_NAME="localhost")
    assert r.status_code == 302 and r.url == reverse("begin")


def test_cart_add_uses_server_price_not_client(auth, monkeypatch):
    _use_store(monkeypatch)
    monkeypatch.setattr(V.catalog, "find_item", lambda store, product_id=None, serial=None: dict(_SERVER_ROW))
    # Attacker posts UnitPrice=0.01 + a fake serial — must be IGNORED.
    r = auth.post(reverse("cart_add"),
                  {"ProductId": "1", "UnitPrice": "0.01", "SerialNo": "HACK", "Cnt": "3"},
                  SERVER_NAME="localhost")
    assert b"Real Product" in r.content
    item = auth.session["cart"][0]
    assert item["UnitPrice"] == 25.0 and item["SerialNo"] == "S1" and item["Cnt"] == 3  # server-trusted
    r2 = auth.post(reverse("cart_remove"), {"idx": "0"}, SERVER_NAME="localhost")
    assert b"Cart empty" in r2.content


def test_cart_add_rejects_unknown_product(auth, monkeypatch):
    _use_store(monkeypatch)
    monkeypatch.setattr(V.catalog, "find_item", lambda store, product_id=None, serial=None: None)
    r = auth.post(reverse("cart_add"), {"ProductId": "999", "Cnt": "1"}, SERVER_NAME="localhost")
    assert b"unavailable" in r.content and auth.session.get("cart", []) == []


def test_cart_add_clamps_qty(auth, monkeypatch):
    _use_store(monkeypatch)
    monkeypatch.setattr(V.catalog, "find_item", lambda store, product_id=None, serial=None: dict(_SERVER_ROW))
    auth.post(reverse("cart_add"), {"ProductId": "1", "Cnt": "9999"}, SERVER_NAME="localhost")
    assert auth.session["cart"][0]["Cnt"] == 99  # clamped


def test_submit_requires_customer_and_items(auth, monkeypatch):
    _use_store(monkeypatch)
    r = auth.post(reverse("cart_submit"), {}, SERVER_NAME="localhost")
    assert b"need a store" in r.content


def test_submit_happy_path_audits(auth, monkeypatch):
    _use_store(monkeypatch)
    monkeypatch.setattr(V, "_client", lambda s: FakeClient())
    s = auth.session
    s["cart"] = [{"ProductId": 1, "ProductDesc": "X", "UnitPrice": 5, "Cnt": 1}]
    s["acct_id"] = 710000003
    s.save()
    r = auth.post(reverse("cart_submit"), {}, SERVER_NAME="localhost")
    assert r.status_code == 200 and b"Shipment 999" in r.content
    assert r["HX-Redirect"] == reverse("begin")          # auto-return to start
    assert "acct_id" not in auth.session and "cart" not in auth.session  # session cleared
    audit = DutchieWriteAudit.objects.latest("created_at")
    assert audit.ok and audit.shipment_id == 999 and audit.action == "submit"


def test_start_continue_as_guest(auth, monkeypatch):
    _use_store(monkeypatch)

    class GC:
        def create_guest(self, **kw):
            return 47532853

    monkeypatch.setattr(V, "_client", lambda s: GC())
    r = auth.post(reverse("start"), {"guest": "1", "store": "yakima"}, SERVER_NAME="localhost")
    assert r.status_code == 302 and r.url == reverse("screen")
    assert auth.session["acct_id"] == 47532853 and auth.session["acct_name"] == "Guest"
