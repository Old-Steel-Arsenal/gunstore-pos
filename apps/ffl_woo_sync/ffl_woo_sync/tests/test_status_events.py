"""Drop-ship status events POS → shop (``woocommerce/status_events.py``).

Pure pieces (transition filter, signature, event body, config gate) are tested
without HTTP; delivery is exercised through a patched ``requests.post``; the
doc_event wiring in ``ffl_integrations.distributor.tasks`` is checked with a
fake Document that carries a before-image.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce import status_events as se


class TestShouldNotify(FrappeTestCase):
	def test_transitions_into_a_notifiable_state_fire(self):
		for status in ("Placed", "Hold", "Partially Shipped", "Complete", "Cancelled", "Error"):
			self.assertTrue(se.should_notify("Queued", status), status)

	def test_pos_internal_states_stay_silent(self):
		self.assertFalse(se.should_notify(None, "Draft"))
		self.assertFalse(se.should_notify("Draft", "Queued"))
		self.assertFalse(se.should_notify("Placed", "Acknowledged"))

	def test_same_state_resave_is_not_a_transition(self):
		"""Idempotent polling re-saves a Placed order as Placed; no event."""
		self.assertFalse(se.should_notify("Placed", "Placed"))
		self.assertFalse(se.should_notify("Hold ", "Hold"))

	def test_first_save_into_a_notifiable_state_fires(self):
		self.assertTrue(se.should_notify(None, "Placed"))

	def test_new_tracking_within_one_status_fires(self):
		"""Parcel 2 of a Partially Shipped leg changes no status."""
		self.assertTrue(se.should_notify("Partially Shipped", "Partially Shipped", {"T1"}, {"T1", "T2"}))
		self.assertFalse(se.should_notify("Partially Shipped", "Partially Shipped", {"T1"}, {"T1"}))
		self.assertFalse(se.should_notify("Queued", "Queued", set(), {"T1"}))

	def test_tracking_numbers_reads_the_line_csv_echoes(self):
		lines = [{"tracking_numbers": "T1, T2"}, {"tracking_numbers": "T2"}, {}, None]
		self.assertEqual(se.tracking_numbers(lines), {"T1", "T2"})


class TestSignature(FrappeTestCase):
	def test_hmac_over_timestamp_dot_body(self):
		body = b'{"a":1}'
		expected = hmac.new(b"s3cret", b"1700000000." + body, hashlib.sha256).hexdigest()
		self.assertEqual(se.sign("s3cret", "1700000000", body), expected)


class TestBuildEvent(FrappeTestCase):
	def _do(self, **over):
		base = {
			"name": "DO-2609-0004", "status": "Partially Shipped", "modified": "2026-09-22 10:00:00",
			"distributor": "RSR", "hold_reason": "", "web_ref": "W123",
			"last_poll_at": "2026-09-16 08:00:00",
			"lines": [
				{"item_code": "GLK19", "sku": "GLK19G5", "wish_qty": 1, "fulfillment": "fds",
				 "line_status": "Shipped", "tracking_numbers": "9400A,9400B"},
				{"item_code": "MAG", "sku": "PMAG30", "wish_qty": 2, "fulfillment": "direct",
				 "line_status": "Acked", "tracking_numbers": "9400A,"},
			],
		}
		base.update(over)
		return frappe._dict(base)

	def test_event_shape(self):
		ev = se.build_event(self._do(), frappe._dict(woo_order_id=1609, site="retail"))
		self.assertEqual(ev["event_id"], "DO-2609-0004:Partially Shipped:2026-09-22 10:00:00")
		self.assertEqual(ev["woo_order_id"], "1609")
		self.assertEqual(ev["status"], "Partially Shipped")
		self.assertEqual([l["item_code"] for l in ev["lines"]], ["GLK19", "MAG"])
		self.assertEqual(ev["lines"][1]["qty"], 2)
		self.assertEqual(ev["lines"][1]["tracking"], ["9400A"])
		self.assertEqual(ev["modified"], "2026-09-22 10:00:00")
		self.assertEqual(ev["placed_at"], "")

	def test_tracking_is_deduped_across_lines_and_dated_from_the_poll(self):
		"""RSR echoes one box's number on every line in it — one parcel, not two."""
		ev = se.build_event(self._do(), frappe._dict(woo_order_id=1, site="retail"))
		self.assertEqual([t["number"] for t in ev["tracking"]], ["9400A", "9400B"])
		self.assertEqual(ev["tracking"][0]["shipped_at"], "2026-09-16")

	def test_json_serialisable(self):
		ev = se.build_event(self._do(), frappe._dict(woo_order_id=1, site="retail"))
		json.dumps(ev)


