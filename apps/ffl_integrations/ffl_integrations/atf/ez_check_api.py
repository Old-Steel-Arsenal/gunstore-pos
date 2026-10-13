"""Whitelisted methods behind the Supplier/Customer Verify FFL buttons."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime

from ffl_core.roles import STOCK_ROLES

from ffl_integrations.atf import ez_check


@frappe.whitelist()
def verify_customer_ffl(customer_name: str) -> dict:
	"""Live-verify a dealer customer's FFL and stamp the result on the Customer.

	Customer-side mirror of verify_supplier_ffl: stamps
	status/licensee/expiry onto the Customer. The dealer-shipment gate
	(ffl_core.compliance.dealer_transfer) requires Verified + unexpired
	before firearms may ship to this customer. A provider outage stamps
	status "Failed" rather than raising.

	Args:
	    customer_name: Customer with a non-empty ffl_number.

	Returns:
	    {ok, status, licensee_name, expiry_date}.
	"""
	frappe.only_for(STOCK_ROLES)
	customer = frappe.get_doc("Customer", customer_name)
	if not customer.get("ffl_number"):
		frappe.throw(_("Customer has no FFL number to verify."))

	try:
		result = ez_check.verify(customer.ffl_number, force_live=True)
		new_status = result.status if result.found else "Failed"
	except ez_check.EZCheckUnavailable as exc:
		new_status = "Failed"
		result = ez_check.EZCheckResult(
			ffl_number=customer.ffl_number, found=False, status="Failed",
			raw={"error": str(exc)},
		)

	frappe.db.set_value("Customer", customer_name, {
		"ez_check_status": new_status,
		"ez_check_last_run": now_datetime(),
		"licensee_name": result.licensee_name or customer.get("licensee_name"),
		"licensee_address": result.address or customer.get("licensee_address"),
		"ffl_expiry_date": result.expiry_date or customer.get("ffl_expiry_date"),
	}, update_modified=False)
	frappe.db.commit()

	return {
		"ok": True,
		"status": new_status,
		"licensee_name": result.licensee_name,
		"expiry_date": result.expiry_date.isoformat() if result.expiry_date else None,
	}


@frappe.whitelist()
def verify_supplier_ffl(supplier_name: str) -> dict:
	"""Live-verify a supplier's FFL and stamp the result on the Supplier.

	The Verify FFL button on the Supplier form. Stamps
	ez_check_status/licensee/expiry; a provider outage stamps status
	"Failed" rather than raising.

	Args:
	    supplier_name: Supplier with a non-empty ffl_number.

	Returns:
	    {ok, status, licensee_name, expiry_date}.
	"""
	frappe.only_for(STOCK_ROLES)
	supplier = frappe.get_doc("Supplier", supplier_name)
	if not supplier.ffl_number:
		frappe.throw(_("Supplier has no FFL number to verify."))

	try:
		result = ez_check.verify(supplier.ffl_number, force_live=True)
		new_status = result.status if result.found else "Failed"
	except ez_check.EZCheckUnavailable as exc:
		new_status = "Failed"
		result = ez_check.EZCheckResult(
			ffl_number=supplier.ffl_number, found=False, status="Failed",
			raw={"error": str(exc)},
		)

	frappe.db.set_value("Supplier", supplier_name, {
		"ez_check_status": new_status,
		"ez_check_last_run": now_datetime(),
		"licensee_name": result.licensee_name or supplier.licensee_name,
		"licensee_address": result.address or supplier.licensee_address,
		"ffl_expiry_date": result.expiry_date or supplier.ffl_expiry_date,
	}, update_modified=False)
	frappe.db.commit()

	return {
		"ok": True,
		"status": new_status,
		"licensee_name": result.licensee_name,
		"expiry_date": result.expiry_date.isoformat() if result.expiry_date else None,
	}


@frappe.whitelist()
def verify_own_ffl() -> dict:
	"""Live-verify the store's own FFL (FFL Settings → Our FFL Licence Number).

	The Test Connection of the FFL Settings form: exercises the configured
	provider end to end without stamping anything. Provider outages come
	back as status "Failed" with the reason rather than a traceback.

	Returns:
	    {ok, status, licensee_name, expiry_date, error}. ``ok`` is true only
	    for status "Verified".
	"""
	frappe.only_for("System Manager")
	ffl_number = (frappe.db.get_single_value("FFL Settings", "own_ffl_license") or "").strip()
	if not ffl_number:
		frappe.throw(_("Enter Our FFL Licence Number first."))
	try:
		result = ez_check.verify(ffl_number, force_live=True)
		status = result.status if result.found else "NotFound"
		error = None
	except ez_check.EZCheckUnavailable as exc:
		status, result, error = "Failed", None, str(exc)
	return {
		"ok": status == "Verified",
		"status": status,
		"licensee_name": result.licensee_name if result else None,
		"expiry_date": result.expiry_date.isoformat() if result and result.expiry_date else None,
		"error": error,
	}
