"""Customer upsert + Dutchie-write audit helpers."""

import re

from .models import Customer, DutchieWriteAudit

_DOB_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b")

# Scan keys we copy into the Customer record (only fills blanks).
_SCAN_FIELDS = (
    "first_name", "last_name", "phone", "mjstateidno",
    "address", "city", "state", "postal_code", "email",
)


def upsert_customer(scan: dict, dutchie_acct_id=None) -> Customer:
    """Get-or-create a Customer by acct_id (preferred) or phone, filling blanks
    from the scan. `scan` is the raw OCR/lookup dict; stored verbatim in raw_scan.
    """
    scan = scan or {}
    phone = (scan.get("phone") or "").strip()

    obj = None
    if dutchie_acct_id is not None:
        obj = Customer.objects.filter(dutchie_acct_id=dutchie_acct_id).first()
    if obj is None and phone:
        obj = Customer.objects.filter(phone=phone).first()
    if obj is None:
        obj = Customer()

    if dutchie_acct_id is not None:
        obj.dutchie_acct_id = dutchie_acct_id

    # Fill only blank string fields from the scan (don't clobber known data).
    for field in _SCAN_FIELDS:
        val = (scan.get(field) or "").strip()
        if val and not getattr(obj, field, ""):
            setattr(obj, field, val)

    if scan.get("birth_date") and obj.birth_date is None:
        obj.birth_date = scan["birth_date"]
    if scan.get("over_21") is not None and obj.over_21 is None:
        obj.over_21 = bool(scan["over_21"])

    if scan:
        obj.raw_scan = scan
    obj.save()
    return obj


def record_write(store, action, ok, acct_id=None, shipment_id=None, summary="", username="") -> DutchieWriteAudit:
    """Append an immutable Dutchie-write audit row.

    `summary` must be PII-free; we truncate to 500 and strip obvious DOB-like
    tokens (YYYY-MM-DD / MM/DD/YYYY) defensively.
    """
    summary = _scrub(summary)[:500]
    return DutchieWriteAudit.objects.create(
        store=(store or "")[:120],
        action=(action or "")[:40],
        acct_id=acct_id,
        shipment_id=shipment_id,
        summary=summary,
        ok=bool(ok),
        username=(username or "")[:150],
    )


def _scrub(text: str) -> str:
    return _DOB_RE.sub("[redacted]", text or "")
