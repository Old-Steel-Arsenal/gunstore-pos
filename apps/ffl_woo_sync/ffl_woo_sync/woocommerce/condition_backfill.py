"""backfill_condition_attributes — re-push ONLY ``attributes`` to existing Woo products.

Why this exists: machine pushes (doc_events, reconcile) are lean — they carry the
POS-owned keys only (``sync._POS_OWNED_KEYS``) and never ``attributes``, because
attributes are Woo-owned after first publish (the 2026-08-12 incident was
automation overwriting hand-edited content). So fixing a gun's ``is_used`` /
``condition_grade`` in POS never reaches a product that is already on the shop;
its ``pa_condition`` term stays whatever the first push wrote. This is the
explicit, operator-run way to deliver that fix.

What it sends, per product: ``PUT``-equivalent ``{"id": <woo id>, "attributes":
[...]}`` through ``products/batch`` — nothing else. No name, description, images,
price, stock, status or meta. The attribute list is built by the same builders a
full push uses (``mapping.item_attributes_payload`` /
``serial_attributes_payload``), so it is exactly what a manual full push would
have sent.

⚠ ``attributes`` on a PUT is the authoritative list: WooCommerce drops any
attribute missing from it. The builders emit Caliber / Condition / Manufacturer /
Model, so a custom attribute someone added by hand in wp-admin to one of these
products is removed by this run. Use ``dry_run=1`` first.

Retail store only (the dealer store is retired). No DB writes at all — not even
the ``woo_synced_at`` stamp — so a re-run is always safe.

Not wired into ``patches.txt`` and must not be: it writes to the live store.

    bench --site <site> execute \\
        ffl_woo_sync.woocommerce.condition_backfill.backfill_condition_attributes \\
        --kwargs "{'dry_run': 1}"
"""
from __future__ import annotations

import frappe
import requests
from frappe.utils import cint

from ffl_core.firearm import ALL_FIREARM_GROUPS, _is_firearm_row

from ffl_woo_sync.woocommerce import mapping
from ffl_woo_sync.woocommerce.client import WooCommerceClient, WooCommerceError
from ffl_woo_sync.woocommerce.sync import _f, _push_with_retry

SITE = "retail"

# lazy: 25 products per products/batch request (WooCommerce's cap is 100): each
# product save re-assigns terms, so smaller requests stay well inside the PHP
# timeout; raise it if a full run proves too slow.
_BATCH_SIZE = 25
_MAX_DETAILS = 20  # cap on error rows echoed back


def _as_list(value) -> list[str]:
    """A list arg from bench (list) or HTTP (JSON string / comma-separated)."""
    if not value:
        return []
    if isinstance(value, str):
        value = frappe.parse_json(value) if value.lstrip().startswith("[") else value.split(",")
    return [str(v).strip() for v in value if str(v).strip()]


def _firearm_items() -> dict[str, dict]:
    """``{item_code: row}`` for every firearm Item (flag or legacy firearm group)."""
    rows = frappe.get_all(
        "Item",
        or_filters=[["is_firearm", "=", 1], ["item_group", "in", list(ALL_FIREARM_GROUPS)]],
        fields=["name", "is_firearm", "item_group", "uniform_sell_price",
                _f(SITE, "product_id"), _f(SITE, "delisted")],
    )
    return {r["name"]: r for r in rows if _is_firearm_row(r)}


def _published_serials(item_codes: list[str]) -> list[dict]:
    """Serial Nos of these Items that have a live (not hidden) retail product."""
    if not item_codes:
        return []
    pid, delisted = _f(SITE, "product_id"), _f(SITE, "delisted")
    return frappe.db.sql(
        f"""SELECT name, item_code, `{pid}` AS woo_id FROM `tabSerial No`
            WHERE item_code IN %(codes)s AND IFNULL(`{pid}`, '') != ''
              AND IFNULL(`{delisted}`, 0) = 0
            ORDER BY name""",
        {"codes": tuple(item_codes)}, as_dict=True)


