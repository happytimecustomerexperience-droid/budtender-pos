"""Local cache of scanned/looked-up guests + an immutable Dutchie WRITE audit."""

from django.db import models


class Customer(models.Model):
    """A local record of a scanned or looked-up guest.

    The dashboard `_log` tables are the source of truth for purchase history;
    this table just caches what the POS flow scanned/resolved so the operator
    doesn't re-scan, and links to the Dutchie account when known.
    """

    dutchie_acct_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    first_name = models.CharField(max_length=120, blank=True)
    last_name = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=40, blank=True, db_index=True)
    birth_date = models.DateField(null=True, blank=True)
    mjstateidno = models.CharField(max_length=120, blank=True)
    over_21 = models.BooleanField(null=True)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=120, blank=True)
    state = models.CharField(max_length=40, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    email = models.CharField(max_length=255, blank=True)
    raw_scan = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["phone"]),
            models.Index(fields=["dutchie_acct_id"]),
        ]

    def __str__(self):
        name = f"{self.first_name} {self.last_name}".strip() or "(no name)"
        return f"{name} ({self.dutchie_acct_id})"


class DutchieWriteAudit(models.Model):
    """Immutable log of every Dutchie WRITE (the cart flow moves real inventory)."""

    store = models.CharField(max_length=120)
    action = models.CharField(max_length=40)  # checkin/select/add/status/submit
    acct_id = models.BigIntegerField(null=True, blank=True)
    shipment_id = models.BigIntegerField(null=True, blank=True)
    summary = models.CharField(max_length=500)  # NO PII, no raw creds
    ok = models.BooleanField()
    username = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} {self.store} ok={self.ok} @ {self.created_at:%Y-%m-%d %H:%M}"