class TestOutboundConfig(FrappeTestCase):
	def test_unknown_site_is_off(self):
		self.assertIsNone(se.outbound_config("nope"))

	def test_blank_secret_is_off(self):
		settings = MagicMock()
		settings.get.side_effect = lambda k: {"store_url": "https://shop.test/"}.get(k)
		settings.get_password.return_value = ""
		with patch("frappe.get_single", return_value=settings):
			self.assertIsNone(se.outbound_config("retail"))

	def test_configured_store_returns_url_and_secret(self):
		settings = MagicMock()
		settings.get.side_effect = lambda k: {"store_url": "https://shop.test/"}.get(k)
		settings.get_password.return_value = "s3cret"
		with patch("frappe.get_single", return_value=settings):
			self.assertEqual(se.outbound_config("retail"), ("https://shop.test", "s3cret"))


class TestDeliver(FrappeTestCase):
	def test_posts_signed_body_to_the_ffl_core_route(self):
		resp = MagicMock(status_code=200)
		with patch.object(se.requests, "post", return_value=resp) as post, \
			 patch.object(se.time, "time", return_value=1700000000):
			code = se.deliver("https://shop.test", "s3cret", {"event_id": "x"})
		self.assertEqual(code, 200)
		args, kw = post.call_args
		self.assertEqual(args[0], "https://shop.test/wp-json/ffl-core/v1/dropship-status")
		self.assertEqual(kw["headers"]["x-osa-timestamp"], "1700000000")
		self.assertEqual(kw["headers"]["x-osa-signature"], se.sign("s3cret", "1700000000", kw["data"]))
		self.assertEqual(json.loads(kw["data"]), {"event_id": "x"})


class TestDocEventWiring(FrappeTestCase):
	"""``tasks.on_distributor_order_update`` enqueues the event after commit on a
	real transition, and only for a web order's leg."""

	def _doc(self, before_status, status, source="Woo Online Order", before_lines=None, lines=None):
		doc = MagicMock()
		data = {"status": status, "source_doctype": source, "source_name": "WOO-ORD-1", "lines": lines}
		doc.get.side_effect = data.get
		doc.name = "DO-1"
		doc.get_doc_before_save.return_value = (
			frappe._dict(status=before_status, lines=before_lines) if before_status is not None else None)
		return doc

	def test_second_parcel_on_a_partially_shipped_leg_enqueues(self):
		from ffl_integrations.distributor import tasks

		with patch.object(tasks, "enqueue_distributor") as enq:
			tasks.on_distributor_order_update(self._doc(
				"Partially Shipped", "Partially Shipped",
				before_lines=[{"tracking_numbers": "T1"}],
				lines=[{"tracking_numbers": "T1"}, {"tracking_numbers": "T2"}]))
		enq.assert_called_once()

	def test_transition_enqueues_after_commit(self):
		from ffl_integrations.distributor import tasks

		captured = {}

		def _enq(method, **kw):
			captured["method"] = method
			captured.update(kw)

		with patch.object(tasks, "enqueue_distributor", side_effect=_enq):
			tasks.on_distributor_order_update(self._doc("Queued", "Placed"))
		self.assertEqual(captured["method"], "ffl_woo_sync.woocommerce.status_events.post_status_event")
		self.assertEqual(captured["do_name"], "DO-1")
		self.assertTrue(captured["enqueue_after_commit"])

	def test_same_state_resave_does_not_enqueue(self):
		from ffl_integrations.distributor import tasks

		with patch.object(tasks, "enqueue_distributor") as enq:
			tasks.on_distributor_order_update(self._doc("Placed", "Placed"))
		enq.assert_not_called()

	def test_counter_order_leg_is_not_reported_to_the_shop(self):
		from ffl_integrations.distributor import tasks

		with patch.object(tasks, "enqueue_distributor") as enq:
			tasks.on_distributor_order_update(self._doc("Queued", "Placed", source="Sales Invoice"))
		enq.assert_not_called()

	def test_a_plain_dict_without_a_before_image_stays_silent(self):
		"""The existing completion tests hand in frappe._dict docs; no before-image
		means no transition we can vouch for."""
		from ffl_integrations.distributor import tasks

		doc = frappe._dict(status="Placed", source_doctype="Woo Online Order", source_name="WOO-ORD-1")
		with patch.object(tasks, "enqueue_distributor") as enq:
			tasks.on_distributor_order_update(doc)
		enq.assert_not_called()


