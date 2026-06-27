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
              org_id=8002, lsp_id=1745, loc_id=3498, register_id=8318,
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

    def find_products(self, query, limit=40):
        return self.resp.get("products", [])

    def submit_cart(self, acct_id, items, **kw):
        return {"shipment_id": 999, "schedule_id": 111, "allotment": 2530.1,
                "added": items, "saved": {"Result": True}}


def _use_store(monkeypatch, store=STORE):
    monkeypatch.setattr(V, "load_stores", lambda: {store.name: store} if store else {})


def test_login_required(client):
    r = client.get(reverse("screen"))
    assert r.status_code == 302 and "/login/" in r.url


def test_screen_ok(auth, monkeypatch):
    _use_store(monkeypatch)
    r = auth.get(reverse("screen"), SERVER_NAME="localhost")
    assert r.status_code == 200
    assert b"Budtender POS" in r.content


def test_lookup_degrades_without_store(auth, monkeypatch):
    _use_store(monkeypatch, store=None)
    r = auth.post(reverse("lookup"), {"phone": "509"}, SERVER_NAME="localhost")
    assert r.status_code == 200 and b"no store configured" in r.content


def test_lookup_lists_guests(auth, monkeypatch):
    _use_store(monkeypatch)
    monkeypatch.setattr(V, "_client", lambda s: FakeClient(guests={"Data": [
        {"Guest_id": 23959577, "Name": "DAKOTA WANGLER", "PhoneNo": "5094808352",
         "PatientType": "Recreational", "LastTransaction": "2026-05-23T19:27:38"}]}))
    r = auth.post(reverse("lookup"), {"phone": "5094808352"}, SERVER_NAME="localhost")
    assert r.status_code == 200 and b"DAKOTA WANGLER" in r.content and b"23959577" in r.content


def test_inventory_live_search(auth, monkeypatch):
    _use_store(monkeypatch)
    monkeypatch.setattr(V, "_client", lambda s: FakeClient(products=[
        {"ProductId": 3498331, "BatchId": 7454015, "SerialNo": "178",
         "ProductDescription": "1UP Cartridge", "UnitPrice": 25, "RecUnitPrice": 25,
         "CannabisInventory": "Yes", "TotalAvailable": 10}]))
    r = auth.get(reverse("inventory"), {"q": "1up"}, SERVER_NAME="localhost")
    assert r.status_code == 200 and b"1UP Cartridge" in r.content and b"live" in r.content


def test_cart_add_remove(auth, monkeypatch):
    _use_store(monkeypatch)
    r = auth.post(reverse("cart_add"),
                  {"ProductId": "1", "ProductDesc": "X", "UnitPrice": "5", "Cnt": "3"},
                  SERVER_NAME="localhost")
    assert b"X" in r.content
    sess = auth.session
    assert sess["cart"][0]["Cnt"] == 3
    r2 = auth.post(reverse("cart_remove"), {"idx": "0"}, SERVER_NAME="localhost")
    assert b"Cart empty" in r2.content


def test_submit_requires_customer_and_items(auth, monkeypatch):
    _use_store(monkeypatch)
    r = auth.post(reverse("cart_submit"), {}, SERVER_NAME="localhost")
    assert b"need a store" in r.content


def test_submit_happy_path_audits(auth, monkeypatch):
    _use_store(monkeypatch)
    monkeypatch.setattr(V, "_client", lambda s: FakeClient())
    s = auth.session
    s["cart"] = [{"ProductId": 1, "ProductDesc": "X", "UnitPrice": 5, "Cnt": 1}]
    s["acct_id"] = 23812947
    s.save()
    r = auth.post(reverse("cart_submit"), {}, SERVER_NAME="localhost")
    assert r.status_code == 200 and b"Shipment 999" in r.content
    assert auth.session["cart"] == []  # cleared
    audit = DutchieWriteAudit.objects.latest("created_at")
    assert audit.ok and audit.shipment_id == 999 and audit.action == "submit"
