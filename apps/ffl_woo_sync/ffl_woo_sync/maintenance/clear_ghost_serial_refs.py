"""Clear dangling per-serial WooCommerce references left by the 2026-07-21 flood.

Incident: a caliber-cleanup batch drove a
bulk per-serial push over ~40 uniform-priced firearm Items, minting 122 ghost
``item_code::serial`` products (plus 5 June siblings, minus 1 kept) — **126**
trashed on the Woo side by lead.  Each Serial No that pushed one still carries the
now-dangling ``woo_product_id`` (and possibly ``woo_delisted``); this module
clears those references so the POS record stops pointing at a trashed product.

Scope:
  * PRIMARY  — the 126 trashed Woo product ids (``INCIDENT_GHOST_PRODUCT_IDS``,
    = 121 + 5 June siblings).  Any extra
    ids surfaced by a Woo-side SKU reverse-lookup can be passed via
    ``product_ids=[...]``.
  * EXCLUDE  — ``U413254`` (a held product): held for
    an in-flight order, deliberately NOT trashed.  Never cleared, even if it
    were somehow to reference a listed id (defense in depth).
  * AUDIT    — :func:`audit_uniform_legacy` additionally REPORTS (never touches)
    the per-serial products that still exist under uniform-priced firearm Items,
    so a human can decide what to do with the surviving legacy listings.

This module writes to Serial No (a live prod DB write when run against prod) and
therefore, exactly like ``backfill_sold_archive``, is **operator-invoked and NOT
wired into patches.txt** — a deploy must never run it unattended.  Per the
project's prod-isolation policy the apply step runs in the main session with
explicit owner confirmation, after the dry-run report has been read.

Read-only usage (safe anywhere — dry run of the default incident set)::

    bench --site <site> execute \\
        ffl_woo_sync.maintenance.clear_ghost_serial_refs.print_report

Apply — CONFIRMED PROD WRITE (defaults to the 126-id incident set; pass an
explicit list to extend/narrow)::

    bench --site <site> execute \\
        ffl_woo_sync.maintenance.clear_ghost_serial_refs.apply_clear

    bench --site <site> execute \\
        ffl_woo_sync.maintenance.clear_ghost_serial_refs.apply_clear \\
        --kwargs "{'product_ids': [2553, 2561, 9999]}"

Idempotent: once a Serial No's ``woo_product_id`` is cleared, no row references
the id, so a re-run reports it under ``not_found`` and writes nothing.
"""
from __future__ import annotations

import frappe

# The 126 trashed ghost product ids:
# trash-ids.txt (121, the 07-21 batch) + june-trash-ids.txt (5, the June
# siblings).  one product is deliberately absent — it is the kept product for the
# in-flight order and U413254 is excluded below.
INCIDENT_GHOST_PRODUCT_IDS: tuple[int, ...] = (
    2553, 2561, 2568, 2569, 2578, 2579, 2580, 2581, 2582, 2583, 2584, 2585,
    2586, 2593, 2594, 2595, 2596, 2597, 2598, 2599, 2602, 2607, 2608, 2609,
    2610, 2611, 2639, 2665, 2674, 2677, 2680, 2681, 2682, 2685, 2687, 2694,
    2700, 2705, 2715, 2725, 2735, 2748, 2761, 2776, 2788, 2806, 2813, 2825,
    2830, 2844, 2847, 2863, 2887, 2888, 2909, 2915, 2917, 2932, 2940, 2942,
    2957, 2960, 2974, 2978, 2991, 3000, 3017, 3027, 3029, 3046, 3047, 3048,
    3049, 3057, 3079, 3084, 3085, 3092, 3093, 3094, 3107, 3109, 3112, 3114,
    3125, 3126, 3127, 3128, 3129, 3133, 3135, 3139, 3142, 3143, 3144, 3145,
    3146, 3147, 3148, 3149, 3150, 3151, 3152, 3153, 3160, 3161, 3162, 3163,
    3164, 3165, 3180, 3187, 3195, 3196, 3197, 3198, 3199, 3200, 3201, 3214,
    3661, 1143, 1151, 1215, 1820, 1823,
)

# Never cleared: one product (U413254) is held for
# an in-flight order and stays outofstock+publish until that order lands.
EXCLUDED_SERIALS: frozenset[str] = frozenset({"U413254"})


def _fields(site: str) -> dict[str, str]:
    """The site's Serial No column names (via sync's single source of truth)."""
    from ffl_woo_sync.woocommerce.sync import _f
    return {key: _f(site, key) for key in ("product_id", "delisted")}


def _normalize_ids(product_ids) -> list[str]:
    """The id set to act on, as strings (Serial No stores product ids as text)."""
    ids = INCIDENT_GHOST_PRODUCT_IDS if product_ids is None else product_ids
    # dedupe, preserve order — a hand-passed list may repeat.
    seen: dict[str, None] = {}
    for pid in ids:
        seen.setdefault(str(pid), None)
    return list(seen)


