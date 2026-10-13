"""Bootstrap a fresh ERPNext site with the minimum masters our test suite
and Phase 1 development workflows depend on.

Idempotent — safe to re-run.

Run inside the bench container:
    bench --site dev.localhost execute scripts.setup_dev_site.run

Used by:
- Local dev after `make dev-bench-init` (one-shot)
- CI in `.github/workflows/ci.yml` before integration tests

What it creates (only if missing):
- root Item Group / Customer Group / Territory
- Company "FFL Test", with an address in Texas (a real store always has one;
  the home state decides in-state vs out-of-state sales tax)
- UOM "Nos"
- Warehouse
- Currency exchange (USD)
- Selling Price List "Standard Selling"
- Mode of Payment "Cash"
- POS Profile "FFL Test"

Standalone of any custom-app code so it can be invoked before our apps are
even installed (useful when bootstrapping CI from scratch).
"""
from __future__ import annotations

import frappe
from ffl_core.utils import commit

COMPANY = "FFL Test"
COMPANY_ABBR = "FT"
CURRENCY = "USD"
COUNTRY = "United States"


def run() -> None:
	_root_groups()
	_warehouse_type_transit()  # must exist before Company insert (auto-creates In-Transit warehouse)
	_company()
	company_address()
	_uom("Nos", whole_number=True)
	_uom("Each", whole_number=True)
	_warehouse("Stores", company=COMPANY)
	_price_list("Standard Selling", currency=CURRENCY, selling=True)
	_mode_of_payment("Cash", payment_type="Cash")
	_pos_profile()
	_firearm_item_groups()
	_stock_settings()
	_stock_entry_types()
	commit()
	print(f"setup_dev_site complete; company={COMPANY}")


def _stock_entry_types() -> None:
	# ERPNext seeds these via a fixture during setup wizard; fresh CI installs
	# don't always have them. Create the ones we use ourselves.
	for name, purpose in (
		("Material Receipt", "Material Receipt"),
		("Material Issue", "Material Issue"),
		("Material Transfer", "Material Transfer"),
		("Repack", "Repack"),
	):
		if not frappe.db.exists("Stock Entry Type", name):
			frappe.get_doc({
				"doctype": "Stock Entry Type",
				"name": name,
				"purpose": purpose,
			}).insert(ignore_permissions=True)


def _warehouse_type_transit() -> None:
	if not frappe.db.exists("Warehouse Type", "Transit"):
		frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert(ignore_permissions=True)


def _stock_settings() -> None:
	# v16 requires opting in to serialised-item Stock Entry handling so that
	# items with has_serial_no=1 can be received/issued via Stock Entry.
	from ffl_core.setup import serial_tracking

	serial_tracking.ensure()
	frappe.db.set_single_value("Stock Settings", "auto_create_serial_and_batch_bundle_for_inward", 1)


def _firearm_item_groups() -> None:
	"""Item Group hierarchy.

	Categories describe product TYPE only — condition (new vs used) is a
	separate Item-level checkbox (`is_used`). Tree:

	  All Item Groups
	  ├─ Firearm                (group)
	  │  ├─ Pistol / Revolver
	  │  ├─ Rifle
	  │  ├─ Shotgun
	  │  ├─ Other Firearm           (black powder, etc.)
	  │  └─ NFA                 (group — National Firearms Act items)
	  │     ├─ Suppressor
	  │     ├─ SBR              (short-barreled rifle)
	  │     ├─ SBS              (short-barreled shotgun)
	  │     ├─ DD               (destructive device)
	  │     ├─ MG               (machine gun)
	  │     └─ AOW              (any other weapon)
	  ├─ Ammunition
	  └─ Accessory              (group)
	     ├─ Optic
	     ├─ Magazine
	     ├─ Holster
	     ├─ Cleaning
	     ├─ Light
	     ├─ Knife
	     ├─ Safe
	     └─ Misc Accessory

	Legacy "New Firearm" / "Used Firearm" are retired (never created; kept
	only where a site still links to them).

	Delegates to the canonical, after_migrate taxonomy so the dev/CI site can
	never drift from prod — this used to re-list the whole tree by hand and had
	already lost the "Service" group.
	"""
	from ffl_core.setup.item_groups import ensure
	ensure()