class TestResendSweep(FrappeTestCase):
	def test_sweep_is_a_no_op_without_a_configured_store(self):
		with patch.object(se, "outbound_config", return_value=None), \
			 patch("frappe.enqueue") as enq:
			out = se.resend_status_events()
		self.assertEqual(out["enqueued"], 0)
		enq.assert_not_called()

	def test_sweep_enqueues_one_quiet_job_per_unacknowledged_leg(self):
		with patch.object(se, "outbound_config", return_value=("https://shop.test", "s")), \
			 patch.object(se, "_unacknowledged_names", return_value=["A", "B"]), \
			 patch("frappe.enqueue") as enq:
			out = se.resend_status_events()
		self.assertEqual(out["enqueued"], 2)
		self.assertEqual([c.kwargs["do_name"] for c in enq.call_args_list], ["A", "B"])
		self.assertTrue(all(c.kwargs["quiet"] for c in enq.call_args_list))

	def test_sweep_query_compares_status_to_ack_in_sql(self):
		"""Acked legs must not fill the batch: the comparison is in the WHERE,
		not in Python after a LIMIT."""
		sql = se._unacknowledged_query().get_sql()
		self.assertIn("`status` IN ('Cancelled','Complete','Error','Hold','Partially Shipped','Placed')", sql)
		self.assertIn("`woo_notified_status`<>`status`", sql.replace(" ", ""))
		self.assertIn("LIMIT 200", sql)

	def test_successful_delivery_records_the_acknowledged_status(self):
		do = frappe._dict(name="DO-1", status="Placed", modified="m", source_doctype="Woo Online Order",
			source_name="WOO-1", lines=[], distributor="RSR")
		with patch("frappe.get_doc", return_value=do), \
			 patch("frappe.db.get_value", return_value={"woo_order_id": 1, "site": "retail"}), \
			 patch.object(se, "outbound_config", return_value=("https://shop.test", "s")), \
			 patch.object(se, "deliver", return_value=200), \
			 patch("frappe.db.set_value") as sv:
			out = se.post_status_event("DO-1")
		self.assertTrue(out["sent"])
		sv.assert_called_once_with("Distributor Order", "DO-1", "woo_notified_status", "Placed",
			update_modified=False)


