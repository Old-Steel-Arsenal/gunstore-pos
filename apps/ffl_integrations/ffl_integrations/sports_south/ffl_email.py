"""SS Firearm Fulfillment: every firearm order must be followed, within
3 business days, by an email to fulfillment@sportssouth.biz carrying the
transfer dealer's FFL (PDF/JPG), subject = our account number + PO, body =
the transfer dealer's contact name and phone. The FFL number on the
attachment must match the one on the electronic order.

The attachment is whatever File is attached to the destination ATF FFL
Record (the dealer's licence copy). No file → the order is NOT silently
un-emailed: an admin alert + a DO comment say to send it by hand.
"""
from __future__ import annotations

import frappe
from frappe import _

from ffl_integrations.distributor import lanes
from ffl_integrations.notifications import notify_admins

SETTINGS = "Sports South Settings"


def enqueue_ffl_email(do_name: str) -> None:
	frappe.enqueue("ffl_integrations.sports_south.ffl_email.send_ffl_email",
		do_name=do_name, queue="short", job_name=f"ss_ffl_email_{do_name}")


def build_message(do, settings) -> dict:
	"""Pure over the DO + settings: {subject, message, dealer_name, phone}."""
	rec = {}
	if do.transfer_ffl_record:
		rec = frappe.db.get_value("ATF FFL Record", do.transfer_ffl_record,
			["licensee_name", "licensee_dba", "phone", "street", "city", "state", "zip"],
			as_dict=True) or {}
	dealer = rec.get("licensee_dba") or rec.get("licensee_name") or ""
	phone = rec.get("phone") or ""
	subject = f"{settings.customer_number} {do.name}"
	lines = [
		f"Sports South account: {settings.customer_number}",
		f"PO: {do.name}" + (f" (SS web order {do.web_ref})" if do.web_ref else ""),
		"",
		f"Transfer FFL: {do.transfer_ffl_license}",
		f"Transfer dealer: {dealer}",
		f"Contact phone: {phone}",
		f"Premises: {do.transfer_ffl_premise_street}, {do.transfer_ffl_premise_city}, "
		f"{do.transfer_ffl_premise_state} {do.transfer_ffl_premise_zip}",
		"",
		"The attached FFL matches the licence number submitted on the electronic order.",
	]
	return {"subject": subject, "message": "\n".join(lines), "dealer_name": dealer, "phone": phone}


_DOC_EXT = ("pdf", "jpg", "jpeg", "png")
SENT_MARK = "ss_ffl_email"


def _ffl_attachments(record_name: str | None, license_no: str | None) -> list:
	"""The dealer's licence copy: a File on the ATF FFL Record, else the
	``ffl_document`` Attach on the Supplier / Customer whose ffl_number is the
	transfer licence (where the counter's dealer-transfer flow stores it)."""
	out = []
	if record_name:
		for f in frappe.get_all("File", filters={
				"attached_to_doctype": "ATF FFL Record", "attached_to_name": record_name},
				fields=["file_url", "file_name"], order_by="creation desc"):
			if (f.file_name or "").lower().rsplit(".", 1)[-1] in _DOC_EXT:
				out.append({"file_url": f.file_url})
	if out or not license_no:
		return out
	for dt in ("Supplier", "Customer"):
		if not frappe.get_meta(dt).get_field("ffl_document"):
			continue
		url = frappe.db.get_value(dt, {"ffl_number": license_no}, "ffl_document")
		if url:
			return [{"file_url": url}]
	return out


def _already_sent(do) -> bool:
	return SENT_MARK in {s for s in str(do.get("notes_posted") or "").split(",") if s}


def _mark_sent(do) -> None:
	marks = {s for s in str(do.get("notes_posted") or "").split(",") if s}
	marks.add(SENT_MARK)
	frappe.db.set_value("Distributor Order", do.name, "notes_posted", ",".join(sorted(marks)),
		update_modified=False)


def send_ffl_email(do_name: str) -> dict:
	do = frappe.get_doc("Distributor Order", do_name)
	s = frappe.get_cached_doc(SETTINGS)
	if lanes.mode_of(do.order_type) != lanes.FDS:
		return {"sent": False, "reason": "not a firearm order"}
	if _already_sent(do):
		return {"sent": False, "reason": "already sent"}
	to = (s.fulfillment_email or "").strip()
	if not to:
		notify_admins(f"Sports South FFL email not sent: {do.name}",
			"Sports South Settings has no fulfillment email address.",
			dedup_key=f"ss_ffl_mail_cfg_{do.name}")
		return {"sent": False, "reason": "no recipient configured"}
	attachments = _ffl_attachments(do.transfer_ffl_record, do.transfer_ffl_license)
	if not attachments:
		msg = _("No FFL copy (PDF/JPG) is attached to ATF FFL Record {0}. Email it to {1} "
			"by hand within 3 business days — subject \"{2} {3}\" — or Sports South may "
			"cancel the order.").format(do.transfer_ffl_record or do.transfer_ffl_license,
			to, s.customer_number, do.name)
		do.add_comment("Comment", msg)
		notify_admins(f"Sports South FFL email needs a licence copy: {do.name}", msg,
			dedup_key=f"ss_ffl_mail_nofile_{do.name}")
		return {"sent": False, "reason": "no ffl attachment"}
	m = build_message(do, s)
	frappe.sendmail(
		recipients=[to],
		cc=[s.order_email_cc] if (s.order_email_cc or "").strip() else None,
		subject=m["subject"], message=m["message"].replace("\n", "<br>"),
		attachments=attachments,
		reference_doctype="Distributor Order", reference_name=do.name,
		now=bool(frappe.flags.in_test),
	)
	_mark_sent(do)
	do.add_comment("Comment", _("FFL copy emailed to {0} (subject \"{1}\").").format(to, m["subject"]))
	return {"sent": True, "to": to, "subject": m["subject"]}
