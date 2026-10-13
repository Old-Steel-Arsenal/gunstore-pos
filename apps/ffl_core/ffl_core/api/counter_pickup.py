"""Counter pickup of a firearm — the 4473 ticket.

A counter order (Create Order → Sales Invoice) may be a pickup with a regulated
firearm on it ONLY when that gun is ordered to the store: the customer pays at
the desk today, the distributor ships the gun to us, and the customer collects
it here with a 4473 once it has arrived. The Sales Invoice is the sale — it
booked the revenue and the payment on order day — but a retail 4473 hangs off
a **POS Invoice** (``start_4473_for_pos_invoice`` begins there, the FastBound
webhook proves coverage against one, and its submit is what issues the gun's
stock). So when the gun arrives this module opens a DRAFT POS Invoice for it:

* the gun line(s) at **$0**, with the serial the receive hung on the order —
  the money is already on the Sales Invoice, and a second full-price line would
  book the sale twice. The remark says so, and every save of the ticket —
  whoever makes it — re-pins it to $0 and refuses items the order did not sell
  (:func:`keep_ticket_free`, :func:`assert_ticket_free`);
* linked back through ``POS Invoice.ffl_counter_order``, which is also the
  idempotency key (one open draft per order) and what the queue reads to offer
  "Open counter order" instead of "Pickup";
* the ordinary counter flow takes it from there: 4473, NICS, Complete Order.
  On that submit the Sales Invoice reads the gun as covered
  (:func:`ffl_core.reservations.si_firearm_serial_state`) and, when nothing
  else is owed, is cleared from the queue.

It opens automatically from the receive (ffl_integrations' receive bridge)
when a register is open, and from the Pending Order page's Pickup button
otherwise — the same builder either way.

The Sales Report reports the SALE — the counter order's line, at its price,
costed from the serial the receive hung on it — and skips the ticket
(``ffl_counter_order`` set), which would otherwise show as a second, free gun.
The sale receipt e-mail skips it for the same reason (ffl_core.receipts).
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt

from ffl_core.pos_draft import open_drafts_by, open_shift, profile_defaults
from ffl_core.roles import COUNTER_ROLES
from ffl_core.utils import set_missing_values_keeping_taxes

# POS Invoice → the counter order (Sales Invoice) whose gun it hands over.
LINK_FIELD = "ffl_counter_order"


def is_pickup_order(si) -> bool:
	"""The positive marker create_order stamps on a counter pickup. The ONE
	definition — the 4473 gate, the serial-state reader and the queue all ask
	here."""
	return bool(cint(getattr(si, "ffl_pickup_in_store", 0) or 0))


def open_pickup_drafts_for_invoices(names: list[str]) -> dict[str, str]:
	"""``{sales invoice: open draft POS Invoice}`` for many orders in ONE query —
	the Pending Order page asks on every load."""
	return open_drafts_by(LINK_FIELD, names)


def submitted_pickup_invoices(sales_invoice: str) -> list[str]:
	"""The submitted 4473 tickets of one order — the guns already handed over."""
	if not frappe.db.has_column("POS Invoice", LINK_FIELD):
		return []
	return frappe.get_all("POS Invoice",
		filters={LINK_FIELD: sales_invoice, "docstatus": 1}, pluck="name")


def pickup_block(si, pending: list[dict]) -> str | None:
	"""Why a pickup ticket cannot open for this order right now — '' when it can.

	``pending`` is the order's undisposed regulated serials
	(si_firearm_serial_state), computed by the caller because the queue already
	has it in hand.
	"""
	from ffl_core.compliance.firearm import is_awaiting_procurement_row

	if not is_pickup_order(si):
		return _("{0} is not a counter pickup.").format(si.name)
	if si.docstatus != 1:
		return _("{0} is not a submitted invoice.").format(si.name)
	if any(is_awaiting_procurement_row(si, row) for row in si.items):
		return _("The gun ordered to the store has not arrived yet.")
	if not pending:
		return _("No firearm on {0} is waiting to be handed over.").format(si.name)
	# An unpaid balance does not stop the ticket: the 4473 can run while the
	# customer pays, and Complete Order collects what the order still owes. The
	# hand-over itself is gated at submit (require_settled_counter_order).
	return None


def require_settled_counter_order(doc, method: str | None = None) -> None:
	"""POS Invoice before_submit — a counter pickup's gun leaves only against a
	settled order. The ticket carries no money, so the balance lives on the
	order (Sales Invoice) and is collected there: Pending Order → Record
	Payment, or the register's Complete Order balance step, which books the
	same collection (terminal or typed) before re-submitting."""
	from ffl_core.api.manual_order import _is_paid

	link = (getattr(doc, LINK_FIELD, None) or "").strip()
	if not link:
		return
	outstanding = flt(frappe.db.get_value("Sales Invoice", link, "outstanding_amount"))
	if not _is_paid(outstanding):
		frappe.throw(_(
			"{0} still owes {1} — collect the balance on the order before handing "
			"over the gun.").format(link, frappe.format_value(outstanding, {"fieldtype": "Currency"})))


def _ticket_target(si) -> tuple[str | None, list[dict], str | None]:
	"""``(existing draft, pending serials, block)`` — what both openers decide
	on. An existing draft short-circuits: nothing else is computed for it."""
	from ffl_core.reservations import si_firearm_serial_state

	existing = open_pickup_drafts_for_invoices([si.name]).get(si.name)
	if existing:
		return existing, [], None
	pending, _disposed, _rows = si_firearm_serial_state(si)
	return None, pending, pickup_block(si, pending)


@frappe.whitelist()
def open_pickup_invoice(sales_invoice: str) -> dict:
	"""Open (or return) the DRAFT POS Invoice that hands over a counter pickup's
	firearm(s) — the Pending Order page's Pickup button on a counter row.

	Args:
	    sales_invoice: Submitted counter-order Sales Invoice with
	        ``ffl_pickup_in_store`` set and an arrived, undisposed gun.

	Returns:
	    {"ok": True, "pos_invoice": name, "already": bool}.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	frappe.only_for(COUNTER_ROLES)
	si = frappe.get_doc("Sales Invoice", sales_invoice)
	existing, pending, block = _ticket_target(si)
	if existing:
		return {"ok": True, "pos_invoice": existing, "already": True}
	if block:
		frappe.throw(block, title=_("Not Ready for Pickup"))
	name = _open_ticket(si, pending, open_shift(si.company))
	return {"ok": True, "pos_invoice": name, "already": False}