def _select_targets(serials: list[str], item_codes: list[str]) -> tuple[list[dict], list[dict]]:
    """Resolve the request into ``(targets, skipped)``.

    A target is ``{"doctype", "name", "woo_id"}``; ``skipped`` rows carry a
    ``reason``. With no lists: every live per-serial firearm serial plus every
    live uniform-price firearm Item. With lists: exactly those (an Item code that
    is a per-serial firearm expands to its live serials).
    """
    pid, delisted = _f(SITE, "product_id"), _f(SITE, "delisted")
    firearms = _firearm_items()
    targets: list[dict] = []
    skipped: list[dict] = []

    def add_item(code: str, row: dict) -> None:
        woo_id = str(row.get(pid) or "").strip()
        if not woo_id:
            skipped.append({"name": code, "reason": "no_product_id"})
        elif cint(row.get(delisted)):
            skipped.append({"name": code, "reason": "delisted"})
        else:
            targets.append({"doctype": "Item", "name": code, "woo_id": woo_id})

    def add_serials(codes: list[str]) -> None:
        for s in _published_serials(codes):
            targets.append({"doctype": "Serial No", "name": s["name"],
                            "woo_id": str(s["woo_id"]).strip()})

    explicit = bool(serials or item_codes)
    if not explicit:
        for code, row in firearms.items():
            if row.get("uniform_sell_price"):
                if str(row.get(pid) or "").strip() and not cint(row.get(delisted)):
                    add_item(code, row)
        add_serials([c for c, r in firearms.items() if not r.get("uniform_sell_price")])
        return targets, skipped

    for code in item_codes:
        row = firearms.get(code)
        if row is None:
            skipped.append({"name": code, "reason": "not_a_firearm_item"})
        elif row.get("uniform_sell_price"):
            add_item(code, row)
        else:
            add_serials([code])
    by_name = {r["name"]: r for r in frappe.get_all(
        "Serial No", filters={"name": ["in", serials]},
        fields=["name", "item_code", pid, delisted])} if serials else {}
    for name in serials:
        r = by_name.get(name)
        item = firearms.get(r["item_code"]) if r else None
        if r is None:
            skipped.append({"name": name, "reason": "not_found"})
        elif item is None:
            skipped.append({"name": name, "reason": "not_a_firearm_item"})
        elif item.get("uniform_sell_price"):
            skipped.append({"name": name, "reason": "uniform_item_use_item_code"})
        elif not str(r.get(pid) or "").strip():
            skipped.append({"name": name, "reason": "no_product_id"})
        elif cint(r.get(delisted)):
            skipped.append({"name": name, "reason": "delisted"})
        else:
            targets.append({"doctype": "Serial No", "name": name,
                            "woo_id": str(r[pid]).strip()})
    return targets, skipped


