"""Customer-360 history loader.

# ponytail: reads the marketing-dashboard's already-ingested Postgres `_log`
# data READ-ONLY via the optional `dashboard` DB alias. We never re-ingest or
# write. If the alias/schema is absent the UI degrades to "history unavailable".

The dashboard's `sales_report_log` (SRL) has NO direct Dutchie acct_id — the
reference loaders (apps/dashboard/loaders/kyc.py) match a customer by NAME
(LOWER(TRIM(customer_name))). We do the same: history is keyed on the guest's
full name. acct_id/phone are accepted for signature parity but the SRL join is
by name. `net_sales` is PRE-TAX; returns are signed-negative rows that net into
the sums (we don't COALESCE cost on returns).
"""

import logging
import re

from django.conf import settings
from django.db import connections

logger = logging.getLogger(__name__)

_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _schema():
    """Return a validated tenant schema name, or None if unavailable."""
    if "dashboard" not in settings.DATABASES:
        return None
    schema = (getattr(settings, "DASHBOARD_TENANT_SCHEMA", "") or "").strip()
    if not schema or not _SCHEMA_RE.match(schema):
        return None
    return schema


def _name_from(acct_id, phone, name):
    """Best available human name to match the SRL customer_name on."""
    return (name or "").strip()


def load_customer_history(acct_id=None, phone=None, name=None):
    """Customer-360 from the dashboard `_log` data, matched by NAME.

    Returns a dict (see shape below) or None when the dashboard mirror is
    unavailable OR no name to match on. SRL has no acct_id/phone customer key,
    so when only acct_id/phone is known and `name` is empty we return None
    (caller renders "history unavailable").

    Shape:
        {
          "orders": int,                 # distinct order_id
          "total_spend": float,          # Σ net_sales (pre-tax, returns netted)
          "aov": float,                  # total_spend / orders
          "recency_days": int | None,    # days since last purchase
          "cadence_days": float | None,  # avg days between visit-days
          "top_products": [{"product","revenue","units"}],
          "top_categories": [{"category","revenue"}],
          "purchase_history": [{"date","order_id","total"}],
          "favorite_strains": [str, ...],
          "matched_by": "name",
        }
    """
    schema = _schema()
    if not schema:
        return None
    cust_name = _name_from(acct_id, phone, name)
    if not cust_name:
        return None

    conn = connections["dashboard"]
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET LOCAL search_path TO "{schema}"')
            ref = cust_name
            params = [ref, ref]
            name_clause = (
                "(s.customer_name = %s OR LOWER(TRIM(s.customer_name)) = LOWER(TRIM(%s)))"
            )

            # Summary: orders, spend (pre-tax, returns netted), recency.
            cur.execute(
                f"""
                SELECT COUNT(DISTINCT s.order_id)::BIGINT,
                       COALESCE(SUM(s.net_sales), 0),
                       MAX(s.report_date)
                FROM sales_report_log s
                WHERE {name_clause}
                """,
                params,
            )
            row = cur.fetchone() or (0, 0, None)
            orders = int(row[0] or 0)
            total_spend = round(float(row[1] or 0), 2)
            last_date = row[2]
            if orders == 0:
                return None

            cur.execute("SELECT CURRENT_DATE")
            today = cur.fetchone()[0]
            recency_days = (today - last_date).days if last_date else None
            aov = round(total_spend / orders, 2) if orders else 0.0

            # Cadence: avg gap between distinct purchase days.
            cur.execute(
                f"""
                WITH days AS (
                    SELECT DISTINCT s.report_date AS d
                    FROM sales_report_log s
                    WHERE {name_clause}
                )
                SELECT COUNT(*)::BIGINT, MIN(d), MAX(d) FROM days
                """,
                params,
            )
            drow = cur.fetchone() or (0, None, None)
            n_days = int(drow[0] or 0)
            cadence_days = None
            if n_days > 1 and drow[1] and drow[2]:
                span = (drow[2] - drow[1]).days
                cadence_days = round(span / (n_days - 1), 1) if span else 0.0

            # Top products (revenue + signed units).
            cur.execute(
                f"""
                SELECT COALESCE(TRIM(s.product_name), 'Unknown') AS product,
                       COALESCE(SUM(s.net_sales), 0) AS revenue,
                       COALESCE(SUM(s.total_inventory_sold), 0) AS units
                FROM sales_report_log s
                WHERE {name_clause}
                GROUP BY COALESCE(TRIM(s.product_name), 'Unknown')
                ORDER BY revenue DESC
                LIMIT 10
                """,
                params,
            )
            top_products = [
                {"product": r[0], "revenue": round(float(r[1] or 0), 2), "units": int(r[2] or 0)}
                for r in cur.fetchall()
            ]

            # Top categories.
            cur.execute(
                f"""
                SELECT COALESCE(NULLIF(TRIM(s.category), ''), 'Unknown') AS category,
                       COALESCE(SUM(s.net_sales), 0) AS revenue
                FROM sales_report_log s
                WHERE {name_clause}
                GROUP BY COALESCE(NULLIF(TRIM(s.category), ''), 'Unknown')
                ORDER BY revenue DESC
                LIMIT 10
                """,
                params,
            )
            top_categories = [
                {"category": r[0], "revenue": round(float(r[1] or 0), 2)} for r in cur.fetchall()
            ]

            # Purchase history (per order_id).
            cur.execute(
                f"""
                SELECT MAX(s.report_date)::TEXT AS date,
                       s.order_id::TEXT AS order_id,
                       COALESCE(SUM(s.net_sales), 0) AS total
                FROM sales_report_log s
                WHERE {name_clause}
                GROUP BY s.order_id
                ORDER BY date DESC
                LIMIT 50
                """,
                params,
            )
            purchase_history = [
                {"date": r[0], "order_id": r[1], "total": round(float(r[2] or 0), 2)}
                for r in cur.fetchall()
            ]

            return {
                "orders": orders,
                "total_spend": total_spend,
                "aov": aov,
                "recency_days": recency_days,
                "cadence_days": cadence_days,
                "top_products": top_products,
                "top_categories": top_categories,
                "purchase_history": purchase_history,
                "favorite_strains": _favorite_strains(top_products),
                "matched_by": "name",
            }
    except Exception:
        logger.debug("load_customer_history failed", exc_info=True)
        return None


def _favorite_strains(top_products):
    """Cheap heuristic: the top product names double as favorite strains.

    SRL has no dedicated strain column; the product name is the closest signal.
    """
    return [p["product"] for p in (top_products or [])[:5]]