def maybe_open_pickup_invoice(sales_invoice: str) -> str | None:
	"""The arrival path: open the ticket when the order is ready and a register
	is open; otherwise do nothing and let the queue's Pickup button do it later.
	Never throws for a closed register or an unready order — the caller has
	just committed a bound-book acquisition. (A ticket that would not come out
	at $0 still raises, see :func:`_open_ticket`; the caller alerts on that.)"""
	if not frappe.db.exists("Sales Invoice", sales_invoice):
		return None
	si = frappe.get_doc("Sales Invoice", sales_invoice)
	if not is_pickup_order(si):
		return None
	existing, pending, block = _ticket_target(si)
	if existing:
		return existing
	if block:
		return None
	shift = open_shift(si.company, throw=False)
	if not shift:
		return None
	return _open_ticket(si, pending, shift)


def _ticket_items(pending: list[dict]) -> list[dict]:
	items = []
	for p in pending:
		items.append({
			"item_code": p["item_code"], "qty": 1,
			"rate": 0, "price_list_rate": 0, "discount_amount": 0,
			"serial_no": p["serial"], "use_serial_batch_fields": 1,
			"warehouse": frappe.db.get_value("Serial No", p["serial"], "warehouse"),
		})
	return items


def _pin_zero(doc) -> None:
	"""A pickup ticket carries no money. Pricing (`set_missing_values`, a price
	list, a pricing rule) may write a rate back on the way in; re-assert $0 as
	the last word."""
	for row in doc.items:
		row.rate = 0
		row.price_list_rate = 0
		row.discount_amount = 0
		row.discount_percentage = 0
		row.margin_rate_or_amount = 0
		# A rule left on the row makes core's validate re-derive it
		# (set_missing_item_details' pricing_rule_removed branch).
		row.pricing_rules = None


def _is_ticket(doc) -> bool:
	"""A counter pickup ticket — not a return of one, which mirrors whatever the
	ticket carried."""
	return bool((getattr(doc, LINK_FIELD, None) or "").strip()) \
		and not cint(getattr(doc, "is_return", 0) or 0)


# Header money a ticket must not carry. Payments are zeroed row by row; core
# does not recompute paid_amount for a POS Invoice (taxes_and_totals only does
# for a Sales Invoice), and validate_change_amount only fills change while it is
# 0 and never resets it — so all of these are pinned here, not left to core.
_ZERO_HEADER = ("paid_amount", "base_paid_amount", "change_amount", "base_change_amount",
	"write_off_amount", "base_write_off_amount", "write_off_outstanding_amount_automatically",
	"additional_discount_percentage", "discount_amount", "base_discount_amount")


