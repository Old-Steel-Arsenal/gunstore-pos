"""GunBroker Settings — credentials, listing defaults and settlement accounts.

Read by ffl_integrations.gunbroker.client (GunBrokerConfig.from_settings) and by
the listing payload builder. Everything here is per-site: a second store runs
its own GunBroker account on its own Single.

`validate` is deliberately almost all msgprint and one throw. The msgprints
describe a configuration that will not do what the operator expects; the throw
covers the one setting whose absence makes every payment on the only live
payment route fail, where letting the form save just moves the failure to a
worse moment.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from ffl_core.utils import single_raw

SETTINGS_DOCTYPE = "GunBroker Settings"

# The only two Auto-Relist values that mean anything on a fixed-price listing.
VALID_AUTO_RELIST = ("1 Do Not Relist", "4 Relist Fixed Price")

# Clearing accounts, by their name WITHOUT the company abbreviation. These are
# Bank-type and so appear in the same picker as the real bank account, which is
# exactly why they have to be named and refused rather than trusted to a rule in
# a checklist.
CLEARING_ACCOUNT_STEMS = ("Payroc Card Clearing", "Woo Clearing", "GunBroker Clearing")


def _clearing_accounts(company: str | None) -> set[str]:
	"""This company's clearing-account names, built from its own abbreviation.

	Read from the Company rather than hard-coded as "… - <ABBR>", so the guard still
	works on the second store — where the same clearing accounts exist under a
	different suffix and the same mistake is available to make.
	"""
	if not company:
		return set()
	abbr = frappe.db.get_value("Company", company, "abbr") or ""
	if not abbr:
		return set()
	return {f"{stem} - {abbr}" for stem in CLEARING_ACCOUNT_STEMS}


# GunBroker sends the password in an HTTP Basic header on a URL anyone can reach; a
# short one is a guess away from letting a stranger nudge our syncs.
WEBHOOK_PASSWORD_MIN = 12


class GunBrokerSettings(Document):
	def onload(self):
		"""The five webhook URLs for this site, for the form to show (see the .js)."""
		from ffl_integrations.gunbroker.webhooks import webhook_urls

		self.set_onload("webhook_urls", webhook_urls())

	def validate(self):
		self._check_webhook_credentials()
		self._warn_about_environment()
		self._check_category_rows()
		self._warn_about_shipping_costs()
		self._warn_about_end_strategy()
		self._warn_about_auto_relist()
		self._enforce_card_switch()
		self._require_deposit_account()
		self._warn_about_insecure_footer()

	def on_update(self):
		"""Point the two GunBroker Modes of Payment at the accounts named here.

		Without this the accounts on this form are decorative. The seed that
		creates and re-points those modes runs on install and on migrate, and
		nothing else calls it — so on the ordinary go-live order (migrate an
		empty site, then fill this form in) the cheque mode is never created at
		all: `GunBroker Check` has no account row, and the first Payment Entry
		throws with the gun already shipped and the cheque already banked. The
		same gap makes a later change of deposit account silent — every receipt
		keeps landing in the old one until somebody happens to migrate.

		Idempotent, and never raises: this is a settings form, and failing to
		save it because a chart of accounts is half built would be a worse
		outcome than the modes being one save behind.
		"""
		self._sync_modes_of_payment()

	def _sync_modes_of_payment(self):
		from ffl_integrations.gunbroker import setup as gb_setup

		for company in frappe.get_all("Company", pluck="name"):
			try:
				gb_setup.ensure_modes_of_payment(company)
			except Exception:  # noqa: BLE001 — never block saving the form
				frappe.log_error(
					title=f"GunBroker modes of payment not synced for {company}",
					message=frappe.get_traceback())

	# ------------------------------------------------------------------ checks

	def _warn_about_environment(self):
		"""Three states, three different messages.

		The stored value has three meanings, not two: 1, 0, and never-saved. The
		read path (`single_flag(..., default=1)`) treats never-saved as sandbox, so
		shouting "you are about to write to the live marketplace" at a site that has
		simply never opened this form would be a false alarm — and a false alarm on
		this particular warning is expensive, because it is the one that has to be
		believed when it is real.
		"""
		raw = single_raw(SETTINGS_DOCTYPE, "sandbox_mode")
		if not self.enabled:
			return
		if raw is None:
			frappe.msgprint(
				_("GunBroker is running against the SANDBOX (Sandbox Mode has never "
				  "been saved on this site, and the default is on). Save this form to "
				  "record the choice explicitly."),
				indicator="orange", alert=True)
			return
		if not self.sandbox_mode:
			frappe.msgprint(
				_("<b>GunBroker is pointed at the LIVE marketplace.</b> Listings "
				  "created from this site are real offers to real buyers, and ending "
				  "one after somebody has bought it means refunding them."),
				indicator="red", alert=True)

	def _check_webhook_credentials(self):
		"""Basic auth cannot carry a colon in the user name, and a webhook with only
		half its credentials can never verify — say so instead of saving silently."""
		user = (self.webhook_username or "").strip()
		if ":" in user:
			frappe.throw(_("The webhook user name cannot contain a colon."),
				title=_("GunBroker Settings incomplete"))
		password = self.webhook_password or ""
		# An unchanged password arrives masked as asterisks; only a new one is checked.
		if password and set(password) != {"*"} and len(password) < WEBHOOK_PASSWORD_MIN:
			frappe.throw(
				_("The webhook password must be at least {0} characters.").format(
					WEBHOOK_PASSWORD_MIN),
				title=_("GunBroker Settings incomplete"))
		if bool(user) != bool(password):
			frappe.msgprint(
				_("Set both the webhook user name and password. GunBroker webhooks are "
				  "refused until both are filled in."),
				indicator="orange")

	def _check_category_rows(self):
		"""Every category row must name exactly one thing to map.

		The table serves two kinds of listing now: a firearm row keys on
		`firearm_type`, an item-level row (ammunition, optics, parts) on
		`item_group`. `firearm_type` used to be mandatory at the field level,
		which is why this moved into validate — a row with neither is a rule
		that can never match, and a row with both would be claimed by whichever
		resolver ran first, which is not a decision to leave to import order.
		"""
		for row in (self.category_map or []):
			has_type = bool((row.firearm_type or "").strip())
			has_group = bool((row.item_group or "").strip())
			if has_type == has_group:
				frappe.throw(
					_("Category row {0}: fill in EITHER Firearm Type (for guns) OR "
					  "Item Group (for ammunition, optics and parts) — not {1}."
					  ).format(row.idx, _("both") if has_type else _("neither")),
					title=_("Category Row Is Not Mappable"))

	def _warn_about_shipping_costs(self):
		if not self.enabled:
			return
		if float(self.long_gun_ship_cost or 0) <= 0 or float(self.handgun_ship_cost or 0) <= 0:
			frappe.msgprint(
				_("Long gun / handgun shipping is not set. Listing a gun will be "
				  "refused with 'no shipping cost configured' until both are filled "
				  "in — a $0 flat rate would mean shipping firearms for free."),
				indicator="orange")
		if float(self.accessory_ship_cost or 0) <= 0:
			frappe.msgprint(
				_("Accessory / ammo shipping is not set. Item-level listings "
				  "(ammunition, optics, parts) will be refused until it is."),
				indicator="orange")

	def _warn_about_end_strategy(self):
		"""Manual Only is the degraded mode, so say so. Delete Endpoint is the
		expected state and gets no message — a warning that fires on the correct
		configuration teaches people to ignore warnings."""
		if self.end_strategy == "Manual Only":
			frappe.msgprint(
				_("End Listing Strategy is on the fallback setting. Every gun that "
				  "leaves stock will have to be ended by hand on GunBroker, and until "
				  "somebody does it the listing can still be bought. DELETE /Items is "
				  "authorised for this account — normally this should be 'Delete "
				  "Endpoint'."),
				indicator="orange")

	def _warn_about_auto_relist(self):
		"""Catch the auction value on a fixed-price listing.

		`2 Relist Until Sold` is not offered by the Select any more, so the only
		ways to get here are a value written through the API and a row that
		predates that change. Both are worth catching: on a fixed-price listing 2
		does nothing at all, which shows up as every unsold gun quietly falling
		off GunBroker after thirty days — a silence nobody reports as a bug.
		"""
		if self.auto_relist in VALID_AUTO_RELIST:
			return
		frappe.msgprint(
			_("<b>Auto-Relist is set to {0}, which will not work.</b> A fixed-price "
			  "listing must use '4 Relist Fixed Price'. 2 is the auction value and "
			  "has no effect here — listings would simply stop after their 30 days "
			  "instead of relisting."
			  ).format(self.auto_relist or _("(blank)")),
			indicator="red", alert=True)

	def _enforce_card_switch(self):
		"""The card tender checkboxes are meaningless while card checkout is off,
		and a ticked-but-inert box is worse than an unticked one: it reads as
		'we accept this'."""
		if self.card_checkout_enabled:
			return
		card_fields = ("pay_visa_mastercard", "pay_amex", "pay_discover")
		if any(self.get(f) for f in card_fields):
			for f in card_fields:
				self.set(f, 0)
			frappe.msgprint(
				_("Card payment methods were cleared: Accept Card Checkout is off, so "
				  "a buyer could not use them even if the listing offered them."),
				indicator="orange")

	def _require_deposit_account(self):
		"""The one hard stop. Cheques and money orders are the only live payment
		route in this phase, and recording one needs a bank account to receive it.
		This company has no default bank account and no operating bank account in
		its chart — only two clearing accounts — so there is nothing to fall back
		to: every single payment would fail at the moment somebody tries to record
		one, with the money already in hand. Refusing to save is the cheaper
		failure."""
		if not self.enabled:
			return
		if not self.check_deposit_account:
			frappe.throw(
				_("Set a Check Deposit Account before enabling GunBroker. Cheques and "
				  "money orders are the only payment method this phase accepts, and "
				  "recording one needs a real operating bank account to deposit into "
				  "— not a clearing account."),
				title=_("GunBroker Settings incomplete"))
		self._require_real_bank_account()

	def _require_real_bank_account(self):
		""""Not a clearing account" has to be enforced, not just written down.

		It was a sentence in a go-live checklist, and the account picker will
		happily offer the two clearing accounts that ARE Bank-type — they are the
		nearest-looking wrong answers on the list. Filling one in produces no
		error anywhere: cheque deposits and card settlements land in one account,
		the bank reconciliation that expects to match real deposits stops
		matching, and nobody finds out until month-end.

		Three separate reasons, each with its own message, because "invalid
		account" would leave somebody guessing which of them they hit.
		"""
		account = frappe.db.get_value(
			"Account", self.check_deposit_account,
			["account_type", "is_group", "company"], as_dict=True) or frappe._dict()

		if account.get("is_group"):
			frappe.throw(
				_("{0} is a group account — postings cannot be made to it. Choose the "
				  "individual bank account underneath it."
				  ).format(self.check_deposit_account),
				title=_("GunBroker Settings incomplete"))

		if (account.get("account_type") or "") != "Bank":
			frappe.throw(
				_("{0} is not a Bank account (it is {1}). The Check Deposit Account is "
				  "where cheques and money orders are actually deposited, so it has to "
				  "be the real bank account."
				  ).format(self.check_deposit_account,
					account.get("account_type") or _("untyped")),
				title=_("GunBroker Settings incomplete"))

		if self.check_deposit_account in _clearing_accounts(account.get("company")):
			frappe.throw(
				_("{0} is a clearing account, not an operating bank account. Money "
				  "physically deposited at the bank must not be booked through a "
				  "clearing account — it puts cheque deposits and card settlements in "
				  "one place and breaks the month-end bank reconciliation. Use the "
				  "bank account the deposits really go into."
				  ).format(self.check_deposit_account),
				title=_("GunBroker Settings incomplete"))

	def _warn_about_insecure_footer(self):
		if "http://" in (self.description_footer or ""):
			frappe.msgprint(
				_("The description footer contains a plain http:// link or image. "
				  "GunBroker serves listings over https, so browsers will block it "
				  "and buyers will see a broken listing."),
				indicator="orange")
