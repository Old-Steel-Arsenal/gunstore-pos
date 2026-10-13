"""Distributor (dropship) listing push — the inverted hook.

The listing pipeline lives in ``ffl_integrations`` (it knows RSR
catalogues, pricing and brand policy); the code that talks to WooCommerce lives
here.  So this one hook runs the other way round from the rest of the table:
**ffl_integrations calls, ffl_woo_sync registers**.

    # ffl_integrations/distributor/listing.py
    for fn in frappe.get_hooks("distributor_listing_push"):
        frappe.get_attr(fn)(item_code, payload)

Neither app imports the other; the payload dict is the whole contract.

Contract
--------
``distributor_listing_push(item_code: str, payload: dict) -> None``

Recognised payload keys (all optional — unknown keys are ignored, so the caller
can carry whatever context it likes):

``shipping_class``  slug: ``dropship-handgun`` / ``dropship-firearm`` /
                    ``dropship-accessory``.  Ownership (which product gets which
                    class) is the caller's decision.
``mode``            ``"stock"`` for a quantity-drift tick → the lightweight
                    stock-only push; anything else (default) → a full push.
``delist``          truthy → the product comes DOWN (draft + qty 0) instead of
                    being pushed.  Outranks ``mode``.  The drop-ship listing
                    pipeline no longer sends it: a delisted drop-ship product
                    stays PUBLISHED at zero stock (a ``mode="stock"`` push,
                    by design).  The branch remains for an explicit
                    take-down by any other caller.
``site``            ``"retail"`` (default, and the only store).

``stock`` / ``price`` / ``product_brand`` / ``_osa_restricted_states`` may be
present and are **advisory**: this app reads them from their sources instead —
the Item, and for the price the listing's website price through the
``distributor_web_price`` hook (``mapping.web_price_for_item``; the Item's own
``standard_rate`` is the store's price tag, which a drop-ship listing does not
change).  The push is enqueued ``after_commit``, so by the time it runs the
caller's writes (``web_price``, ``restricted_states``, ``brand``) are committed —
no chance of a payload and a document disagreeing about what got listed.

The push is enqueued, never inline: a B-selection batch is hundreds of SKUs and
each product create is an HTTP round-trip. A push that fails inside the queue
is reported back through the ``distributor_listing_push_failed`` hook (see
``tasks._report_listing_push_failure``), so the listing pipeline can retry it.
"""
from __future__ import annotations

import re

import frappe

from ffl_woo_sync.woocommerce.client import SITES, resolve_site

# The three dropship flat-rate classes.  Zone rates are configured once
# in the WC admin by operations; this app only ensures the terms exist and tags
# each product.  Display names are used when a term has to be created.
DROPSHIP_SHIPPING_CLASSES: dict[str, str] = {
    "dropship-handgun": "Dropship Handgun",
    "dropship-firearm": "Dropship Firearm",
    "dropship-accessory": "Dropship Accessory",
}

_FULL_PUSH_TASK = "ffl_woo_sync.woocommerce.tasks.distributor_listing_push_task"
_STOCK_BATCH_TASK = "ffl_woo_sync.woocommerce.tasks.push_stock_batch_task"
_DELIST_TASK = "ffl_woo_sync.woocommerce.tasks.delist_item_task"
_STOCK_MODES = frozenset({"stock", "stock_only"})
_COPY_PUSH_TASK = "ffl_woo_sync.woocommerce.tasks.push_copy_task"
# A WooCommerce term slug: lowercase alphanumerics and dashes.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")


def shipping_class_name(slug: str) -> str:
    """Display name for a shipping-class slug (used only when creating a term)."""
    known = DROPSHIP_SHIPPING_CLASSES.get((slug or "").strip().lower())
    return known or (slug or "").replace("-", " ").strip().title()


def _clean_slug(value: object) -> str:
    """Normalise a payload-supplied shipping-class slug.

    Two situations, deliberately handled differently:

    * **Not a slug at all** (spaces, punctuation, empty) → dropped.  It cannot
      identify a class, and creating it would put garbage in the store's
      shipping settings.  Dropped rather than raised: one odd row must not abort
      a listing batch — the product still lists, at the default shipping rate.
    * **A well-formed slug outside the table** → passed through, *and
      announced*.  Which class a product belongs to is the caller's decision and
      the table may grow, but this WILL create a shipping class on the live
      store, so it does not happen quietly.
    """
    slug = str(value or "").strip().lower()
    if not slug:
        return ""
    if not _SLUG_RE.match(slug):
        frappe.logger("woo.sync").warning(
            f"distributor_listing_push: ignoring malformed shipping_class {value!r}"
        )
        return ""
    if slug not in DROPSHIP_SHIPPING_CLASSES:
        frappe.logger("woo.sync").warning(
            f"distributor_listing_push: shipping_class {slug!r} is not one of the "
            f"known classes {sorted(DROPSHIP_SHIPPING_CLASSES)} — it will be "
            "created on the store if it does not already exist"
        )
    return slug


