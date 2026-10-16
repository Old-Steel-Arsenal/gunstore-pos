"""Waivers and agreements: request, sign, paper copies, status, and the sale gate.

An **Agreement Template** holds the text (a range waiver, a rental agreement); a
**Customer Agreement** is one customer's copy of it. The copy is written only here.

* **Request** — :func:`request_agreement` (counter) creates the customer's Pending
  copy, or re-issues the one already pending (the old link stops working), with the
  text rendered for this customer and frozen on the copy (``body_snapshot``). It
  hands out a one-time signing link: shown as a QR code at the counter (the store's
  tablet or the customer's phone scans it) or emailed to the customer. The link's
  token is 256 random bits; only its sha256 is stored, and it works for
  :data:`TOKEN_HOURS`.
* **Sign** — the page ``/agreement/sign#t=<token>`` (``www/agreement/sign.*``, the
  token in the fragment so it never reaches a log or a Referer) calls the two Guest
  endpoints :func:`load_for_signing` and :func:`submit_signature`. They answer only
  for a valid token, return only the customer's name, the title and the text, and
  give one and the same error for every bad token. The signature is a PNG checked
  for size, type and ink; it and the signed PDF are private files. After the
  signature commits a background job builds the PDF and, when the customer has an
  email address, sends it to them.
* **Minors** — on a template with ``minors_need_guardian``, a participant under 18
  is signed for by a parent or guardian. The age comes from the customer's date of
  birth when the store keeps one (FFL Settings → Store Customer ID Numbers);
  otherwise the signer declares it on the page.
* **Paper** — a manager can record a copy signed on paper
  (:func:`record_paper_agreement`): the scan, attached to the copy, is made private
  and the copy becomes Signed via Paper. No one can waive a missing signature.
* **Status** — :func:`agreement_status` (signed / expiring / expired / pending /
  missing) feeds the counter panel (:func:`counter_panel_rows`, the ``agreements``
  key of ``customer_flags.get_customer_flags``); :func:`require_agreement` raises
  :class:`AgreementRequired` for an extension that needs a signature first.
  :func:`expire_agreements` (daily) turns copies past ``expires_on`` to Expired.
* **Sale gate** — an Item can name the agreement its buyer must have signed
  (``Item.ffl_required_agreement``, ``setup/agreements.py``), and extensions can add
  more through the ``gunshop_required_agreements`` hook. :func:`refuse_missing_required`
  refuses to submit a counter sale (POS Invoice / Sales Invoice) until the customer
  has signed them. Returns, POS consolidation invoices and web / GunBroker sales
  booked after the fact are left alone.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re
import secrets
from datetime import timedelta

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import add_days, cint, escape_html, formatdate, get_datetime, get_url, getdate, \
	now_datetime, nowdate
from frappe.utils.html_utils import sanitize_html

from ffl_core.compliance.channel_invoice import is_channel_mirrored_invoice
from ffl_core.roles import COUNTER_ROLES, CUSTOMER_MANAGER_ROLES
from ffl_core.store_identity import company_brand
from ffl_core.utils import atomic, commit, guest_job_elevated, set_file_privacy

DOCTYPE = "Customer Agreement"
TEMPLATE = "Agreement Template"
TOKEN_HOURS = 72
EXPIRING_DAYS = 30
ADULT_AGE = 18
# A data-URL PNG of a 1200x400 signature is ~20-60 KB; these are the hard ceilings.
MAX_SIGNATURE_CHARS = 700_000
MAX_SIGNATURE_BYTES = 500 * 1024
MAX_SIGNATURE_PX = 2000
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_LINK_EMAIL = "Agreement Signing Link"
_COPY_EMAIL = "Agreement Signed Copy"
_PDF_TEMPLATE = "templates/agreement/agreement_pdf.html"
# Every invalid token gets this, whatever the reason (the reason goes to the log).
INVALID_LINK = "This signing link is not valid any more. Ask the store for a new one."
DELIVERIES = ("qr", "email", "none")
RELATIONSHIPS = ("Self", "Parent", "Guardian")


class AgreementRequired(frappe.ValidationError):
	"""The customer has not signed an agreement this action needs."""


def _log():
	return frappe.logger("agreements")


def _hash(token: str) -> str:
	return hashlib.sha256(token.encode()).hexdigest()


def _is_manager() -> bool:
	return bool(set(frappe.get_roles()) & set(CUSTOMER_MANAGER_ROLES))


def _default_company() -> str | None:
	return frappe.defaults.get_user_default("Company") or frappe.defaults.get_global_default("company")


def _age_on(dob, on) -> int:
	dob, on = getdate(dob), getdate(on)
	return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def _expires_on(template, signed_day):
	days = cint(frappe.db.get_value(TEMPLATE, template, "valid_days"))
	return add_days(getdate(signed_day), days) if days > 0 else None


def render_body(template_doc, customer_doc, company: str | None) -> str:
	"""The template's text for this customer, placeholders filled, sanitized.

	Only the documented placeholders are in the context, each value HTML-escaped.
	"""
	brand = company_brand(company)
	name = customer_doc.customer_name or customer_doc.name
	first = (customer_doc.get("ffl_first_name") or "").strip() or name.split(" ")[0]
	ctx = {k: escape_html(str(v)) for k, v in {
		"customer_name": name, "first_name": first, "date": formatdate(nowdate()),
		"store_name": brand.get("name") or "", "store_address": brand.get("address_line") or "",
		"valid_days": template_doc.valid_days or "",
	}.items()}
	return sanitize_html(frappe.render_template(template_doc.body or "", ctx))


# ------------------------------------------------------------------ status


def agreement_status(customer: str, template: str, today=None) -> dict:
	"""Where *customer* stands with *template* today.

	Returns:
	    {"template", "state", "expires_on", "agreement"} where state is
	    ``signed`` (valid, not expiring within 30 days), ``expiring``, ``expired``
	    (the last signed copy ran out), ``pending`` (sent, not signed) or
	    ``missing``. ``agreement`` is the copy the state comes from.
	"""
	today = getdate(today or nowdate())
	rows = frappe.get_all(DOCTYPE, filters={"customer": customer, "template": template,
		"status": ("in", ("Signed", "Pending", "Expired"))},
		fields=["name", "status", "expires_on"], order_by="creation desc")
	valid = [r for r in rows if r.status == "Signed" and (not r.expires_on or getdate(r.expires_on) >= today)]
	out = {"template": template, "state": "missing", "expires_on": None, "agreement": None}
	if valid:
		# Never-expiring first, then the one that lasts longest.
		best = max(valid, key=lambda r: (r.expires_on is None, getdate(r.expires_on) if r.expires_on else today))
		soon = best.expires_on and getdate(best.expires_on) <= getdate(add_days(today, EXPIRING_DAYS))
		out.update(state="expiring" if soon else "signed", agreement=best.name,
			expires_on=str(best.expires_on) if best.expires_on else None)
		return out
	pending = next((r for r in rows if r.status == "Pending"), None)
	if pending:
		out.update(state="pending", agreement=pending.name)
		return out
	lapsed = next((r for r in rows if r.status in ("Signed", "Expired")), None)
	if lapsed:
		out.update(state="expired", agreement=lapsed.name,
			expires_on=str(lapsed.expires_on) if lapsed.expires_on else None)
	return out


def require_agreement(customer: str, template: str) -> None:
	"""Raise :class:`AgreementRequired` unless *customer* holds a valid signed *template*."""
	if agreement_status(customer, template)["state"] not in ("signed", "expiring"):
		name = frappe.db.get_value("Customer", customer, "customer_name") or customer
		frappe.throw(_("{0} has not signed {1}.").format(frappe.bold(escape_html(name)),
			frappe.bold(escape_html(template))), AgreementRequired, title=_("Agreement needed"))


def counter_panel_rows(customer: str) -> list[dict]:
	"""Status of every active template marked "Show on Counter Panel"."""
	templates = frappe.get_all(TEMPLATE, filters={"active": 1, "show_on_counter_panel": 1},
		pluck="name", order_by="title asc")
	return [agreement_status(customer, t) for t in templates]


def required_templates(doc) -> list[str]:
	"""Active templates a sale's customer must have signed: the Items' own
	``ffl_required_agreement`` plus whatever ``gunshop_required_agreements``
	providers name. A provider that fails is logged and adds nothing."""
	codes = list({r.item_code for r in doc.get("items") or [] if r.get("item_code")})
	names = []
	if codes and frappe.db.has_column("Item", "ffl_required_agreement"):
		names += frappe.get_all("Item", filters={"name": ("in", codes), "ffl_required_agreement": ("is", "set")},
			pluck="ffl_required_agreement")
	for hook in frappe.get_hooks("gunshop_required_agreements"):
		try:
			names += [n for n in (frappe.get_attr(hook)(doc) or []) if isinstance(n, str)]
		except Exception:
			frappe.log_error(title=f"gunshop_required_agreements provider failed: {hook}")
	names = list(dict.fromkeys(names))
	if not names:
		return []
	active = set(frappe.get_all(TEMPLATE, filters={"name": ("in", names), "active": 1}, pluck="name"))
	return [n for n in names if n in active]


def refuse_missing_required(doc, method=None) -> None:
	"""POS Invoice / Sales Invoice before_submit: the customer must have signed what the sale needs.

	There is no override: the customer signs (Request agreement on the customer),
	or a manager records a copy signed on paper.
	"""
	customer = doc.get("customer")
	if not customer or doc.get("is_return") or doc.get("is_consolidated"):
		return
	if frappe.flags.in_import or frappe.flags.in_patch or frappe.flags.in_migrate:
		return
	if is_channel_mirrored_invoice(doc):
		return
	missing = [t for t in required_templates(doc)
		if agreement_status(customer, t)["state"] not in ("signed", "expiring")]
	if not missing:
		return
	name = frappe.db.get_value("Customer", customer, "customer_name") or customer
	frappe.throw(_("{0} must sign {1} before this sale. At the register, click the agreement under "
		"the customer's name (or Request agreement on the customer) to show a QR code or email a link; "
		"or a manager records a copy signed on paper.").format(
		frappe.bold(escape_html(name)), ", ".join(frappe.bold(escape_html(t)) for t in missing)),
		AgreementRequired, title=_("Agreement needed"))


# ------------------------------------------------------------------ staff side


def _qr_data_uri(url: str) -> str:
	import pyqrcode

	buf = io.BytesIO()
	pyqrcode.create(url, error="M").svg(buf, scale=6, background="#ffffff", xmldecl=False, svgns=True)
	return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


@frappe.whitelist(methods=["POST"])
def request_agreement(customer: str, template: str, deliver: str = "qr") -> dict:
	"""Send a customer an agreement to sign.

	Creates the customer's Pending copy of the template, or re-issues the one
	already pending (its old link stops working and its text is rendered again).

	Args:
	    customer: Customer name (document id).
	    template: Agreement Template name; must be active.
	    deliver: ``qr`` — return the signing link and a QR code for the counter;
	        ``email`` — email the link to the customer (refused when the customer
	        has no email address); ``none`` — only create the Pending copy (a
	        manager then records a paper copy on it).

	Returns:
	    {"name": the Customer Agreement, "deliver", "token_expires"} plus, for
	    ``qr``, "sign_url" and "qr_svg" (an SVG data URI); for ``email``, "email".
	    The link itself is never stored.
	"""
	frappe.only_for(COUNTER_ROLES)
	frappe.has_permission("Customer", "read", doc=customer, throw=True)
	if deliver not in DELIVERIES:
		frappe.throw(_("Unknown delivery: {0}").format(escape_html(deliver)))
	tpl = frappe.get_doc(TEMPLATE, template)
	if not tpl.active:
		frappe.throw(_("{0} is switched off.").format(escape_html(template)))
	cust = frappe.get_doc("Customer", customer)
	email = (cust.get("email_id") or "").strip()
	if deliver == "email" and not email:
		frappe.throw(_("{0} has no email address. Show the QR code instead, or add an email to the customer.")
			.format(escape_html(cust.customer_name)), title=_("No email address"))
	token = secrets.token_urlsafe(32) if deliver != "none" else None
	expires = now_datetime() + timedelta(hours=TOKEN_HOURS) if token else None
	company = _default_company()
	with atomic("request agreement"):
		existing = frappe.db.get_value(DOCTYPE, {"customer": customer, "template": template,
			"status": "Pending"}, "name", for_update=True)
		doc = frappe.get_doc(DOCTYPE, existing) if existing else frappe.new_doc(DOCTYPE)
		doc.update({
			"customer": customer, "template": template, "template_version": tpl.version or 1,
			"company": company, "status": "Pending",
			"body_snapshot": render_body(tpl, cust, company),
			"sign_token_hash": _hash(token) if token else None, "token_expires": expires,
			"delivery_channel": {"qr": "QR", "email": "Email"}.get(deliver),
			"requested_by": frappe.session.user,
		})
		doc.flags.via_agreements = True
		doc.save(ignore_permissions=True)
	out = {"name": doc.name, "deliver": deliver, "token_expires": str(expires) if expires else None}
	if not token:
		return out
	url = get_url("/agreement/sign#t=" + token)
	if deliver == "qr":
		out.update(sign_url=url, qr_svg=_qr_data_uri(url))
		return out
	_send_link_email(doc, cust, email, url)
	out["email"] = email
	return out


def _send_link_email(doc, cust, email: str, url: str) -> None:
	tpl = frappe.get_doc("Email Template", _LINK_EMAIL)
	ctx = {"customer_name": cust.customer_name, "title": doc.template, "sign_url": url,
		"store_name": company_brand(doc.company).get("name") or "", "hours": TOKEN_HOURS}
	frappe.sendmail(recipients=[email], subject=frappe.render_template(tpl.subject, ctx),
		message=frappe.render_template(tpl.response_html if tpl.use_html else tpl.response, ctx),
		reference_doctype=DOCTYPE, reference_name=doc.name, now=False)


@frappe.whitelist(methods=["POST"])
def void_agreement(name: str, reason: str) -> dict:
	"""Manager voids a copy (sent by mistake, signed by the wrong person).

	A Pending copy's link stops working. The copy is kept, marked Void, and the
	reason goes on its timeline.

	Args:
	    name: Customer Agreement name.
	    reason: Why (required).

	Returns:
	    {"name", "status": "Void"}.
	"""
	frappe.only_for(CUSTOMER_MANAGER_ROLES)
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Enter a reason."), title=_("Reason required"))
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.status == "Void":
		frappe.throw(_("{0} is already void.").format(name))
	doc.update({"status": "Void", "void_reason": reason, "sign_token_hash": None, "token_expires": None})
	doc.flags.via_agreements = True
	doc.save(ignore_permissions=True)
	doc.add_comment("Comment", _("Voided by {0}: {1}").format(escape_html(frappe.session.user), escape_html(reason)))
	return {"name": doc.name, "status": doc.status}


@frappe.whitelist(methods=["POST"])
def record_paper_agreement(name: str, file_url: str, signer_name: str, paper_signed_on: str,
		participant_is_minor: int = 0, signer_relationship: str = "Self") -> dict:
	"""Manager records a copy the customer signed on paper.

	The scan must already be attached to this Customer Agreement; it is made
	private and becomes the signed copy. The copy turns Signed via Paper, its
	expiry counts from the date on the paper, and who recorded it, when, and the
	file go on the timeline. A missing signature can never be waived: without a
	signed scan there is nothing to record.

	Args:
	    name: The Pending Customer Agreement.
	    file_url: The attached scan (PDF or image).
	    signer_name: Name as signed on the paper.
	    paper_signed_on: Date written on the paper (YYYY-MM-DD), not in the future.
	    participant_is_minor: 1 when the participant is under 18 (then a Parent or
	        Guardian signed).
	    signer_relationship: Self, Parent or Guardian.

	Returns:
	    {"name", "status", "expires_on", "signed_pdf"}.
	"""
	frappe.only_for(CUSTOMER_MANAGER_ROLES)
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.status != "Pending":
		frappe.throw(_("Only a Pending agreement can take a paper copy ({0} is {1}).").format(name, doc.status))
	signer_name = (signer_name or "").strip()
	if not signer_name or len(signer_name) > 140:
		frappe.throw(_("Enter the name signed on the paper."))
	day = getdate(paper_signed_on) if paper_signed_on else None
	if not day or day > getdate(nowdate()):
		frappe.throw(_("Enter the date written on the paper (not in the future)."))
	minor = cint(participant_is_minor)
	if signer_relationship not in RELATIONSHIPS or (minor and signer_relationship == "Self"):
		frappe.throw(_("A participant under 18 is signed for by a Parent or Guardian."))
	url = (file_url or "").strip()
	attached = url and frappe.db.exists("File", {"file_url": url, "attached_to_doctype": DOCTYPE,
		"attached_to_name": name})
	if not attached:
		frappe.throw(_("Attach the signed paper to this agreement first."), title=_("Scan needed"))
	private_url = set_file_privacy(url, private=True)
	if not private_url or not private_url.startswith("/private/"):
		frappe.throw(_("The scan could not be made private."))
	with atomic("record paper agreement"):
		doc.update({
			"status": "Signed", "signed_via": "Paper", "signed_on": get_datetime(day),
			"paper_signed_on": day, "signer_name": signer_name, "signer_relationship": signer_relationship,
			"participant_is_minor": minor, "minor_determined_by": "Paper" if minor else None,
			"signed_pdf": private_url, "recorded_by": frappe.session.user,
			"expires_on": _expires_on(doc.template, day),
			"sign_token_hash": None, "token_expires": None,
		})
		doc.flags.via_agreements = True
		doc.save(ignore_permissions=True)
		doc.add_comment("Comment", _("Paper copy recorded by {0} on {1}: {2}, signed {3} by {4}.").format(
			escape_html(frappe.session.user), now_datetime().strftime("%Y-%m-%d %H:%M"),
			escape_html(url.rsplit("/", 1)[-1]), day, escape_html(signer_name)))
	return {"name": doc.name, "status": doc.status, "expires_on": str(doc.expires_on or ""),
		"signed_pdf": doc.signed_pdf}


@frappe.whitelist()
def get_customer_agreements(customer: str) -> dict:
	"""The Agreements section of the Customer form.

	Args:
	    customer: Customer name (document id).

	Returns:
	    {"rows": [{"name", "template", "status", "signed_on", "signed_via",
	    "expires_on", "signed_pdf"}] newest first, "can_manage": whether the caller
	    may void or record paper copies}.
	"""
	frappe.has_permission("Customer", "read", doc=customer, throw=True)
	rows = frappe.get_all(DOCTYPE, filters={"customer": customer},
		fields=["name", "template", "status", "signed_on", "signed_via", "expires_on", "signed_pdf"],
		order_by="creation desc", limit=50) if frappe.has_permission(DOCTYPE, "read") else []
	return {"rows": [{**r, "signed_on": str(r.signed_on or ""), "expires_on": str(r.expires_on or "")}
		for r in rows], "can_manage": _is_manager()}


@frappe.whitelist(methods=["POST"])
def regenerate_signed_pdf(name: str) -> dict:
	"""Build a signed copy's PDF again (when the background job failed).

	Args:
	    name: A Customer Agreement signed on a tablet or by link.

	Returns:
	    {"signed_pdf": file URL}.
	"""
	frappe.only_for(COUNTER_ROLES)
	frappe.has_permission(DOCTYPE, "read", doc=name, throw=True)
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.status not in ("Signed", "Expired") or doc.signed_via == "Paper" or not doc.signature:
		frappe.throw(_("Only an agreement signed on a tablet or by link has a generated PDF."))
	_attach_pdf(doc)
	return {"signed_pdf": doc.signed_pdf}


# ------------------------------------------------------------------ signing (Guest)


def _by_token(token, for_update: bool = False):
	"""The Pending copy this token opens, or throw :data:`INVALID_LINK`."""
	reason = None
	doc = None
	if not isinstance(token, str) or not _TOKEN_RE.match(token):
		reason = "malformed"
	else:
		name = frappe.db.get_value(DOCTYPE, {"sign_token_hash": _hash(token)}, "name", for_update=for_update)
		if not name:
			reason = "unknown"
		else:
			doc = frappe.get_doc(DOCTYPE, name)
			if doc.status != "Pending":
				reason = f"{name} is {doc.status}"
			elif not doc.token_expires or get_datetime(doc.token_expires) < now_datetime():
				reason = f"{name} link expired"
			elif not frappe.db.get_value(TEMPLATE, doc.template, "active"):
				reason = f"{name} template off"
	if reason:
		_log().info(f"refused signing link from {_request_ip()}: {reason}")
		frappe.throw(_(INVALID_LINK), title=_("Link not valid"))
	return doc


def _request_ip() -> str | None:
	return getattr(frappe.local, "request_ip", None)


def _user_agent() -> str:
	request = getattr(frappe.local, "request", None)
	return ((request.headers.get("User-Agent") if request else "") or "")[:500]


def _minor_by_dob(doc) -> bool | None:
	"""True/False from the customer's date of birth, None when none is kept."""
	dob = frappe.db.get_value("Customer", doc.customer, "date_of_birth") \
		if frappe.db.has_column("Customer", "date_of_birth") else None
	return _age_on(dob, nowdate()) < ADULT_AGE if dob else None


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=30, seconds=3600)
def load_for_signing(token: str) -> dict:
	"""What the signing page shows for a one-time link (Guest).

	Only the customer's name, the agreement's title and text, and the store's
	name — nothing else about the customer. Every invalid, used, void or
	expired link gets the same error. 30 calls an hour per IP address.

	Args:
	    token: The token from the link's ``#t=`` fragment.

	Returns:
	    {"customer_name", "title", "body_html", "store_name",
	    "minors_need_guardian", "minor_by_dob": true / false from the customer's
	    date of birth, or null when the store keeps none (the page then asks)}.
	"""
	doc = _by_token(token)
	needs_guardian = bool(frappe.db.get_value(TEMPLATE, doc.template, "minors_need_guardian"))
	return {
		"customer_name": frappe.db.get_value("Customer", doc.customer, "customer_name") or doc.customer_name,
		"title": doc.template,
		"body_html": sanitize_html(doc.body_snapshot or ""),
		"store_name": company_brand(doc.company).get("name") or "",
		"minors_need_guardian": needs_guardian,
		"minor_by_dob": _minor_by_dob(doc) if needs_guardian else None,
	}