@frappe.whitelist()
def backfill_condition_attributes(serials=None, item_codes=None, dry_run=0):
    """Re-push ONLY the attributes to existing retail Woo firearm products.

    The attributes are Caliber / Condition / Manufacturer / Model.

    Fixes stale ``pa_condition`` terms after a grade / ``is_used`` correction in
    POS: lean machine pushes never carry attributes, so they never get there on
    their own. The payload is exactly ``{"attributes": [...]}`` per product —
    name, description, images, price, stock and meta are never sent, and no
    POS record is written. Products on the shop that were hidden
    (``woo_delisted``) or never listed are skipped.

    Attributes are authoritative on a PUT: any attribute on the product other
    than those four is removed. Run with ``dry_run=1`` first. A big run is
    best done with ``bench execute`` rather than over HTTP (it is synchronous).

    Args:
        serials: Serial No names (list, JSON list or comma-separated). Per-serial
            firearms only; a serial of a uniform-price Item is skipped (name the
            Item instead).
        item_codes: Item codes. A uniform-price firearm Item pushes its own
            product; a per-serial firearm Item expands to its live serials.
        dry_run: 1 builds every payload and reports what WOULD be sent, without
            sending (still reads the store's attribute ids).

    Returns:
        ``{"pushed": n, "skipped": n, "errors": n, "dry_run": bool, "would_push":
        n, "skipped_by_reason": {...}, "error_details": [...]}`` — ``pushed`` is
        products the store accepted; with ``dry_run`` it is 0 and ``would_push``
        counts the products that would be sent.
    """
    frappe.only_for("System Manager")
    dry_run = bool(cint(dry_run))
    targets, skipped = _select_targets(_as_list(serials), _as_list(item_codes))

    wc = WooCommerceClient.from_settings(SITE)
    attr_ids = wc.get_attribute_ids()

    errors: list[dict] = []
    updates: list[dict] = []  # {"id", "attributes"} ready to send
    owner: dict[str, dict] = {}  # woo id -> target, to attribute batch results
    for t in targets:
        if t["woo_id"] in owner:  # one request entry per product
            skipped.append({"name": t["name"], "reason": "duplicate_product_id"})
            continue
        try:
            build = (mapping.item_attributes_payload if t["doctype"] == "Item"
                     else mapping.serial_attributes_payload)
            payload = build(t["name"], attr_ids=attr_ids)
        except Exception as exc:  # one bad record must not sink the run
            errors.append({"name": t["name"], "error": str(exc)[:300]})
            continue
        # [] is a real answer ("this gun has no attributes now") and must be
        # sent — that is how a stale pa_condition gets cleared. Only a record the
        # builder gives no attributes key at all (a non-firearm) is skipped.
        if payload.get("attributes") is None:
            skipped.append({"name": t["name"], "reason": "no_attributes"})
            continue
        # id 0 would make WooCommerce mint a per-product custom attribute that no
        # filter or theme query sees — worse than not sending.
        missing = [a["slug"] for a in payload["attributes"] if not a.get("id")]
        if missing:
            errors.append({"name": t["name"],
                           "error": f"attribute taxonomy not on the store: {', '.join(missing)}"})
            continue
        owner[t["woo_id"]] = t
        updates.append({"id": int(t["woo_id"]), **payload})

    pushed = 0
    if not dry_run:
        for start in range(0, len(updates), _BATCH_SIZE):
            chunk = updates[start:start + _BATCH_SIZE]
            try:
                resp = _push_with_retry(lambda c=chunk: wc.batch_update_products(c))
            except (WooCommerceError, requests.RequestException) as exc:
                # Whole request failed: stop rather than hammer a store that is
                # down / blocking us; the rest is reported as not attempted.
                errors.extend({"name": owner[str(u["id"])]["name"], "error": str(exc)[:300]}
                              for u in chunk)
                skipped.extend({"name": owner[str(u["id"])]["name"], "reason": "aborted"}
                               for u in updates[start + _BATCH_SIZE:])
                break
            answered: set[str] = set()
            for entry in (resp.get("update") if isinstance(resp, dict) else None) or []:
                wid = str(entry.get("id")) if isinstance(entry, dict) else ""
                if wid not in owner:
                    continue
                answered.add(wid)
                if entry.get("error"):
                    err = entry["error"]
                    errors.append({"name": owner[wid]["name"],
                                   "error": str(err.get("message") or err)[:300]})
                else:
                    pushed += 1
            for u in chunk:  # asked for, never answered
                if str(u["id"]) not in answered:
                    errors.append({"name": owner[str(u["id"])]["name"],
                                   "error": "missing from the products/batch response"})

    by_reason: dict[str, int] = {}
    for s in skipped:
        by_reason[s["reason"]] = by_reason.get(s["reason"], 0) + 1
    result = {
        "pushed": pushed,
        "skipped": len(skipped),
        "errors": len(errors),
        "dry_run": dry_run,
        "would_push": len(updates) if dry_run else 0,
        "skipped_by_reason": by_reason,
        "error_details": errors[:_MAX_DETAILS],
    }
    frappe.logger("woo.backfill").info(
        f"backfill_condition_attributes: {({k: v for k, v in result.items() if k != 'error_details'})}")
    return result