def _site_product_id(item_code: str, site: str) -> str:
    """The WooCommerce product id the Item carries for ``site`` ('' when none)."""
    from ffl_woo_sync.woocommerce.sync import _f

    return str(frappe.db.get_value("Item", item_code, _f(site, "product_id")) or "")


# --------------------------------------------------------------------------
# Stock refreshes: N calls of the hook -> ONE queued ``push_stock_batch_task``
# --------------------------------------------------------------------------
#
# `sync_listings` ticks every 15 minutes and pushes each moved listing's stock
# through this hook.  Queueing a job (and issuing a Woo PUT) per item made a
# ~68-job burst on the single serial `long` worker; the items are collected here
# instead and handed to ONE job that sends them with `products/batch`.
#
# No state outside the database transaction machinery: the items live in
# `frappe.flags` (this request / job only) in two stages —
#
#   pending  written by the hook, inside the caller's still-open transaction
#   ready    that transaction COMMITTED (state the job will read is visible)
#
# `db.after_commit` moves pending -> ready, `db.after_rollback` drops pending, so
# a rolled-back trigger enqueues nothing and leaves nothing behind.  (Frappe's
# `db.rollback()` resets after_commit and runs after_rollback; a savepoint
# rollback runs neither — same as `enqueue_after_commit`, which this replaces.)
#
# The ready items are enqueued at once when the code runs outside a background
# job.  Inside a job (the drop-ship sync commits after EVERY listing) that would
# be one job per listing again, so they are held until the job ends
# (`frappe.job.after_job`, run after the job's final commit) and go out together.
# The job reads every quantity when it runs, so a take-down (0) and a refresh
# are the same work and the queued list carries no quantity.
_STOCK_STATE = "woo_stock_refresh"


def _stock_state() -> dict:
    state = frappe.flags.get(_STOCK_STATE)
    if state is None:
        state = frappe.flags[_STOCK_STATE] = {"armed": False, "pending": {}, "ready": {}}
    return state


def queue_stock_refresh(item_code: str, site: str = "retail") -> None:
    """Note that ``item_code``'s WooCommerce quantity needs a push.

    Nothing is queued now: the item is enqueued (with every other item of this
    request / job, deduplicated) once the caller's transaction commits — see the
    block comment above.  Registers ONE commit callback and ONE rollback
    callback per transaction, however many items follow.
    """
    state = _stock_state()
    if not state["armed"]:
        state["armed"] = True
        frappe.db.after_commit.add(_stock_committed)
        frappe.db.after_rollback.add(_stock_rolled_back)
    state["pending"].setdefault(site, {})[item_code] = None  # dict = ordered set


def _stock_committed() -> None:
    state = _stock_state()
    state["armed"] = False
    pending, state["pending"] = state["pending"], {}
    if not pending:
        return
    if frappe.job:
        # lazy: items held between their commit and the job's end live in worker
        # memory, so a SIGKILL there loses the push (osa_dropship already marked
        # it sent; its 24 h stale-baseline resync heals it) — upgrade: a
        # persisted pending table drained by the batch job.
        holding = bool(state["ready"])
        for site, codes in pending.items():
            state["ready"].setdefault(site, {}).update(codes)
        if not holding:
            frappe.job.after_job.add(_stock_flush)
        return
    # lazy: outside a job, one queued job per COMMIT of the request — a request
    # that commits repeatedly (none of today's stock callers) gets several.
    _stock_enqueue(pending)


def _stock_rolled_back() -> None:
    state = _stock_state()
    state["armed"] = False
    state["pending"] = {}  # `ready` was committed: it stays


def _stock_flush() -> None:
    state = _stock_state()
    ready, state["ready"] = state["ready"], {}
    _stock_enqueue(ready)