def _decode_signature(signature) -> bytes:
	"""The PNG bytes of a data-URL signature, or throw. Cheap checks first."""
	if not isinstance(signature, str) or len(signature) > MAX_SIGNATURE_CHARS:
		frappe.throw(_("The signature is too large."))
	raw = signature.split(",", 1)[1] if signature.startswith("data:") else signature
	try:
		data = base64.b64decode(raw, validate=True)
	except (binascii.Error, ValueError):
		frappe.throw(_("The signature could not be read."))
	if len(data) > MAX_SIGNATURE_BYTES:
		frappe.throw(_("The signature is too large."))
	if not data.startswith(_PNG_MAGIC):
		frappe.throw(_("The signature could not be read."))
	from PIL import Image, ImageOps

	try:
		with Image.open(io.BytesIO(data)) as img:
			if img.format != "PNG" or max(img.size) > MAX_SIGNATURE_PX:
				raise ValueError("format/size")
			img.verify()
		with Image.open(io.BytesIO(data)) as img:
			img.load()
			flat = Image.new("RGBA", img.size, "white")
			flat.alpha_composite(img.convert("RGBA"))
			ink = ImageOps.invert(flat.convert("L")).point(lambda v: 255 if v > 48 else 0).getbbox()
	except Exception:
		frappe.throw(_("The signature could not be read."))
	if not ink:
		frappe.throw(_("Please sign in the box."))
	return data


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=10, seconds=3600)
def submit_signature(token: str, signature: str, agreed: int, signer_name: str,
		participant_is_minor: int = 0, signer_relationship: str = "Self") -> dict:
	"""Sign an agreement through a one-time link (Guest).

	Checked before anything is written: the "I agree" tick, the typed name, the
	token (same error for every invalid one), the signature (a PNG of at most
	500 KB and 2000 px with ink on it) and the age rule. A link opened as a QR
	code at the counter is recorded as signed on a Tablet, an emailed one as
	Remote. The link is used up; the signed PDF is built and emailed afterwards.
	10 calls an hour per IP address.

	Args:
	    token: The token from the link's ``#t=`` fragment.
	    signature: The drawn signature as a PNG data URL.
	    agreed: 1 — the signer ticked "I agree".
	    signer_name: Name typed by the signer.
	    participant_is_minor: 1 when the signer declares the participant is under
	        18 (ignored when the store keeps the customer's date of birth).
	    signer_relationship: Self, or Parent / Guardian when signing for a minor.

	Returns:
	    {"signed": true, "store_name"}.
	"""
	if not cint(agreed):
		frappe.throw(_("Tick \"I agree\" to sign."))
	signer_name = (signer_name or "").strip() if isinstance(signer_name, str) else ""
	if not signer_name or len(signer_name) > 140:
		frappe.throw(_("Type your full name."))
	if not isinstance(signature, str) or len(signature) > MAX_SIGNATURE_CHARS:
		frappe.throw(_("The signature is too large."))
	doc = _by_token(token, for_update=True)
	png = _decode_signature(signature)
	needs_guardian = bool(frappe.db.get_value(TEMPLATE, doc.template, "minors_need_guardian"))
	minor, determined = 0, None
	if needs_guardian:
		by_dob = _minor_by_dob(doc)
		if by_dob is None:
			minor, determined = cint(participant_is_minor), "Self-declared"
		else:
			minor, determined = int(by_dob), "Date of Birth"
	if signer_relationship not in RELATIONSHIPS:
		frappe.throw(_("Choose who is signing."))
	if minor and signer_relationship == "Self":
		frappe.throw(_("The participant is under 18: a parent or guardian must sign, with their own name."))
	via = "Remote" if doc.delivery_channel == "Email" and frappe.session.user == "Guest" else "Tablet"
	today = nowdate()
	with guest_job_elevated(), atomic("sign agreement"):
		f = frappe.get_doc({"doctype": "File", "file_name": f"{doc.name}-signature.png", "is_private": 1,
			"attached_to_doctype": DOCTYPE, "attached_to_name": doc.name, "attached_to_field": "signature",
			"content": png})
		f.flags.ignore_permissions = True
		f.insert()
		doc.update({
			"status": "Signed", "signed_on": now_datetime(), "signed_via": via,
			"signer_name": signer_name, "signer_relationship": signer_relationship,
			"participant_is_minor": minor, "minor_determined_by": determined if minor else None,
			"signature": f.file_url, "expires_on": _expires_on(doc.template, today),
			"signer_ip": _request_ip(),
			"signer_user_agent": _user_agent(),
			"sign_token_hash": None, "token_expires": None,
		})
		doc.flags.via_agreements = True
		doc.save(ignore_permissions=True)
	frappe.enqueue("ffl_core.agreements.finish_signed_agreement", queue="short",
		enqueue_after_commit=True, name=doc.name)
	return {"signed": True, "store_name": company_brand(doc.company).get("name") or ""}


