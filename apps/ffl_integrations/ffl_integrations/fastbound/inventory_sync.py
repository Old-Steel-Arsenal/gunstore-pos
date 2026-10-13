"""Reconcile ERPNext firearm stock against the FastBound bound book.

FastBound is the ATF source of truth. When a firearm is disposed in FastBound
(sold/transferred) but its ERPNext sale never completed — e.g. a held POS
invoice whose 4473 webhook failed — the gun lingers as in-stock in ERPNext.
This pulls FastBound's items and Material-Issues every in-stock firearm that
FastBound reports `disposed` (voided `deleted` records are skipped).

Matching keys off the FastBound item id stamped on the Serial No
(`fastbound_item_id`), not the serial string: serials aren't unique across
manufacturers and the DB compares them case-/space-insensitively, so a bare
serial match could remove the wrong gun. A firearm never pushed to FastBound
(no `fastbound_item_id`) therefore can't be auto-removed — the safe direction.

Safety: a partial/failed FastBound fetch raises and removes nothing, so the
sync under-removes (a later run catches up) and never mass-removes. The real
(non-dry) run removes only the FastBound item ids the operator approved from the
dry-run preview, re-verified as still disposed and in stock.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from ffl_core.firearm import issue_serial
from ffl_core.roles import OVERVIEW_ROLES

_ALLOWED_ROLES = {"Sales Manager", "Stock Manager", "System Manager"}


@frappe.whitelist()
def sync_in_stock_from_boundbook(dry_run: int | str = 0, item_ids=None) -> dict:
    """Material-Issue in-stock firearms FastBound reports disposed.

    Args:
        dry_run: 1 returns the would-remove list (each with its FastBound
            item id) without touching stock.
        item_ids: On the real run, the ids the operator approved from the
            dry-run preview — only those are removed, and only if still
            disposed and in stock. That makes the confirmation
            authoritative: a firearm that became disposed between the
            preview and the apply is never removed without being shown.

    Returns:
        Dry run: {dry_run: True, fb_disposed_count, would_remove,
        would_remove_count, consignment_held, consignment_held_count};
        real run: {dry_run: False, fb_disposed_count, removed,
        removed_count, errors}. Guns held at a consignment dealer are
        reported under consignment_held and are never removed by either
        run — "disposed in FastBound, in stock here" is their correct
        steady state, not a discrepancy (see in_stock_targets).

    Permissions:
        Stock Manager / Sales Manager / System Manager.
    """
    _gate()
    return _sync_in_stock_body(cint(dry_run) != 0, item_ids)


@frappe.whitelist(methods=["POST"])
def boundbook_mismatches() -> dict:
    """Report in-stock firearms FastBound shows as disposed — dry run only.

    The read-only face of `sync_in_stock_from_boundbook`: it runs the same
    reconcile with the dry-run switch hard-wired on, so it can never
    Material-Issue stock (there is no flag to turn writing on). Safe for
    read-only API consumers such as the nightly check. POST-only because it
    pages the whole FastBound book on every call.

    Returns:
        Same dict as `sync_in_stock_from_boundbook` with `dry_run=1`:
        {dry_run: True, fb_disposed_count, would_remove, would_remove_count,
        consignment_held, consignment_held_count}. A dry run always reports
        every mismatch.
    """
    frappe.only_for(OVERVIEW_ROLES)
    return _sync_in_stock_body(True)


def _gate() -> None:
    """Role gate shared by the reconcile endpoints."""
    if not (set(frappe.get_roles()) & _ALLOWED_ROLES):
        frappe.throw(
            _("Bound-book sync requires Stock Manager, Sales Manager, or System Manager."),
            frappe.PermissionError,
        )


def _sync_in_stock_body(dry: bool, item_ids=None) -> dict:
    """The reconcile itself (ungated — callers gate). `dry` False removes stock."""
    disposed = _fb_disposed_items()  # {fb_id: serial}; raises on FastBound error -> nothing removed
    targets = in_stock_targets(disposed)

    if dry:
        # Consignment stock is unreachable from in_stock_targets on purpose;
        # report it so the counts add up (see consignment_held_targets).
        consigned = consignment_held_targets(disposed)
        return {
            "dry_run": True,
            "fb_disposed_count": len(disposed),
            "would_remove": targets,
            "would_remove_count": len(targets),
            "consignment_held": consigned,
            "consignment_held_count": len(consigned),
        }

    approved = _parse_item_ids(item_ids)
    if approved is not None:
        targets = [t for t in targets if t["fb_id"] in approved]

    removed, errors = [], []
    for t in targets:
        try:
            t["stock_entry"] = issue_serial(t["item_code"], t["serial"], t["warehouse"])
            frappe.db.commit()
            removed.append(t)
        except Exception as exc:  # noqa: BLE001 — one bad serial must not abort the batch
            frappe.db.rollback()
            errors.append({"serial": t["serial"], "error": str(exc)[:300]})

    return {
        "dry_run": False,
        "fb_disposed_count": len(disposed),
        "removed": removed,
        "removed_count": len(removed),
        "errors": errors,
    }


def _parse_item_ids(item_ids) -> set[str] | None:
    """Approved FastBound item ids from the client. Only a genuinely ABSENT param
    (None) means "no filter" (remove every disposed in-stock firearm — the
    programmatic full-reconcile path). An EXPLICIT empty selection — "" or [] —
    returns an empty set (remove NOTHING), so a UI that posts an empty approval
    can never mass-issue every disposed gun out of stock."""
    if item_ids is None:
        return None
    if isinstance(item_ids, str):
        item_ids = item_ids.strip()
        item_ids = frappe.parse_json(item_ids) if item_ids else []
    return {str(x).strip() for x in (item_ids or []) if str(x).strip()}


def _fb_disposed_items() -> dict[str, str]:
    """Map FastBound item id -> serial for every item FastBound reports disposed,
    excluding voided (`deleted`) records. Raises if FastBound is unreachable, so
    the caller never removes stock on incomplete data.

    Matching keys off the FastBound item id (stable + unique), not the serial
    string: serials aren't unique across manufacturers and the DB compares them
    case-/space-insensitively, so a bare serial match could remove the wrong gun.
    """
    from ffl_integrations.fastbound.client import FastBoundClient

    client = FastBoundClient.from_settings()
    disposed: dict[str, str] = {}
    for page in client.iter_items(page_size=100):
        for item in page:
            if not item.get("disposed") or item.get("deleted"):
                continue
            fb_id = (item.get("id") or "").strip()
            if fb_id:
                disposed[fb_id] = (item.get("serial") or "").strip()
    return disposed


def _disposed_in_stock_rows(disposed: dict[str, str]) -> list:
    """Active firearm Serial No rows whose fastbound_item_id is in `disposed`.
    The shared query behind in_stock_targets and consignment_held_targets; an
    empty disposed set means no rows (and no query)."""
    if not disposed:
        return []
    return frappe.db.sql(
        """
        SELECT sn.name AS serial, sn.item_code, sn.warehouse,
               sn.fastbound_item_id AS fb_id, sn.consigned_out
        FROM `tabSerial No` sn
        INNER JOIN `tabItem` i ON i.name = sn.item_code AND i.is_firearm = 1
        WHERE sn.status = 'Active' AND sn.fastbound_item_id IN %(ids)s
        """,
        {"ids": tuple(disposed)},
        as_dict=True,
    )


def _shape(r) -> dict:
    return {"serial": r.serial, "item_code": r.item_code, "warehouse": r.warehouse, "fb_id": r.fb_id}


def _split_consignment(disposed: dict[str, str]) -> tuple[list, list]:
    """(safe_rows, consignment_rows) by the canonical two-key predicate."""
    from ffl_core.warehouses import consignment_warehouse_names, is_consigned_out

    names = consignment_warehouse_names()
    safe, held = [], []
    for r in _disposed_in_stock_rows(disposed):
        target = held if is_consigned_out(
            consigned_out=r.consigned_out, warehouse=r.warehouse, names=names) else safe
        target.append(r)
    return safe, held


def in_stock_targets(disposed: dict[str, str]) -> list[dict]:
    """Firearms FastBound reports disposed that are still in stock here AND are
    safe to issue out. Outbound-consignment stock is never in this list.

    A gun out on consignment is *supposed* to be disposed in FastBound and
    Active here at once: the A&D book records that FFL responsibility moved to
    the dealer, while the asset stays ours until the dealer sells it. That
    "disposed there, in stock here" shape is its CORRECT steady state, not a
    discrepancy — and issuing it out (the only thing either caller does with a
    row from here) makes the asset vanish from the books and skips the
    settlement invoice that is the sole way the sale gets billed.

    Incident CONO-2026-01544 (2026-08-08) was exactly that, through the webhook
    caller: 39 consignment guns written off for $22,323.85. `apply=true` on
    sync_in_stock_from_boundbook would have done the same to the same 39 from
    the other side. Both consumers draw their rows from here, so excluding it
    here closes both.

    The exclusion is warehouses.is_consigned_out — the canonical two-key
    predicate (consigned_out flag OR a consignment warehouse). Consignment rows
    are not merely filtered out of a shared return type: they are unreachable
    from this function, so a stock-out path cannot opt into them. To REPORT on
    them, call consignment_held_targets.
    """
    safe, _held = _split_consignment(disposed)
    return [_shape(r) for r in safe]


def consignment_held_targets(disposed: dict[str, str]) -> list[dict]:
    """The rows in_stock_targets deliberately withholds — guns held at a
    consignment dealer that FastBound reports disposed.

    For REPORTING only: an operator reconciling the book has to see where those
    guns went, or the counts silently won't add up and the next person "fixes"
    it by hand. Never feed these to a stock-out.
    """
    _safe, held = _split_consignment(disposed)
    return [_shape(r) for r in held]
