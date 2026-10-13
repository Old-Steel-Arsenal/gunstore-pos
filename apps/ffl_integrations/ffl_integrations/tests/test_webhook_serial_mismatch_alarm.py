"""P5 — when a committed disposition is linked to
a POS Invoice whose firearm line serial does NOT match the serial the 4473 was run
on (H5123 vs V2689), the webhook currently, SILENTLY, routes the unmatched serial
to the direct-disposal stock sync. It must instead raise an early, loud alarm
(Error Log + invoice comment + realtime nudge to POS) so staff catch it before
submit — and it must NOT auto-dispose a serial that belongs to a linked retail
sale. The submit hard-gate (_require_disposition_coverage) is unchanged; this is a
belt-and-braces early warning.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase


def _committed_payload(disposition_id, items, *, is4473, type_):
	"""Mirror the REAL disposition.committed `data` shape (evidence: prod Error Log
	qvfjg7sjuc — the incident's own committed event): top-level `is4473`, `type`
	(Sale/Dealer Transfer/…), full `items[]` carrying id/serial/statusText/type, plus
	a contact object. Values are anonymized `_Test` only — NO PII ever in repo. Each
	item = (fb_item_id, serial). This proves the handlers read the fields off the real
	structure, not a simplified fixture."""
	return {
		"id": disposition_id,
		"date": "2026-01-01T00:00:00",
		"invoiceNumber": None,
		"is4473": is4473,
		"isManufacturingDisposition": False,
		"otsn": "_TEST-OTSN",
		"ttsn": "_TEST-TTSN",
		"type": type_,
		"contact": {"firstName": "_Test", "lastName": "Buyer", "statusText": "Approved"},
		"items": [
			{"id": fb_id, "serial": serial, "externalId": serial,
				"manufacturer": "_TestMfg", "model": "_TestModel", "type": "Pistol",
				"statusText": "Disposed", "acquisitionType": "_Test"}
			for fb_id, serial in items
		],
	}


# Non-4473 direct disposal (e.g. a dealer transfer booked directly in FastBound).
_DATA = _committed_payload("FB-DISP-XFER", [("FB-IT-1", "FBX-SYNC-1")],
	is4473=False, type_="Dealer Transfer")
_TARGET = {"serial": "FBX-SYNC-1", "item_code": "GUN-X", "warehouse": "WH-1", "fb_id": "FB-IT-1"}


class SerialMismatchAlarm(FrappeTestCase):
	def test_alarm_fires_when_invoice_serial_is_uncovered(self):
		"""A linked invoice whose firearm serial has NO matching Retail Sale
		disposition → loud alarm: Error Log + invoice comment + realtime."""
		from ffl_integrations.fastbound import webhook as fb
		gd = MagicMock()
		with patch.object(fb, "_invoice_firearm_serials", return_value={"V2689"}), \
		     patch.object(fb.frappe, "get_all", return_value=[]), \
		     patch.object(fb, "_log_error") as log, \
		     patch.object(fb.frappe, "publish_realtime") as rt, \
		     patch.object(fb.frappe, "get_doc", return_value=gd):
			fb._alert_invoice_serial_coverage("FB-DISP-D", ["ACC-PSINV-2026-99001"])

		log.assert_called_once()
		title = (log.call_args.kwargs.get("title") or "")
		self.assertIn("MISMATCH", title.upper())
		gd.add_comment.assert_called_once()
		rt.assert_called_once()
		# The uncovered serial must appear in the realtime payload for the POS nudge.
		self.assertIn("V2689", str(rt.call_args))

	def test_no_alarm_when_every_invoice_serial_is_covered(self):
		"""The normal path: the invoice's firearm serial IS covered by a Retail
		Sale disposition → no alarm at all."""
		from ffl_integrations.fastbound import webhook as fb
		with patch.object(fb, "_invoice_firearm_serials", return_value={"V2689"}), \
		     patch.object(fb.frappe, "get_all", return_value=["V2689"]), \
		     patch.object(fb, "_log_error") as log, \
		     patch.object(fb.frappe, "publish_realtime") as rt, \
		     patch.object(fb.frappe, "get_doc") as gd:
			fb._alert_invoice_serial_coverage("FB-DISP-D", ["INV-OK"])

		log.assert_not_called()
		rt.assert_not_called()
		gd.assert_not_called()


class DirectDisposalSkipsLinkedSale(FrappeTestCase):
	"""A disposition LINKED to a POS Invoice is a retail sale — the POS owns its
	stock-out, so it must never be routed to the direct-disposal stock sync (that
	silent routing is exactly how H5123 was wrongly headed out of stock)."""

	def setUp(self):
		super().setUp()
		self._prev_user = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._prev_user)
		super().tearDown()

	@patch("ffl_core.firearm.issue_serial")
	@patch("ffl_integrations.fastbound.webhook._serial_on_open_pos_invoice", return_value=False)
	@patch("ffl_integrations.fastbound.inventory_sync.in_stock_targets", return_value=[_TARGET])
	def test_sync_skips_when_disposition_has_linked_pos_invoice(self, _targets, _onpos, mock_issue):
		from ffl_integrations.fastbound import webhook as fb
		# A linked POS Invoice exists for this disposition_id → retail sale.
		with patch.object(fb.frappe, "get_all", return_value=[{"name": "INV-1"}]):
			fb._sync_fb_direct_disposal_stock("FB-DISP-LINKED", _DATA)
		_targets.assert_not_called()
		mock_issue.assert_not_called()


# is4473 with NO linked/matching invoice — the "never-linked" mismatch variant.
# Realistic anonymized committed payload (top-level is4473:true + type:"Sale" + items[]).
_DATA_4473 = _committed_payload("FB-DISP-4473-NOINV", [("FB-IT-1", "H5123-X")],
	is4473=True, type_="Sale")
_TARGET_4473 = {"serial": "H5123-X", "item_code": "GUN-X", "warehouse": "WH-1", "fb_id": "FB-IT-1"}


class DirectDisposal4473NoInvoice(FrappeTestCase):
	"""The 'never-linked' variant: a single-gun 4473 whose serial was mistyped on
	the invoice (invoice line V2689, 4473 run on H5123) never links to any invoice,
	so form4473.completed can't match it. When disposition.committed's is4473 gun
	reaches the direct-disposal path with NO linked invoice and NO open invoice
	carrying it, it must HOLD + alarm — never silently auto-dispose a possibly-
	mistyped retail serial. A genuine NON-4473 direct disposal still syncs."""

	def setUp(self):
		super().setUp()
		self._prev_user = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._prev_user)
		super().tearDown()

	@patch("ffl_core.firearm.issue_serial")
	@patch("ffl_integrations.fastbound.webhook._serial_on_open_pos_invoice", return_value=False)
	@patch("ffl_integrations.fastbound.inventory_sync.in_stock_targets", return_value=[_TARGET_4473])
	def test_is4473_no_invoice_holds_and_alarms(self, _targets, _onpos, mock_issue):
		from ffl_integrations.fastbound import webhook as fb
		frappe.set_user("Guest")
		with patch.object(fb.frappe, "get_all", return_value=[]), \
		     patch.object(fb, "_log_error") as log, \
		     patch.object(fb.frappe, "publish_realtime"):
			fb._sync_fb_direct_disposal_stock("FB-DISP-4473-NOINV", _DATA_4473)
		# HELD, not auto-disposed.
		mock_issue.assert_not_called()
		# Alarmed loudly.
		log.assert_called()
		self.assertIn("H5123-X", str(log.call_args))

	@patch("ffl_core.firearm.issue_serial", return_value="STE-OK")
	@patch("ffl_integrations.fastbound.webhook._serial_on_open_pos_invoice", return_value=False)
	@patch("ffl_integrations.fastbound.inventory_sync.in_stock_targets", return_value=[_TARGET])
	def test_non_4473_direct_disposal_still_issues(self, _targets, _onpos, mock_issue):
		"""A genuine non-4473 direct disposal (no is4473 flag) still syncs stock."""
		from ffl_integrations.fastbound import webhook as fb
		with patch.object(fb.frappe, "get_all", return_value=[]):
			fb._sync_fb_direct_disposal_stock("FB-DISP-XFER", _DATA)  # _DATA has no is4473
		mock_issue.assert_called_once()

	@patch("ffl_core.firearm.issue_serial")
	@patch("ffl_integrations.fastbound.webhook._serial_on_open_pos_invoice", return_value=False)
	@patch("ffl_integrations.fastbound.inventory_sync.in_stock_targets", return_value=[_TARGET_4473])
	def test_sync_honors_passed_is4473_when_payload_lacks_flag(self, _targets, _onpos, mock_issue):
		"""Defensive OR: even if the payload has NO is4473, a caller
		that derived is_4473=True (e.g. from a retail committed row) forces HOLD."""
		from ffl_integrations.fastbound import webhook as fb
		frappe.set_user("Guest")
		payload = {"items": [{"id": "FB-IT-1", "serial": "H5123-X"}]}  # NO is4473 flag
		with patch.object(fb.frappe, "get_all", return_value=[]), \
		     patch.object(fb, "_log_error") as log, \
		     patch.object(fb.frappe, "publish_realtime"):
			fb._sync_fb_direct_disposal_stock("FB-DISP-NOFLAG", payload, is_4473=True)
		mock_issue.assert_not_called()          # HELD
		log.assert_called()                     # alarmed
		self.assertIn("H5123-X", str(log.call_args))


class CommittedDerivesIs4473(FrappeTestCase):
	"""The committed handler ORs the payload is4473 flag with a rows-derived hint
	(any Retail Sale/Sale disposition for this fastbound_id), so a real retail 4473
	committed still HOLDs a mismatched serial even if a future payload drops the flag.
	"""

	def test_handler_ors_is4473_from_retail_committed_row(self):
		from ffl_integrations.fastbound import webhook as fb
		with patch.object(fb, "_sync_fb_direct_disposal_stock") as sync, \
		     patch.object(fb, "_alert_invoice_serial_coverage"), \
		     patch.object(fb, "_ensure_disposition_coverage"), \
		     patch.object(fb, "_fetch_disposition_serials", return_value=[]), \
		     patch.object(fb, "_our_serials_from_fb_items", return_value=[]), \
		     patch.object(fb, "now_datetime"), \
		     patch.object(fb.frappe.db, "set_value"), \
		     patch.object(fb.frappe, "publish_realtime"), \
		     patch.object(fb.frappe, "get_all") as ga:
			# 1st get_all → FFL Disposition rows (a Retail Sale); 2nd → POS Invoice names ([]).
			ga.side_effect = [[frappe._dict(name="D-1", disposition_type="Retail Sale")], []]
			fb._handle_disposition_committed({
				"dispositionId": "D", "type": "Sale",
				"items": [{"id": "FB-IT-9", "serial": "X"}],  # NO is4473 in payload
			})
		sync.assert_called_once()
		self.assertTrue(sync.call_args.kwargs.get("is_4473"),
			"is_4473 must be OR'd True from the retail committed row, not just the payload flag")
