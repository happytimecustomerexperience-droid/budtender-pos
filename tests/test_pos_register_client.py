"""Ponytail check — register calls post the EXACT shapes confirmed by the live HAR.

Bypasses login by pinning a fake session; captures (path, body) instead of posting.
Run: pytest tests/test_pos_register_client.py
"""

from dutchie.pos_register_client import PosRegisterClient, map_product_row
from dutchie.session import EmployeeSession, Store

STORE = Store(
    name="yakima", base_url="https://bo", pos_base_url="https://pos",
    org_id=8002, lsp_id=1745, loc_id=3498, register_id=8318,
    username="u", password="p", api_key="",
)


def _client(capture, resp=None):
    c = PosRegisterClient(STORE)
    c._pinned = EmployeeSession(cookie_header="ck", session_gid="SID-123", user_id=95602)

    def fake_post(path, body, **kw):
        capture.append((path, body))
        return resp.get(path) if resp and path in resp else {"Result": True, "Data": []}

    c.post = fake_post  # type: ignore[assignment]
    return c


def test_checkin_guest_shape():
    cap = []
    _client(cap).checkin_guest(47531504)
    path, body = cap[0]
    assert path == "/api/v2/guest/checkin_guest"
    assert body["AcctId"] == 47531504 and body["MJStateIDNo"] == "" and body["Register"] == 0
    assert body["RoomId"] == "" and body["SessionId"] == "SID-123"
    assert body["LspId"] == "1745" and body["LocId"] == "3498"
    assert "Register" in body and body["Register"] == 0  # checkin uses Register 0


def test_guest_details_shape():
    cap = []
    _client(cap).guest_details(47531504)
    path, body = cap[0]
    assert path == "/api/v2/guest/details"
    assert body["Guest_id"] == 47531504 and body["SessionId"] == "SID-123"


def test_select_guest_shape():
    cap = []
    _client(cap).select_guest_to_register(47531504, 147717704, 229057824)
    path, body = cap[0]
    assert path == "/api/v2/guest/Select_Guest_To_Register"
    assert body["Guest_id"] == 47531504 and body["RegisterId"] == 8318
    assert body["ScheduleId"] == 147717704 and body["ShipmentId"] == 229057824
    assert "Register" not in body  # select uses RegisterId


def test_product_search_returns_list():
    cap = []
    c = _client(cap, {"/api/v2/product/product_SearchV2": {"Result": True, "Data": [{"ProductId": 1}]}})
    rows = c.product_search()
    path, body = cap[0]
    assert path == "/api/v2/product/product_SearchV2"
    assert body["Register"] == 8318 and rows == [{"ProductId": 1}]


def test_price_check_shape():
    cap = []
    _client(cap).price_check("17892319679541569")
    path, body = cap[0]
    assert path == "/api/v2/inventory/price-check"
    assert body["PackageSerialNumber"] == "17892319679541569"


def test_add_item_shape():
    cap = []
    item = map_product_row({"ProductId": 3498331, "BatchId": 7454015, "SerialNo": "17892319679541569",
                            "ProductDesc": "1UP Cartridge", "UnitPrice": 25, "RecUnitPrice": 25,
                            "CannabisInventory": "Yes"})
    _client(cap).add_item_to_cart(47531504, 229057824, item, avail_oz=2530.1)
    path, body = cap[0]
    assert path == "/api/v2/cart/add_item_to_shopping_cart"
    assert body["AcctId"] == 47531504 and body["ShipmentId"] == 229057824
    assert body["ProductId"] == 3498331 and body["BatchId"] == 7454015
    assert body["SerialNo"] == "17892319679541569" and body["AvailOz"] == 2530.1
    assert body["Cnt"] == 1 and body["QuantityItem"] is True and body["Register"] == 8318
    assert body["LoyaltyAsDiscount"] is True and body["Weight"] == 0


def test_update_status_shape():
    cap = []
    _client(cap).update_transaction_status(229057824, "Ready for pickup")
    path, body = cap[0]
    assert path == "/api/posv3/maintenance/UpdateTransactionStatus"
    assert body["TransId"] == 229057824 and body["TransactionStatus"] == "Ready for pickup"
    assert "Register" not in body


def test_submit_cart_full_flow():
    cap = []
    resp = {
        "/api/v2/guest/checkin_guest": {"Result": True, "Data": [
            {"ShipmentId": 229057824, "ScanResult": "147717704"}]},
        "/api/v2/guest/details": {"Result": True, "Data": {"Allotment": 2530.1}},
    }
    c = _client(cap, resp)
    item = map_product_row({"ProductId": 1, "BatchId": 2, "SerialNo": "3", "UnitPrice": 5})
    res = c.submit_cart(47531504, [item])
    paths = [p for p, _ in cap]
    assert paths == [
        "/api/v2/guest/checkin_guest",
        "/api/v2/guest/details",
        "/api/v2/guest/Select_Guest_To_Register",
        "/api/v2/cart/add_item_to_shopping_cart",
        "/api/posv3/maintenance/UpdateTransactionStatus",
    ]
    assert res["shipment_id"] == 229057824 and res["schedule_id"] == 147717704
    assert res["allotment"] == 2530.1
    # add_item got the allotment as AvailOz
    add_body = dict(cap[3][1])
    assert add_body["AvailOz"] == 2530.1


def test_guest_search_shape():
    cap = []
    _client(cap).guest_search("5094808352")
    path, body = cap[0]
    assert path == "/api/v2/guest/checkin_search_by_string"
    assert body["SearchString"] == "5094808352"
    assert body["SessionId"] == "SID-123" and "Register" not in body


def test_map_product_row():
    row = {"ProductId": 3567668, "BatchId": 7548777, "SerialNo": "214", "UnitPrice": 19,
           "RecUnitPrice": 19, "ProductDescription": "Temple Ball 1g", "CannabisInventory": "Yes",
           "ProductCategory": "DOH Approved Concentrate", "BrandName": "111 RANCH"}
    it = map_product_row(row)
    assert it["ProductId"] == 3567668 and it["BatchId"] == 7548777
    assert it["ProductDesc"] == "Temple Ball 1g" and it["CannbisProduct"] == "Yes"
    assert it["Brand"] == "111 RANCH"