def find_serials(product_ids, site: str = "retail") -> dict[str, list[str]]:
    """``{product_id: [serial names]}`` for every Serial No pointing at one of
    *product_ids* on this channel.  READ-ONLY.

    One ``in`` query rather than N point lookups; the result is grouped so the
    caller can tell "cleared/never-written-back" (absent key) from "still
    referenced" (present key)."""
    f = _fields(site)
    ids = [str(p) for p in product_ids]
    rows = frappe.get_all(
        "Serial No",
        filters={f["product_id"]: ["in", ids]},
        fields=["name", f["product_id"]],
    )
    grouped: dict[str, list[str]] = {}
    for row in rows:
        pid = str(row.get(f["product_id"]))
        grouped.setdefault(pid, []).append(row["name"])
    return grouped


def audit_uniform_legacy(site: str = "retail") -> list:
    """REPORT (never touch) the per-serial products that still exist under
    uniform-priced firearm Items — surviving legacy listings a human must decide
    on.  READ-ONLY.

    Includes U413254 by construction; that one is the known in-flight
    exception and is called out in the report footer."""
    from ffl_core.firearm import _is_firearm_row

    f = _fields(site)
    items = frappe.get_all(
        "Item", filters={"uniform_sell_price": 1},
        fields=["name", "is_firearm", "item_group"],
    )
    uni_firearm = [i["name"] for i in items if _is_firearm_row(i)]
    if not uni_firearm:
        return []
    return frappe.get_all(
        "Serial No",
        filters={f["product_id"]: ["!=", ""], "item_code": ["in", uni_firearm]},
        fields=["name", "item_code", f["product_id"]],
    )


def build_report(product_ids=None, site: str = "retail") -> dict:
    """Classify the id set into ``will_clear`` / ``excluded`` / ``not_found`` and
    attach the uniform-legacy audit.  READ-ONLY — writes nothing."""
    ids = _normalize_ids(product_ids)
    found = find_serials(ids, site)

    will_clear: list[str] = []
    excluded: list[str] = []
    not_found: list[str] = []
    for pid in ids:
        serials = found.get(pid)
        if not serials:
            not_found.append(pid)
            continue
        for sn in serials:
            (excluded if sn in EXCLUDED_SERIALS else will_clear).append(sn)

    return {
        "site": site,
        "total_ids": len(ids),
        "counts": {
            "will_clear": len(will_clear),
            "excluded": len(excluded),
            "not_found": len(not_found),
        },
        "will_clear": will_clear,
        "excluded": excluded,
        "not_found": not_found,
        "audit_uniform_legacy": audit_uniform_legacy(site),
    }


def format_report(report: dict) -> str:
    """Render :func:`build_report` as an operator-readable block."""
    f = _fields(report["site"])
    lines = [
        f"Ghost per-serial reference cleanup — site={report['site']}, "
        f"{report['total_ids']} product id(s) in scope",
        "",
        f"  will_clear   {report['counts']['will_clear']:>4}  "
        f"(clear {f['product_id']} + {f['delisted']})",
    ]
    for sn in report["will_clear"]:
        lines.append(f"      {sn}")
    lines.append(f"  excluded     {report['counts']['excluded']:>4}  "
                 f"(in-flight order #3687 — left untouched)")
    for sn in report["excluded"]:
        lines.append(f"      {sn}")
    lines.append(f"  not_found    {report['counts']['not_found']:>4}  "
                 f"(no Serial No references it — already cleared / idempotent)")

    audit = report["audit_uniform_legacy"]
    lines += [
        "",
        f"  audit: per-serial products still under uniform-priced firearms "
        f"({len(audit)}) — REPORT ONLY, nothing here is changed:",
    ]
    for row in audit:
        lines.append(f"      {row['name']:<16} item={row.get('item_code')}  "
                     f"{f['product_id']}={row.get(f['product_id'])}")
    lines += [
        "",
        "  → apply_clear writes to Serial No (a prod write against prod). Review "
        "this report first, then run it with owner confirmation.",
        "  → U413254 / #2865 stays as-is until order #3687 lands (lead-tracked).",
    ]
    return "\n".join(lines)


def print_report(product_ids=None, site: str = "retail") -> None:
    """READ-ONLY entry point — classify + print.  Writes nothing."""
    print(format_report(build_report(product_ids, site)))


def apply_clear(product_ids=None, site: str = "retail") -> dict:
    """Clear the dangling ``{site}_product_id`` + ``{site}_delisted`` on every
    Serial No that references one of the trashed ids.  **CONFIRMED PROD WRITE.**

    Defaults to the 126-id incident set; pass ``product_ids`` to extend or narrow.
    ``EXCLUDED_SERIALS`` (U413254) is skipped even if it references a listed id.
    Idempotent — a re-run finds no referencing rows and writes nothing."""
    ids = _normalize_ids(product_ids)
    f = _fields(site)
    found = find_serials(ids, site)

    cleared: list[str] = []
    skipped_excluded: list[str] = []
    for serials in found.values():
        for sn in serials:
            if sn in EXCLUDED_SERIALS:
                skipped_excluded.append(sn)
                continue
            frappe.db.set_value("Serial No", sn, {f["product_id"]: "", f["delisted"]: 0})
            cleared.append(sn)

    return {
        "cleared": cleared,
        "skipped_excluded": skipped_excluded,
        "not_found_ids": [pid for pid in ids if pid not in found],
    }