def keep_ticket_free(doc, method: str | None = None) -> None:
	"""POS Invoice before_validate — a pickup ticket stays at $0 on EVERY save.

	The ticket opens at $0, but any later save — the POS page refreshes posting
	time and re-applies the price list when it loads a draft — can re-rate the
	gun and fill a tender for it, and Complete Order then asks for the money a
	second time. This re-asserts $0 whoever saves.

	Hook point: before_validate, first in the list. Frappe runs it on save AND
	submit, before ERPNext's validate (document.run_before_save_methods; the
	controller method runs before doc_events, Document.hook), so core's
	set_missing_values / calculate_taxes_and_totals / validate_change_amount /
	validate_full_payment and every validate hook after it see $0 — the same
	reasoning as compliance/locked_lines. ``ignore_pricing_rule`` keeps core's
	validate from applying a Rate rule or adding a free item behind it;
	:func:`assert_ticket_free` fails closed on anything else.
	"""
	if not _is_ticket(doc):
		return
	_refuse_items_off_the_order(doc)
	doc.ignore_pricing_rule = 1
	_pin_zero(doc)
	for p in doc.get("payments") or []:
		p.amount = 0
		p.base_amount = 0
	for field in _ZERO_HEADER:
		setattr(doc, field, 0)
	doc.calculate_taxes_and_totals()


def _refuse_items_off_the_order(doc) -> None:
	"""A ticket hands over what the order sold. Anything else rung on it would
	be given away (the pin above) and vanish from reporting (the Sales Report
	skips tickets)."""
	link = getattr(doc, LINK_FIELD).strip()
	sold = set(frappe.get_all("Sales Invoice Item",
		filters={"parent": link, "parenttype": "Sales Invoice"}, pluck="item_code"))
	extra = sorted({r.item_code for r in doc.items if r.item_code and r.item_code not in sold})
	if extra:
		frappe.throw(_(
			"{0} is not on order {1}. A pickup ticket hands over what the order "
			"sold; ring other items as a separate sale.").format(", ".join(extra), link),
			title=_("Not on the order"))


def assert_ticket_free(doc, method: str | None = None) -> None:
	"""POS Invoice validate, after ERPNext's own — fail closed: a ticket that
	core's validate put money back on is refused, never saved charging."""
	if not _is_ticket(doc):
		return
	money = [flt(doc.get(f)) for f in ("grand_total", "rounded_total", "paid_amount",
		"change_amount", "write_off_amount")]
	if any(money) or any(flt(r.rate) for r in doc.items):
		frappe.throw(_(
			"Pickup ticket for {0} came out at {1} instead of 0 — the sale is "
			"already on the order.").format(getattr(doc, LINK_FIELD), flt(doc.grand_total)))


def _zero_payment_row(doc) -> None:
	"""One payment row at $0 on the profile's default tender.

	`validate_mode_of_payment` refuses an invoice with no payment row, $0 or
	not. `set_missing_values` has already seeded `payments` from the POS Profile
	(ERPNext's update_multi_mode_option, default flag included), so the tender
	is read off the doc rather than queried again; a profile with no payment
	methods falls back to Cash.
	"""
	default = next((p for p in doc.get("payments") or [] if cint(p.get("default"))), None)
	first = default or ((doc.get("payments") or [None])[0])
	mode = first.mode_of_payment if first else "Cash"
	doc.set("payments", [{"mode_of_payment": mode, "amount": 0}])


def ticket_remarks(sales_invoice: str) -> str:
	"""The ticket's remark, naming the order it hands over — rewritten when an
	edit moves the ticket to a rebuilt invoice (manual_order._carry_arrived_goods)."""
	return _("Counter pickup for order {0}. Paid on that order — do not "
		"collect payment. Run the 4473 here and Complete Order to hand the "
		"firearm over.").format(sales_invoice)


def _open_ticket(si, pending: list[dict], shift) -> str:
	"""Insert the $0 ticket and return its name, or raise having left NOTHING
	behind: the arrival path swallows the raise (the receive is already
	committed), so a half-made ticket would otherwise stay on the order —
	linked, offered as "Open counter order", and blocking Edit / Cancel until
	deleted by hand. The savepoint makes the raise undo the insert."""
	payload = {
		"doctype": "POS Invoice",
		"company": si.company,
		"pos_profile": shift.pos_profile,
		"customer": si.customer,
		# NOT set_posting_time: the ticket opens when the gun ARRIVES and is
		# rung when the customer collects it, days later. Left to ERPNext, the
		# posting date refreshes to the day of the submit, so the stock issue
		# and the consolidated invoice book on the day the gun actually left —
		# the same day as its FastBound 4473 disposition.
		LINK_FIELD: si.name,
		"items": _ticket_items(pending),
		# Pinned empty and re-asserted (set_missing_values_keeping_taxes): the
		# ticket must not pick the profile's tax template up and tax $0 into
		# something else on a later POS-UI touch.
		"taxes_and_charges": "",
		"remarks": ticket_remarks(si.name),
	}
	payload.update(profile_defaults(shift.pos_profile))
	doc = frappe.get_doc(payload)
	doc.flags.ignore_permissions = True
	set_missing_values_keeping_taxes(doc)
	_zero_payment_row(doc)
	savepoint = "counter_pickup_ticket"
	frappe.db.savepoint(savepoint)
	# The insert's own save pins $0 (keep_ticket_free, before_validate) and
	# refuses a ticket core's validate re-priced (assert_ticket_free). Fail
	# CLOSED and leave nothing behind: a ticket that would charge is not a ticket.
	try:
		doc.insert()
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	si.add_comment("Comment", _(
		"Counter pickup ticket {0} opened for serial {1} — run the 4473 there and "
		"Complete Order to hand the firearm over.").format(
		doc.name, ", ".join(p["serial"] for p in pending)))
	return doc.name


