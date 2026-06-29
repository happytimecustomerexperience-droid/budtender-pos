"""Session-activity tracking — visit lifecycle, event log, degrade-safety, no-PII,
dedupe, the operator dashboard, and the purge command.

Tracking must NEVER break a page: the degrade tests assert a thrown error is swallowed.
"""

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from budtender import views as V
from customers import tracking
from customers.models import ShopEvent, ShopVisit
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


def _use_store(monkeypatch, store=STORE):
    monkeypatch.setattr(V, "load_stores", lambda: {store.name: store} if store else {})


class _User:
    username = "bud"


def _req():
    """A bare request whose session is a plain dict — all tracking touches is get/set/pop."""
    r = RequestFactory().get("/")
    r.session = {}
    r.user = _User()
    return r


# ── unit: visit lifecycle + event log ─────────────────────────────────────────
def test_full_lifecycle_records_ordered_events_and_rollups():
    r = _req()
    r.session["store"] = "yakima"
    v = tracking.start_visit(r, acct_id=770001, name="Jane", phone="509", how="lookup")
    assert v.outcome == "open" and r.session["visit_id"] == v.id

    tracking.track(r, "search", detail="gummies", results=5)
    prod = {"product_id": "5001", "name": "Blue Dream"}
    tracking.track(r, "product_view", product=prod, dedupe_key="5001")
    tracking.track(r, "product_view", product=prod, dedupe_key="5001")          # duplicate
    tracking.track(r, "suggestions_shown", dedupe_key="a,b", ids=["a", "b"])
    tracking.track(r, "item_add", product=prod, price=40, qty=2)
    tracking.track(r, "checkout", detail="1 items", total=40)
    tracking.end_visit(r, "checked_out", shipment_id=999, cart_total=40)

    v.refresh_from_db()
    assert v.outcome == "checked_out" and v.ended_at is not None
    assert v.order_shipment_id == 999 and float(v.cart_total) == 40.0
    kinds = list(v.events.values_list("kind", flat=True))
    assert kinds == ["visit_start", "search", "product_view",
                     "suggestions_shown", "item_add", "checkout"]   # deduped product_view
    assert v.items_viewed == 1 and v.items_added == 1
    assert "visit_id" not in r.session


def test_switching_customer_closes_prior_visit():
    r = _req()
    v1 = tracking.start_visit(r, acct_id=1, name="A")
    v2 = tracking.start_visit(r, acct_id=2, name="B")
    v1.refresh_from_db()
    assert v1.outcome == "abandoned" and v1.ended_at is not None
    assert v2.id != v1.id and r.session["visit_id"] == v2.id


def test_same_customer_reuses_open_visit():
    r = _req()
    v1 = tracking.start_visit(r, acct_id=1, name="A")
    v2 = tracking.start_visit(r, acct_id=1, name="A")
    assert v1.id == v2.id and ShopVisit.objects.count() == 1


def test_track_is_noop_without_open_visit():
    tracking.track(_req(), "product_view", product={"product_id": "1", "name": "x"})
    assert ShopEvent.objects.count() == 0


def test_login_event_is_standalone():
    tracking.track(_req(), "login")
    e = ShopEvent.objects.get()
    assert e.kind == "login" and e.visit is None and e.budtender == "bud"


def test_no_pii_persisted():
    """The scan 21+ flag is fine to keep; DOB / ID# must never land in an event."""
    r = _req()
    tracking.start_visit(r, acct_id=1, name="A", phone="509", how="scan", scan_over21=True)
    blob = " ".join(f"{e.meta} {e.detail} {e.product_name}" for e in ShopEvent.objects.all()).lower()
    assert "dob" not in blob and "birth" not in blob and "mjstateid" not in blob


# ── degrade safety: a failure inside tracking never reaches the request ────────
def test_start_visit_degrades(monkeypatch):
    def boom(**k):
        raise RuntimeError("db down")
    monkeypatch.setattr(ShopVisit.objects, "create", boom)
    assert tracking.start_visit(_req(), acct_id=1) is None     # swallowed, returns None


def test_track_degrades(monkeypatch):
    r = _req()
    tracking.start_visit(r, acct_id=1)
    monkeypatch.setattr(tracking, "_log", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    tracking.track(r, "search", detail="q")                    # must not raise


def test_start_visit_clears_taste_even_on_post_create_failure(monkeypatch):
    """A prior shopper's taste must never leak into the next customer, even if start_visit
    fails AFTER creating the visit row."""
    r = _req()
    r.session["taste"] = {"category": {"Flower": 9}}           # previous customer's taste
    monkeypatch.setattr(tracking, "_log", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert tracking.start_visit(r, acct_id=2) is None          # _log raises post-create
    assert "taste" not in r.session                            # cleared before create


# ── integration: views wire the hooks ─────────────────────────────────────────
def test_end_session_abandons_open_visit(auth, monkeypatch):
    _use_store(monkeypatch)
    v = ShopVisit.objects.create(store="yakima", budtender="bud", acct_id=1, acct_name="A")
    s = auth.session
    s["visit_id"] = v.id
    s["acct_id"] = 1
    s.save()
    auth.get(reverse("end"), SERVER_NAME="localhost")
    v.refresh_from_db()
    assert v.outcome == "abandoned" and v.ended_at is not None


def test_cart_add_logs_item_add(auth, monkeypatch):
    _use_store(monkeypatch)
    v = ShopVisit.objects.create(store="yakima", budtender="bud", acct_id=1, acct_name="A")
    s = auth.session
    s["visit_id"] = v.id
    s["cart"] = []
    s.save()
    row = {"ProductId": 1, "BatchId": 2, "SerialNo": "S1", "UnitPrice": 25.0,
           "RecUnitPrice": 25.0, "ProductDesc": "Real Product", "CannbisProduct": "Yes"}
    monkeypatch.setattr(V.catalog, "find_item", lambda store, product_id=None, serial=None: dict(row))
    monkeypatch.setattr(V, "_client", lambda store: type("C", (), {"price_check": lambda self, x: {}})())
    auth.post(reverse("cart_add"), {"ProductId": "1", "Cnt": "2"}, SERVER_NAME="localhost")
    e = ShopEvent.objects.get(kind="item_add")
    assert e.product_name == "Real Product" and e.meta.get("qty") == 2


def test_dashboard_pages_render(auth, monkeypatch):
    _use_store(monkeypatch)
    v = ShopVisit.objects.create(store="yakima", budtender="bud", acct_id=1, acct_name="Jane",
                                 outcome="checked_out", ended_at=timezone.now())
    ShopEvent.objects.create(visit=v, kind="product_view", product_name="OG Kush")
    assert auth.get(reverse("sessions"), SERVER_NAME="localhost").status_code == 200
    assert auth.get(reverse("sessions_active"), SERVER_NAME="localhost").status_code == 200
    rr = auth.get(reverse("sessions_rollups"), SERVER_NAME="localhost")
    assert rr.status_code == 200 and b"Per budtender" in rr.content
    dr = auth.get(reverse("session_detail", args=[v.id]), SERVER_NAME="localhost")
    assert dr.status_code == 200 and b"OG Kush" in dr.content and b"Viewed product" in dr.content


def test_purge_visits_deletes_only_old(monkeypatch):
    old = ShopVisit.objects.create(store="yakima")
    ShopVisit.objects.filter(id=old.id).update(
        started_at=timezone.now() - timezone.timedelta(days=400))
    new = ShopVisit.objects.create(store="yakima")
    call_command("purge_visits", days=365)
    assert not ShopVisit.objects.filter(id=old.id).exists()
    assert ShopVisit.objects.filter(id=new.id).exists()