def _stock_enqueue(by_site: dict[str, dict]) -> None:
    """One ``push_stock_batch_task`` per site, run AFTER the commit (so
    ``enqueue_after_commit`` is off: it would wait for a commit that is not
    coming).  A failed enqueue is reported per item through the same path a
    failed push takes, so the listing records the error and the next sync
    retries it."""
    from ffl_woo_sync.woocommerce import tasks

    for site, codes in by_site.items():
        item_codes = list(codes)
        try:
            frappe.enqueue(_STOCK_BATCH_TASK, queue="long", site=site, item_codes=item_codes)
        except Exception as exc:
            message = frappe.get_traceback()
            for code in item_codes:
                tasks.report_stock_push_failure(code, site, exc, message)
            tasks.safe_commit()


def distributor_listing_push(item_code: str, payload: dict | None = None) -> None:
    """``distributor_listing_push`` hook implementation (registered in hooks.py).

    Enqueues this app's own push wrapper for ``item_code``.  Raises
    :class:`ValueError` on a malformed call — a ``site`` this POS runs no store for
    included — the hook may raise, and a listing run that pushed nothing
    must not look like a success.
    """
    if not item_code:
        raise ValueError("distributor_listing_push() requires an item_code")

    payload = payload or {}
    site = resolve_site(payload.get("site"))
    if site is None:
        raise ValueError(
            f"distributor_listing_push(): unknown site {payload.get('site')!r} "
            f"(expected one of {sorted(SITES)})"
        )

    # Same guard the doc_events pushes use: never echo an inbound webhook back
    # to WooCommerce, and stay quiet during install / migrate / import.
    from ffl_woo_sync.woocommerce.doc_events import _skip_push

    if _skip_push():
        return

    def _job(action: str) -> dict:
        """Enqueue kwargs, deduplicated per item **per action**.

        `sync_listings` ticks every 15 minutes across hundreds of SKUs, so a
        repeat of the SAME action while one is still queued should collapse
        (house pattern: ffl_integrations/shipstation/tasks.py:52).

        The action belongs in the key.  With one key per item, whichever action
        queued first would silently eat the other: a `delist` arriving while a
        push is queued would be dropped and the discontinued product would stay
        on the shop — exactly the C2 hole the delist branch exists to close —
        and a restock push arriving behind a queued delist would leave a live
        SKU dark.  Different actions are different work; they queue separately
        and run in order.

        (Within an action, the queued job's own arguments win — a
        `shipping_class` that changed while it waited is corrected by the next
        15-minute sweep, which re-pushes with the current one.)
        """
        return {
            "queue": "long",
            "enqueue_after_commit": True,
            "job_id": f"woo-listing:{site}:{item_code}:{action}",
            "deduplicate": True,
            "item_code": item_code,
            "site": site,
        }

    if payload.get("delist"):
        # Outranks `mode`: a payload carrying both is a product going away, and
        # re-stocking it would put it straight back on the shop.
        frappe.enqueue(_DELIST_TASK, **_job("delist"))
        return

    mode = str(payload.get("mode") or payload.get("push_mode") or "full").strip().lower()
    if mode == "copy":
        # Our optimized copy, loaded after the product was listed: name and
        # description only — a full push would sideload every image again. A
        # product not on this site yet gets the copy with its first (full) push.
        if _site_product_id(item_code, site):
            frappe.enqueue(_COPY_PUSH_TASK, **_job("copy"))
        return
    if mode in _STOCK_MODES:
        # Quantity drift only — reuse the lightweight stock-only path rather than
        # rebuilding and re-uploading the whole product payload.
        if not _site_product_id(item_code, site):
            # No product on this site yet (its full push is still queued, or failed
            # and is being retried): there is nothing to update, and the full push
            # carries the quantity. A queued stock job would only fail, be reported
            # and be retried for ever.
            return
        # Coalesced into one batch job at commit (see `queue_stock_refresh`).
        queue_stock_refresh(item_code, site)
        return

    # `pos_fields_only` (absent = full, same convention as `mode`): the ordinary
    # `*/15` sync sets it so a routine price drift updates only POS-owned fields
    # on an existing product. Lean and full are DIFFERENT actions for the dedup
    # key — a queued lean push must never swallow an operator's forced full
    # re-sync (its args would win and the repair would silently not happen).
    lean = bool(payload.get("pos_fields_only"))
    frappe.enqueue(
        _FULL_PUSH_TASK,
        shipping_class=_clean_slug(payload.get("shipping_class")),
        pos_fields_only=lean,
        **_job("lean" if lean else "full"),
    )