# ------------------------------------------------------------------ after signing


def _pdf_bytes(doc) -> bytes:
	from frappe.utils.pdf import get_pdf

	sig = frappe.get_doc("File", {"file_url": doc.signature}).get_content() if doc.signature else b""
	html = frappe.render_template(_PDF_TEMPLATE, {
		"doc": doc, "brand": company_brand(doc.company), "body_html": sanitize_html(doc.body_snapshot or ""),
		"signature_uri": "data:image/png;base64," + base64.b64encode(sig).decode() if sig else "",
		"signed_on": str(get_datetime(doc.signed_on).strftime("%Y-%m-%d %H:%M")) if doc.signed_on else "",
	})
	return get_pdf(html)


def _attach_pdf(doc) -> bytes:
	pdf = _pdf_bytes(doc)
	f = frappe.get_doc({"doctype": "File", "file_name": f"{doc.name}.pdf", "is_private": 1,
		"attached_to_doctype": DOCTYPE, "attached_to_name": doc.name, "attached_to_field": "signed_pdf",
		"content": pdf})
	f.flags.ignore_permissions = True
	f.insert()
	doc.db_set("signed_pdf", f.file_url, update_modified=False)
	return pdf


def finish_signed_agreement(name: str) -> None:
	"""Background, after a signature commits: attach the PDF, email it when the
	customer has an address. A failure is logged; the signature stands and the
	form's Regenerate PDF builds it again."""
	with guest_job_elevated():
		doc = frappe.get_doc(DOCTYPE, name)
		try:
			pdf = _attach_pdf(doc)
		except Exception:
			frappe.log_error(title=f"Agreement PDF failed: {name}")
			return
		# The PDF stands even when the email cannot go (no outgoing account, bad address).
		commit()
		email = (frappe.db.get_value("Customer", doc.customer, "email_id") or "").strip()
		if not email:
			return
		try:
			tpl = frappe.get_doc("Email Template", _COPY_EMAIL)
			ctx = {"customer_name": doc.customer_name, "title": doc.template,
				"store_name": company_brand(doc.company).get("name") or ""}
			frappe.sendmail(recipients=[email], subject=frappe.render_template(tpl.subject, ctx),
				message=frappe.render_template(tpl.response_html if tpl.use_html else tpl.response, ctx),
				reference_doctype=DOCTYPE, reference_name=name,
				attachments=[{"fname": f"{name}.pdf", "fcontent": pdf}], now=False)
		except Exception:
			frappe.log_error(title=f"Agreement signed copy email failed: {name}")


def expire_agreements() -> None:
	"""Daily: signed copies past ``expires_on`` become Expired."""
	# "is set" too: frappe compares a blank date as the earliest one (ifnull), which
	# would expire every never-expiring copy.
	for name in frappe.get_all(DOCTYPE, filters=[["status", "=", "Signed"], ["expires_on", "is", "set"],
			["expires_on", "<", nowdate()]], pluck="name"):
		frappe.db.set_value(DOCTYPE, name, "status", "Expired")