class TestDeliveryFailures(FrappeTestCase):
	def _post(self, exc, quiet=False):
		do = frappe._dict(name="DO-1", status="Placed", modified="m", source_doctype="Woo Online Order",
			source_name="WOO-1", lines=[], distributor="RSR")
		with patch("frappe.get_doc", return_value=do), \
			 patch("frappe.db.get_value", return_value={"woo_order_id": 1, "site": "retail"}), \
			 patch.object(se, "outbound_config", return_value=("https://shop.test", "s")), \
			 patch.object(se, "deliver", side_effect=exc), \
			 patch("frappe.log_error") as log, \
			 patch("frappe.db.set_value") as sv:
			out = se.post_status_event("DO-1", quiet=quiet)
		return out, log, sv

	@staticmethod
	def _http(code, wp_code=None):
		import requests
		resp = MagicMock(); resp.status_code = code
		resp.json.return_value = {"code": wp_code} if wp_code else {}
		return requests.HTTPError(response=resp)

	def _acked(self, sv, value):
		sv.assert_called_once_with("Distributor Order", "DO-1", "woo_notified_status", value,
			update_modified=False)

	def test_no_such_order_is_logged_once_and_settled(self):
		out, log, sv = self._post(self._http(404, "ffl_dropship_no_order"), quiet=True)
		self.assertEqual(out["reason"], "refused")
		log.assert_called_once()
		self._acked(sv, "Placed")

	def test_a_404_from_a_shop_without_the_route_stays_retryable(self):
		"""rest_no_route: the storefront plugin not deployed yet / rolled back — the events
		must reach it once it is."""
		out, log, sv = self._post(self._http(404, "rest_no_route"))
		self.assertEqual(out["reason"], "delivery failed")
		self._acked(sv, "")

	def test_auth_failure_stays_retryable(self):
		out, log, sv = self._post(self._http(401))
		self.assertEqual(out["reason"], "delivery failed")
		log.assert_called_once()
		self._acked(sv, "")

	def test_a_busy_shop_is_retried_without_an_error_log(self):
		out, log, sv = self._post(self._http(503))
		log.assert_not_called()
		self._acked(sv, "")

	def test_failure_clears_the_ack_so_a_tracking_only_event_is_resent(self):
		"""The ack already equals the status after parcel 1; a failed parcel-2
		event must not look delivered."""
		out, log, sv = self._post(ConnectionError("down"), quiet=True)
		self.assertFalse(out["sent"])
		log.assert_not_called()
		self._acked(sv, "")


class TestDistributorLeadRepush(FrappeTestCase):
	def test_the_hook_repushes_the_items_in_batches(self):
		"""ffl_integrations names the Items (``dropship_items_changed``, fired on a
		house's lead-time edit); this app queues their pushes."""
		from ffl_woo_sync.woocommerce import doc_events as de
		from ffl_woo_sync.woocommerce import tasks

		codes = [f"I{n:03}" for n in range(tasks.DISTRIBUTOR_REPUSH_CHUNK + 1)]
		with patch.object(de, "_skip_push", return_value=False), \
			 patch("frappe.enqueue") as enq:
			tasks.dropship_items_changed(codes + ["I000"])
		batches = [c.kwargs["item_codes"] for c in enq.call_args_list]
		self.assertEqual([len(b) for b in batches], [tasks.DISTRIBUTOR_REPUSH_CHUNK, 1])
		self.assertEqual(sum(batches, []), codes)
		self.assertTrue(all(c.args[0] == "ffl_woo_sync.woocommerce.tasks.push_items_task"
			and c.kwargs["enqueue_after_commit"] for c in enq.call_args_list))

	def test_nothing_during_migrate_or_an_inbound_echo(self):
		from ffl_woo_sync.woocommerce import doc_events as de
		from ffl_woo_sync.woocommerce import tasks

		with patch.object(de, "_skip_push", return_value=True), patch("frappe.enqueue") as enq:
			tasks.dropship_items_changed(["I1"])
		enq.assert_not_called()

	def test_batch_push_shares_one_attribute_lookup(self):
		from ffl_woo_sync.woocommerce import sync, tasks
		client = MagicMock(); client.get_attribute_ids.return_value = {"Caliber": 1}
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings", return_value=client), \
			 patch.object(sync, "push_item", side_effect=[{}, RuntimeError("bad"), {}]) as push, \
			 patch("frappe.log_error") as log:
			tasks.push_items_task(["A", "B", "C"])
		client.get_attribute_ids.assert_called_once()
		self.assertEqual([c.args[0] for c in push.call_args_list], ["A", "B", "C"])
		self.assertTrue(all(c.kwargs["attr_ids"] == {"Caliber": 1} and c.kwargs["client"] is client
			for c in push.call_args_list))
		log.assert_called_once()   # the bad SKU, and the batch carried on