def close_counter_order_on_pos_submit(doc, method: str | None = None) -> None:
	"""POS Invoice on_submit — the gun on a counter pickup ticket has left.

	The Sales Invoice now reads that serial as covered (its Retail Sale
	disposition links THIS invoice; si_firearm_serial_state folds it in), so the
	queue no longer offers a pickup for it. When nothing else is owed — no gun
	still to hand over, no stock line still to deliver, no ship-to-store parcel
	still on its way — the accessories' Delivery Note leg is booked and the
	order is stamped cleared, exactly as Mark Picked Up would (it IS Mark Picked
	Up's locked core, manual_order.clear_shipping_leg). Anything left keeps the
	row on the queue for that button. Never fails the POS submit.
	"""
	link = (getattr(doc, LINK_FIELD, None) or "").strip()
	if not link:
		return
	try:
		_clear_if_done(link, doc.name)
	except Exception:  # noqa: BLE001 — the sale is real; the queue can be cleared by hand
		frappe.log_error(title=f"counter pickup: clearing {link} after {doc.name} failed")


def _clear_if_done(sales_invoice: str, pos_invoice: str) -> None:
	from ffl_core.api.manual_order import clear_shipping_leg, ship_arrival_block
	from ffl_core.reservations import si_firearm_serial_state

	si = frappe.get_doc("Sales Invoice", sales_invoice)
	if si.docstatus != 1:
		return
	pending, _disposed, _rows = si_firearm_serial_state(si)
	if pending:
		return  # another gun on the order is still to be handed over
	if ship_arrival_block(si):
		return
	res = clear_shipping_leg(si)
	if not res.get("ok") or res.get("already"):
		return
	notes = res.get("delivery_notes") or []
	si.add_comment("Comment", _("Picked up — firearm handed over on {0}{1}.").format(
		pos_invoice, (", stock lines delivered on " + ", ".join(notes)) if notes else ""))


def reopen_counter_order_on_pos_cancel(doc, method: str | None = None) -> None:
	"""POS Invoice on_cancel — the counterpart of the submit hook above.

	ERPNext puts the gun back in stock when the ticket is cancelled, and with
	the ticket at docstatus 2 the order no longer reads its Retail Sale
	disposition as coverage (si_firearm_serial_state only folds in SUBMITTED
	tickets), so the gun is pending — and held — again. What does not undo
	itself is the "Shipped" stamp the submit hook put on the order: left there,
	the order never returns to the Pending Order queue, and the pickup arm of
	the counter-hold registry never sees it, so the POS picker would offer the
	customer's paid gun to the next walk-in. Lift the stamp when it is ours —
	never when a real ShipStation label went out (that is the shipping half's
	own record). Delivery Notes already booked for the accessories stay; the
	next clear-out skips delivered lines.

	Frappe only lets the ticket cancel once its 4473 disposition is cancelled
	(a submitted link refuses the cancel), so by the time this runs the gun's
	coverage is gone too; a new ticket needs its own — the counter's ⟳ refresh
	or a manager override books it. Deliberate: the gate never trusts a voided
	document's proof.
	"""
	link = (getattr(doc, LINK_FIELD, None) or "").strip()
	if not link:
		return
	stamp = frappe.db.get_value("Sales Invoice", link,
		["shipstation_order_id", "shipstation_status"], as_dict=True)
	if not stamp or (stamp.shipstation_order_id or "").strip():
		return
	if stamp.shipstation_status != "Shipped":
		return
	frappe.db.set_value("Sales Invoice", link, "shipstation_status", "Not Requested",
		update_modified=False)
	si = frappe.get_doc("Sales Invoice", link)
	si.add_comment("Comment", _(
		"Pickup ticket {0} was cancelled — the order is back on the Pending Order "
		"queue with its firearm still to be handed over.").format(doc.name))