def _root_groups() -> None:
	for dt, root_name in [
		("Item Group", "All Item Groups"),
		("Customer Group", "All Customer Groups"),
		("Territory", "All Territories"),
	]:
		if not frappe.db.exists(dt, root_name):
			doc = frappe.new_doc(dt)
			# field name varies between doctypes (item_group_name, customer_group_name, territory_name)
			field = f"{dt.lower().replace(' ', '_')}_name"
			doc.update({field: root_name, "is_group": 1})
			doc.insert(ignore_permissions=True)
	# Leaf children Customer / Address require to exist for inserts.
	if not frappe.db.exists("Customer Group", "Individual"):
		frappe.get_doc({
			"doctype": "Customer Group",
			"customer_group_name": "Individual",
			"parent_customer_group": "All Customer Groups",
			"is_group": 0,
		}).insert(ignore_permissions=True)
	if not frappe.db.exists("Customer Group", "Commercial"):
		frappe.get_doc({
			"doctype": "Customer Group",
			"customer_group_name": "Commercial",
			"parent_customer_group": "All Customer Groups",
			"is_group": 0,
		}).insert(ignore_permissions=True)
	if not frappe.db.exists("Territory", "United States"):
		frappe.get_doc({
			"doctype": "Territory",
			"territory_name": "United States",
			"parent_territory": "All Territories",
			"is_group": 0,
		}).insert(ignore_permissions=True)


def _company() -> None:
	if frappe.db.exists("Company", {"company_name": COMPANY}):
		return
	frappe.get_doc({
		"doctype": "Company",
		"company_name": COMPANY,
		"abbr": COMPANY_ABBR,
		"default_currency": CURRENCY,
		"country": COUNTRY,
	}).insert(ignore_permissions=True)


def company_address() -> None:
	"""Give the test Company the address `store_setup` gives every real store.

	The home state is read from the Company's own address and there is no default
	(ffl_core.compliance.interstate_tax.home_state raises without one), so a test
	Company with no address could not take a sale shipped anywhere.
	"""
	from ffl_core.store_identity import company_address as existing

	company = frappe.db.get_value("Company", {"company_name": COMPANY}, "name")
	if not company or existing(company):
		return
	frappe.get_doc({
		"doctype": "Address", "address_title": f"{COMPANY} Shop", "address_type": "Office",
		"address_line1": "100 Main St", "city": "Springfield", "state": "TX",
		"pincode": "70001", "country": COUNTRY,
		"links": [{"link_doctype": "Company", "link_name": company}],
	}).insert(ignore_permissions=True)


def _uom(name: str, whole_number: bool = False) -> None:
	if frappe.db.exists("UOM", name):
		return
	frappe.get_doc({
		"doctype": "UOM",
		"uom_name": name,
		"must_be_whole_number": int(whole_number),
	}).insert(ignore_permissions=True)


def _warehouse(label: str, company: str) -> str:
	# v16's Company.create_default_warehouses references a "Transit" Warehouse
	# Type for the auto-created "Goods In Transit" warehouse. Setup wizard
	# seeds it; without the wizard we have to create it ourselves before any
	# Warehouse is inserted (which happens during Company insert).
	if not frappe.db.exists("Warehouse Type", "Transit"):
		frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert(ignore_permissions=True)

	existing = frappe.db.get_value(
		"Warehouse", {"warehouse_name": label, "company": company}, "name"
	)
	if existing:
		return existing
	doc = frappe.get_doc({
		"doctype": "Warehouse",
		"warehouse_name": label,
		"company": company,
	}).insert(ignore_permissions=True)
	return doc.name


def _price_list(name: str, currency: str, selling: bool) -> None:
	if frappe.db.exists("Price List", name):
		return
	frappe.get_doc({
		"doctype": "Price List",
		"price_list_name": name,
		"currency": currency,
		"enabled": 1,
		"selling": int(selling),
	}).insert(ignore_permissions=True)


