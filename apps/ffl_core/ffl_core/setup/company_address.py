"""Tell the System Managers when a Company has no usable address.

The home state comes from the Company's own address and has no default, so a
Company without one cannot take a sale shipped to another state. This reports it
after every migrate (ToDo + Error Log) for each Company that has no open ToDo about
it, so it is fixed before a sale fails; closing the ToDo without fixing the address
re-arms the report at the next migrate. Reads through ``interstate_tax.check_company_address``; never raises.
"""
from __future__ import annotations

import frappe

TITLE = "Company address missing"


def ensure() -> None:
	from ffl_core.alerts import alert_assignees, alert_once
	from ffl_core.compliance.interstate_tax import check_company_address

	for company in frappe.get_all("Company", pluck="name"):
		problem = check_company_address(company)
		if not problem:
			continue
		try:
			alert_once(
				"Company", company, title=f"{TITLE}: {company}",
				message=f"{company}: {problem} Until then, in-state / out-of-state sales tax "
					"and freight tax cannot be decided.",
				description=f"{TITLE}: see the Error Log of the same title.",
				assignees=[u for u in alert_assignees() if "System Manager" in frappe.get_roles(u)])
		except Exception:
			frappe.log_error(title=f"{TITLE}: alert failed")
