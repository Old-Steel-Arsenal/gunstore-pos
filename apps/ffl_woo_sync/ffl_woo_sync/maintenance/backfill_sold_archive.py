"""One-time SOLD-archive backfill & zombie audit.

Every per-serial firearm that left the shelf BEFORE the SOLD-archive lifecycle
 shipped is in one of the four nondeterministic states catalogued in
the audit.  This module triages them so the operator can see exactly what would
change before anything is written.

    ENUMERATE   serials with ``{site}_delisted=1`` OR
                (``status`` in ``sync.SOLD_STATUS`` AND ``{site}_product_id``
                non-empty)
    CLASSIFY    each into one of :data:`BUCKETS`
    REPORT      counts + the serial names behind every bucket

**This module is NOT wired into ``patches.txt`` and must never be.**  It is
operator-invoked, and :func:`apply_archive` writes to a live WooCommerce store —
a prod write, which per the project's prod-isolation policy happens only in the
main session with explicit owner confirmation, after the report has been read.
``tests/test_backfill_sold_archive.py`` pins the patches.txt exclusion so a
future "let's just automate it" cannot turn a deploy into an unattended
storefront rewrite.

Read-only usage (safe anywhere)::

    bench --site <site> execute \\
        ffl_woo_sync.maintenance.backfill_sold_archive.print_report

Apply — CONFIRMED PROD WRITE, one explicit list of serials from the report::

    bench --site <site> execute \\
        ffl_woo_sync.maintenance.backfill_sold_archive.apply_archive \\
        --kwargs "{'serials': ['SP000279', ...]}"

There is deliberately no "apply everything the query found" entry point: the
archive bucket is a *recommendation*, and a human has to hand the serials back.
"""
from __future__ import annotations

import frappe

# Buckets, in triage order.
ARCHIVE = "archive"                      # sold + still listed → give it a SOLD page
DELISTED_NOT_SOLD = "delisted_not_sold"  # removed, not sold → stays draft, wants a 301
ALREADY_ARCHIVED = "already_archived"    # nothing to do
STALE_SENTINEL = "stale_sentinel"        # delisted=1 but no product id → flag points at nothing
NEVER_LISTED = "never_listed"            # POS-only inventory → no URL was ever exposed

BUCKETS = (ARCHIVE, DELISTED_NOT_SOLD, ALREADY_ARCHIVED, STALE_SENTINEL, NEVER_LISTED)

# NOT a private copy: imported from sync so the two can never drift.  They DID
# drift once — this module kept its own "Delivered" while sync gained
# ``Consumed``, which would have made the audit miss the entire web-sold
# population it exists to surface.
from ffl_woo_sync.woocommerce.sync import SOLD_STATUS as _SOLD_STATUS


def _fields(site: str) -> dict[str, str]:
    """The site's Serial No column names (via sync's single source of truth)."""
    from ffl_woo_sync.woocommerce.sync import _f
    return {key: _f(site, key) for key in ("product_id", "delisted", "sold_archived")}


def classify(row, site: str = "retail") -> str:
    """Triage ONE Serial No row into a bucket.  Pure — no DB, no HTTP.

    Order matters, and mirrors :func:`sync.route_serial_exit`: the sold check
    comes first, so a gun that sold is never mistaken for a plain withdrawal
    just because someone delisted it on the way out (that is exactly the
    SP000279 shape — ``Delivered`` AND ``delisted=1``).
    """
    f = _fields(site)
    listed = bool(row.get(f["product_id"]))
    sold = (row.get("status") or "") in _SOLD_STATUS

    if row.get(f["sold_archived"]):
        return ALREADY_ARCHIVED
    if sold:
        return ARCHIVE if listed else NEVER_LISTED
    if row.get(f["delisted"]):
        return DELISTED_NOT_SOLD if listed else STALE_SENTINEL
    return NEVER_LISTED


def enumerate_candidates(site: str = "retail") -> list:
    """Every Serial No in scope for the backfill, deduped by name.  READ-ONLY.

    Two queries rather than one ``or_filters``: Frappe ORs the whole or_filters
    set, which cannot express "delisted=1 OR (sold AND listed)" without also
    matching bare sold rows that were never web-listed.
    """
    f = _fields(site)
    fields = ["name", "status", f["product_id"], f["delisted"], f["sold_archived"]]

    rows: dict[str, object] = {}
    for filters in (
        {f["delisted"]: 1},
        {"status": ["in", list(_SOLD_STATUS)], f["product_id"]: ["!=", ""]},
    ):
        for row in frappe.get_all("Serial No", filters=filters, fields=fields):
            rows[row.name] = row
    return list(rows.values())


def build_report(site: str = "retail") -> dict:
    """``{site, total, counts, buckets}`` for the whole candidate set.  READ-ONLY.

    Every bucket is present even when empty — an omitted bucket reads as "not
    checked" rather than "checked, nothing found".
    """
    buckets: dict[str, list[str]] = {b: [] for b in BUCKETS}
    rows = enumerate_candidates(site)
    for row in rows:
        buckets[classify(row, site)].append(row.name)
    return {
        "site": site,
        "total": len(rows),
        "counts": {b: len(names) for b, names in buckets.items()},
        "buckets": buckets,
    }


def format_report(report: dict) -> str:
    """Render :func:`build_report` as an operator-readable block."""
    lines = [
        f"SOLD-archive backfill audit — site={report['site']}, "
        f"{report['total']} candidate serial(s)",
        "",
    ]
    for bucket in BUCKETS:
        names = report["buckets"][bucket]
        lines.append(f"  {bucket:<20} {len(names):>4}")
        for name in names:
            lines.append(f"      {name}")
    lines += [
        "",
        f"  → '{ARCHIVE}' is a RECOMMENDATION. Applying it writes to the live "
        f"store; run apply_archive with an explicit serial list after review.",
        f"  → '{DELISTED_NOT_SOLD}' stays drafted and needs a 301 from the storefront plugin's "
        f"redirect engine.",
        f"  → '{STALE_SENTINEL}' has a delisted flag pointing at no WC product; "
        f"clear the stale link.",
    ]
    return "\n".join(lines)


def print_report(site: str = "retail") -> None:
    """READ-ONLY entry point — enumerate, classify, print.  Writes nothing."""
    print(format_report(build_report(site)))


def apply_archive(serials: list[str], site: str = "retail") -> dict:
    """Archive an EXPLICIT list of serials.  **CONFIRMED PROD WRITE.**

    ``serials`` is a list a human copied out of the report's ``archive`` bucket —
    this function never runs its own query, so it cannot act on more than was
    reviewed.  No ``sold_price`` is passed: a backfill cannot know the realized
    figure, and the rule is to show none rather than an inexact one.

    Per-serial failures are collected, not raised, so one dead WC product does
    not strand the rest of the batch half-done.

    Retail only — refused up front rather than as N identical
    per-serial errors.  ``archive_serial_sold`` enforces this too; the early
    check just makes the operator's mistake legible in one line.
    """
    from ffl_woo_sync.woocommerce import sync

    if site != sync.RETAIL_SITE:
        raise ValueError(
            f"The SOLD archive is retail-only (spec Addendum 5); refusing site {site!r}. "
            f"Non-retail channels stay drafted — there is nothing to backfill there."
        )

    archived: list[str] = []
    errors: list[str] = []
    for serial_no in serials:
        try:
            sync.archive_serial_sold(serial_no, site=site)
            archived.append(serial_no)
        except Exception as exc:
            errors.append(f"{serial_no}: {str(exc)[:200]}")
    return {"archived": archived, "errors": errors}