def _mode_of_payment(name: str, payment_type: str) -> None:
	if not frappe.db.exists("Mode of Payment", name):
		frappe.get_doc({
			"doctype": "Mode of Payment",
			"mode_of_payment": name,
			"type": payment_type,
			"enabled": 1,
		}).insert(ignore_permissions=True)

	# POS Profile insert validates that the MoP has a default account for the
	# company. Wire one up if missing — pick the first GL account whose name
	# starts with "Cash" for Cash MoP, else the company's default cash account.
	mop = frappe.get_doc("Mode of Payment", name)
	if any(row.company == COMPANY for row in mop.accounts):
		return

	default_account = _company_cash_account()
	if not default_account:
		# Create a placeholder Cash account under the company root.
		default_account = _ensure_cash_account()

	mop.append("accounts", {"company": COMPANY, "default_account": default_account})
	mop.save(ignore_permissions=True)


def _company_cash_account() -> str | None:
	# ERPNext's standard chart of accounts puts a Cash leaf under "Cash In Hand".
	for filters in (
		{"company": COMPANY, "account_name": "Cash", "is_group": 0},
		{"company": COMPANY, "account_name": ("like", "Cash%"), "is_group": 0},
	):
		row = frappe.get_all("Account", filters=filters, pluck="name", limit=1)
		if row:
			return row[0]
	return None


def _ensure_cash_account() -> str:
	# Find a parent Asset / Cash In Hand group; fallback to Application of Funds (Assets) root.
	parent = frappe.db.get_value(
		"Account",
		{"company": COMPANY, "account_name": "Cash In Hand", "is_group": 1},
		"name",
	) or frappe.db.get_value(
		"Account",
		{"company": COMPANY, "root_type": "Asset", "is_group": 1},
		"name",
	)
	if not parent:
		raise RuntimeError("no Asset parent account exists; Company chart of accounts not initialised")
	doc = frappe.get_doc({
		"doctype": "Account",
		"account_name": "Cash",
		"parent_account": parent,
		"company": COMPANY,
		"account_type": "Cash",
		"is_group": 0,
	}).insert(ignore_permissions=True)
	return doc.name


def _pos_profile() -> None:
	if frappe.db.exists("POS Profile", COMPANY):
		return
	warehouse = frappe.db.get_value(
		"Warehouse", {"warehouse_name": "Stores", "company": COMPANY}, "name"
	)
	# Required-field auto-population in POS Profile is incomplete in v16; supply
	# write-off bookkeeping accounts directly to keep validate happy.
	write_off_account = frappe.db.get_value(
		"Company", COMPANY, "write_off_account"
	) or _first_account({"company": COMPANY, "account_name": ("like", "Write Off%"), "is_group": 0})
	cost_center = frappe.db.get_value(
		"Company", COMPANY, "cost_center"
	) or _first_value("Cost Center", {"company": COMPANY, "is_group": 0})

	doc = frappe.get_doc({
		"doctype": "POS Profile",
		"name": COMPANY,
		"company": COMPANY,
		"warehouse": warehouse,
		"currency": CURRENCY,
		"selling_price_list": "Standard Selling",
		"write_off_account": write_off_account,
		"write_off_cost_center": cost_center,
		"income_account": _first_account({"company": COMPANY, "account_type": "Income Account", "is_group": 0}),
		"expense_account": _first_account({"company": COMPANY, "account_type": "Expense Account", "is_group": 0}),
		"payments": [{"mode_of_payment": "Cash", "default": 1}],
	})
	doc.insert(ignore_permissions=True)


def _first_account(filters: dict) -> str | None:
	row = frappe.get_all("Account", filters=filters, pluck="name", limit=1)
	return row[0] if row else None


def _first_value(doctype: str, filters: dict) -> str | None:
	row = frappe.get_all(doctype, filters=filters, pluck="name", limit=1)
	return row[0] if row else None
