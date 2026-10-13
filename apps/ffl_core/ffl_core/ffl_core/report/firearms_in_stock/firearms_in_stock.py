"""Firearms In Stock — every in-stock firearm by serial number.

The ATF A&D bound book lives in FastBound; staff view acquisitions and
dispositions there. This report answers the daily floor question "which
guns do we physically have right now, by serial?" off our own Serial No
records, enriched with where in the store each gun sits (its storage location, blank when
it has not been placed), the acquisition source and a FastBound deep link to the
authoritative item record.
"""
from __future__ import annotations

from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import getdate

_FB_BASE = "https://cloud.fastbound.com"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	return _columns(), _rows(filters)


def _columns():
	return [
		{"label": _("Serial No"), "fieldname": "serial_no", "fieldtype": "Link", "options": "Serial No", "width": 180},
		{"label": _("Item"), "fieldname": "item_code", "fieldtype": "Link", "options": "Item", "width": 200},
		{"label": _("Manufacturer"), "fieldname": "manufacturer", "fieldtype": "Data", "width": 130},
		{"label": _("Model"), "fieldname": "model_name", "fieldtype": "Data", "width": 130},
		{"label": _("Caliber"), "fieldname": "caliber", "fieldtype": "Data", "width": 110},
		{"label": _("Firearm Type"), "fieldname": "firearm_type", "fieldtype": "Data", "width": 110},
		{"label": _("Importer"), "fieldname": "importer", "fieldtype": "Data", "width": 120},
		{"label": _("Condition"), "fieldname": "condition_grade", "fieldtype": "Data", "width": 100},
		{"label": _("Warehouse"), "fieldname": "warehouse", "fieldtype": "Link", "options": "Warehouse", "width": 160},
		{"label": _("Location"), "fieldname": "location", "fieldtype": "Data", "width": 110},
		{"label": _("Acquired"), "fieldname": "acquired", "fieldtype": "Date", "width": 100},
		{"label": _("Source"), "fieldname": "source", "fieldtype": "Data", "width": 150},
		{"label": _("FastBound"), "fieldname": "fastbound", "fieldtype": "HTML", "width": 120},
	]


def _rows(filters):
	# Per-unit override: old / surplus / imported guns under a shared model SKU
	# can carry their own maker / importer on the Serial No (the bound-book push
	# prefers them too). Surface the per-serial value, falling back to the model
	# Item — so the by-serial floor view shows what each physical gun actually is.
	# importer never falls back (2026-08-20): on generic buckets the Item-level
	# value is one gun's marking, so the column shows only what THIS gun carries.
	mfg = "COALESCE(NULLIF(sn.manufacturer, ''), i.manufacturer)"
	model = "COALESCE(NULLIF(sn.model_name, ''), i.model_name)"
	cal = "COALESCE(NULLIF(sn.caliber, ''), i.caliber)"
	ftype = "COALESCE(NULLIF(sn.firearm_type, ''), i.firearm_type)"
	imp = "NULLIF(sn.importer, '')"

	conditions = ["sn.status = %(status)s"]
	params = {"status": filters.get("status") or "Active"}
	if filters.get("warehouse"):
		conditions.append("sn.warehouse = %(warehouse)s")
		params["warehouse"] = filters.warehouse
	if filters.get("manufacturer"):
		conditions.append(f"{mfg} = %(manufacturer)s")
		params["manufacturer"] = filters.manufacturer

	guns = frappe.db.sql(
		f"""
		SELECT sn.name AS serial_no, sn.item_code, sn.warehouse, loc.label AS location,
		       sn.fastbound_item_id, {mfg} AS manufacturer, {model} AS model_name,
		       {cal} AS caliber, {ftype} AS firearm_type, {imp} AS importer, sn.condition_grade
		FROM `tabSerial No` sn
		INNER JOIN `tabItem` i ON i.name = sn.item_code AND i.is_firearm = 1
		LEFT JOIN `tabStorage Location` loc ON loc.name = sn.storage_location
		WHERE {" AND ".join(conditions)}
		ORDER BY {mfg}, {model}, sn.name
		""",
		params,
		as_dict=True,
	)
	if not guns:
		return []

	acquired_by_serial = _acquisitions([g.serial_no for g in guns])
	account = frappe.db.get_single_value("FastBound Settings", "account_num")

	rows = []
	for g in guns:
		acq = acquired_by_serial.get(g.serial_no)
		rows.append({
			"serial_no": g.serial_no,
			"item_code": g.item_code,
			"manufacturer": g.manufacturer,
			"model_name": g.model_name,
			"caliber": g.caliber,
			"firearm_type": g.firearm_type,
			"importer": g.importer,
			"condition_grade": g.condition_grade,
			"warehouse": g.warehouse,
			"location": g.location,
			"acquired": getdate(acq.acquisition_date) if acq else None,
			"source": acq.source_name if acq else None,
			"fastbound": _fastbound_link(g.fastbound_item_id, account),
		})
	return rows


def _acquisitions(serials):
	"""Map each serial to its latest submitted FFL Acquisition (date + source)."""
	rows = frappe.db.sql(
		"""
		SELECT serial, acquisition_date, source_name
		FROM `tabFFL Acquisition`
		WHERE docstatus = 1 AND serial IN %(serials)s
		ORDER BY acquisition_date
		""",
		{"serials": serials},
		as_dict=True,
	)
	# Ascending order → last assignment wins = latest acquisition per serial.
	return {r.serial: r for r in rows}


def _fastbound_link(item_id, account):
	if not item_id:
		return '<span class="text-muted">&mdash;</span>'
	path = f"/{quote(account)}/Items/{quote(str(item_id))}" if account else f"/Items/{quote(str(item_id))}"
	return f'<a href="{_FB_BASE}{path}" target="_blank" rel="noopener">&#8599; {_("FastBound")}</a>'
