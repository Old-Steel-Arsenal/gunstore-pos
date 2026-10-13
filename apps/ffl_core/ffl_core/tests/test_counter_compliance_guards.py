"""The SI `validate` compliance chain vs a drop-ship firearm row.

`_validate_items` is only the Create Order API's own check. The compliance
guards are an INDEPENDENT doc_event chain on `Sales Invoice: validate`, and
`require_serial_for_firearm` hard-throws on an FDS drop-ship gun: it has no serial of
OURS (the distributor ships it and reports the serial back on the Distributor Order).

The exemption is deliberately the narrowest expressible — `delivered_by_supplier`
AND `dropship_distributor`, both set:

* a LOCAL firearm row carries neither, so every guard still applies to it;
* a P4c **procure-to-store** row is a LOCAL row (received into our inventory,
  dispositioned here, serial assigned) with `delivered_by_supplier=0` — guards intact;
* requiring BOTH means a stray core flag alone can never widen it.

The chain's other members were audited and need NO change (asserted below), which is
the point of this module: it pins WHY each is safe, so a later edit that widens an
exemption has to argue with a test.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ffl_core.compliance import firearm

_dict = firearm.frappe._dict
_GUNS = {"RSR-GUN", "LOCAL-GUN"}


def _si(rows, **over):
	doc = types.SimpleNamespace(
		doctype="Sales Invoice", docstatus=1, items=[_dict(**r) for r in rows])
	for k, v in over.items():
		setattr(doc, k, v)
	return doc


_DROPSHIP_GUN = {"item_code": "RSR-GUN", "idx": 1, "serial_no": "",
	"delivered_by_supplier": 1, "dropship_distributor": "RSR"}
_LOCAL_GUN_NO_SERIAL = {"item_code": "LOCAL-GUN", "idx": 1, "serial_no": ""}


class RequireSerialDropshipExemptionTest(unittest.TestCase):
	def setUp(self):
		p = mock.patch.object(firearm, "is_firearm_item", lambda code: code in _GUNS)
		p.start()
		self.addCleanup(p.stop)

	def test_dropship_firearm_row_is_exempt(self):
		firearm.require_serial_for_firearm(_si([_DROPSHIP_GUN]))  # no throw

	def test_local_firearm_row_still_requires_a_serial(self):
		with self.assertRaises(firearm.frappe.ValidationError):
			firearm.require_serial_for_firearm(_si([_LOCAL_GUN_NO_SERIAL]))

	def test_delivered_by_supplier_alone_does_not_exempt(self):
		# A stray core flag with no distributor pin must not smuggle a gun through.
		with self.assertRaises(firearm.frappe.ValidationError):
			firearm.require_serial_for_firearm(_si([
				{"item_code": "LOCAL-GUN", "idx": 1, "serial_no": "",
					"delivered_by_supplier": 1, "dropship_distributor": ""}]))

	def test_distributor_pin_alone_does_not_exempt(self):
		# This is the P4c procure-to-store shape: sourced from a distributor but
		# received into OUR inventory, so it must still carry a serial.
		with self.assertRaises(firearm.frappe.ValidationError):
			firearm.require_serial_for_firearm(_si([
				{"item_code": "LOCAL-GUN", "idx": 1, "serial_no": "",
					"delivered_by_supplier": 0, "dropship_distributor": "RSR"}]))

	def test_mixed_invoice_still_catches_the_local_gun(self):
		with self.assertRaises(firearm.frappe.ValidationError):
			firearm.require_serial_for_firearm(
				_si([_DROPSHIP_GUN, dict(_LOCAL_GUN_NO_SERIAL, idx=2)]))

	def test_draft_is_untouched(self):
		doc = _si([_LOCAL_GUN_NO_SERIAL])
		doc.docstatus = 0
		firearm.require_serial_for_firearm(doc)  # drafts stay open


class SiblingGuardAuditTest(unittest.TestCase):
	"""The rest of the chain needed no change — these pin the reasons."""

	def setUp(self):
		# The 4473 gate keys off is_REGULATED_firearm_item (antiques are exempt),
		# not is_firearm_item — patch both or the gate silently no-ops.
		for name in ("is_firearm_item", "is_regulated_firearm_item"):
			p = mock.patch.object(firearm, name, lambda code: code in _GUNS)
			p.start()
			self.addCleanup(p.stop)

	def test_4473_gate_already_exempts_a_destination_ffl_order(self):
		"""An FDS gun ships to the buyer's transfer FFL, where THEY complete the 4473 —
		the guard's existing `ffl_destination_ffl` early-return covers this exactly, so
		no drop-ship-specific exemption was added (a narrower blast radius)."""
		doc = _si([_DROPSHIP_GUN], ffl_destination_ffl="1-99-888", is_consolidated=0)
		firearm.require_4473_and_nics_for_firearm_sale(doc)  # no throw

	def test_4473_gate_still_fires_without_a_destination_ffl(self):
		# Regression: the counter retail path is unchanged.
		doc = _si([_LOCAL_GUN_NO_SERIAL], ffl_destination_ffl="", is_consolidated=0,
			customer=None, form_4473_status="", nics_status="")
		with self.assertRaises(firearm.frappe.ValidationError):
			firearm.require_4473_and_nics_for_firearm_sale(doc)


if __name__ == "__main__":
	unittest.main()
