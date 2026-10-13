"""backfill_condition_attributes — the attributes-only re-push.

Machine pushes never carry ``attributes``, so a grade / ``is_used`` fix never
reaches a product that is already on the shop. The backfill delivers it, and the
one thing it must never do is ride along with anything else: the 2026-08-12
incident was a full push overwriting hand-edited descriptions.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce import condition_backfill as cb
from ffl_woo_sync.woocommerce import mapping
from ffl_woo_sync.woocommerce.client import WooCommerceError

ATTR_IDS = {"pa_caliber": 5, "pa_condition": 7, "pa_manufacturer": 8, "pa_model": 9}


def _item(**kw):
	row = dict(name="UNI-1", item_code="UNI-1", is_firearm=1, uniform_sell_price=1,
		is_used=0, is_antique=0, condition_grade="Very Good", caliber="9mm",
		item_group="Pistol", manufacturer="Glock", model_name="19")
	row.update(kw)
	return frappe._dict(row)


def _serial(**kw):
	row = dict(name="SN-1", item_code="PER-1", condition_grade="", caliber="",
		manufacturer="", model_name="")
	row.update(kw)
	return frappe._dict(row)


def _client(update_response=None):
	wc = MagicMock()
	wc.get_attribute_ids.return_value = dict(ATTR_IDS)
	wc.batch_update_products.side_effect = update_response or (
		lambda updates: {"update": [{"id": u["id"]} for u in updates]})
	return wc


def _targets(*pairs):
	return [{"doctype": dt, "name": n, "woo_id": w} for dt, n, w in pairs]


class _Run:
	"""Run the backfill against mocked Woo + mocked docs."""

	def __init__(self, test, targets, docs, wc=None, skipped=None, **kwargs):
		self.wc = wc or _client()
		self.kwargs = kwargs
		self.patches = [
			patch.object(cb, "_select_targets", return_value=(targets, list(skipped or []))),
			patch.object(cb.WooCommerceClient, "from_settings", return_value=self.wc),
			patch("frappe.get_doc", side_effect=lambda dt, name=None: docs[(dt, name)]),
		]

	def __enter__(self):
		for p in self.patches:
			p.start()
		return cb.backfill_condition_attributes(**self.kwargs), self.wc

	def __exit__(self, *exc):
		for p in self.patches:
			p.stop()


class TestAttributesOnly(FrappeTestCase):
	DOCS = {
		("Item", "UNI-1"): _item(),
		("Serial No", "SN-1"): _serial(condition_grade="Good"),
		("Item", "PER-1"): _item(name="PER-1", uniform_sell_price=0, is_used=1,
			caliber=".45", manufacturer="Colt", model_name="1911"),
	}

	def test_sends_only_id_and_attributes(self):
		targets = _targets(("Item", "UNI-1", "101"), ("Serial No", "SN-1", "202"))
		with _Run(self, targets, self.DOCS) as (res, wc):
			sent = wc.batch_update_products.call_args.args[0]
		self.assertEqual(len(sent), 2)
		for update in sent:
			self.assertEqual(set(update), {"id", "attributes"})
		self.assertEqual((res["pushed"], res["skipped"], res["errors"]), (2, 0, 0))

	def test_not_used_item_with_stale_default_grade_pushes_new(self):
		# is_used=0 + "Very Good": the production shape. Must correct to New.
		with _Run(self, _targets(("Item", "UNI-1", "101")), self.DOCS) as (_, wc):
			attrs = wc.batch_update_products.call_args.args[0][0]["attributes"]
		by_slug = {a["slug"]: a for a in attrs}
		self.assertEqual(by_slug["pa_condition"]["options"], ["New"])
		self.assertEqual(by_slug["pa_condition"]["id"], 7)  # global taxonomy, not custom

	def test_used_serial_pushes_its_own_grade(self):
		with _Run(self, _targets(("Serial No", "SN-1", "202")), self.DOCS) as (_, wc):
			attrs = wc.batch_update_products.call_args.args[0][0]["attributes"]
		by_slug = {a["slug"]: a["options"] for a in attrs}
		self.assertEqual(by_slug["pa_condition"], ["Good"])  # parent Item is_used=1

	def test_used_ungraded_serial_drops_condition_but_still_sends_attributes(self):
		docs = {**self.DOCS, ("Serial No", "SN-1"): _serial(condition_grade="")}
		with _Run(self, _targets(("Serial No", "SN-1", "202")), docs) as (_, wc):
			update = wc.batch_update_products.call_args.args[0][0]
		slugs = {a["slug"] for a in update["attributes"]}
		self.assertNotIn("pa_condition", slugs)
		self.assertIn("pa_caliber", slugs)  # falls back to the Item's caliber

	def test_used_ungraded_with_nothing_else_sends_empty_attributes_list(self):
		# Empty list must still be sent: omitting the key leaves the stale term.
		docs = {
			("Item", "PER-1"): _item(name="PER-1", uniform_sell_price=0, is_used=1,
				caliber="", manufacturer="", model_name=""),
			("Serial No", "SN-1"): _serial(condition_grade=""),
		}
		with _Run(self, _targets(("Serial No", "SN-1", "202")), docs) as (res, wc):
			update = wc.batch_update_products.call_args.args[0][0]
		self.assertEqual(update["attributes"], [])
		self.assertEqual(res["pushed"], 1)

	def test_payload_builders_return_nothing_but_attributes(self):
		with patch("frappe.get_doc", side_effect=lambda dt, name=None: self.DOCS[(dt, name)]):
			self.assertEqual(set(mapping.item_attributes_payload("UNI-1", ATTR_IDS)), {"attributes"})
			self.assertEqual(set(mapping.serial_attributes_payload("SN-1", ATTR_IDS)), {"attributes"})

	def test_antique_with_is_used_zero_is_never_new(self):
		docs = {("Item", "UNI-1"): _item(is_antique=1, condition_grade="Fair")}
		with _Run(self, _targets(("Item", "UNI-1", "101")), docs) as (_, wc):
			attrs = wc.batch_update_products.call_args.args[0][0]["attributes"]
		self.assertEqual({a["slug"]: a["options"] for a in attrs}["pa_condition"], ["Fair"])


class TestRunMechanics(FrappeTestCase):
	DOCS = TestAttributesOnly.DOCS

	def test_dry_run_sends_nothing(self):
		targets = _targets(("Item", "UNI-1", "101"), ("Serial No", "SN-1", "202"))
		with _Run(self, targets, self.DOCS, dry_run=1) as (res, wc):
			wc.batch_update_products.assert_not_called()
		self.assertEqual((res["pushed"], res["would_push"], res["dry_run"]), (0, 2, True))

	def test_batches_of_batch_size(self):
		docs = {("Item", f"I{n}"): _item(name=f"I{n}") for n in range(cb._BATCH_SIZE + 3)}
		targets = _targets(*[("Item", f"I{n}", str(1000 + n)) for n in range(cb._BATCH_SIZE + 3)])
		with _Run(self, targets, docs) as (res, wc):
			sizes = [len(c.args[0]) for c in wc.batch_update_products.call_args_list]
		self.assertEqual(sizes, [cb._BATCH_SIZE, 3])
		self.assertEqual(res["pushed"], cb._BATCH_SIZE + 3)

	def test_entry_level_error_is_counted_not_fatal(self):
		def respond(updates):
			return {"update": [
				{"id": updates[0]["id"], "error": {"message": "nope"}},
				*({"id": u["id"]} for u in updates[1:])]}
		targets = _targets(("Item", "UNI-1", "101"), ("Serial No", "SN-1", "202"))
		with _Run(self, targets, TestAttributesOnly.DOCS, wc=_client(respond)) as (res, _):
			pass
		self.assertEqual((res["pushed"], res["errors"]), (1, 1))
		self.assertEqual(res["error_details"][0]["error"], "nope")

	def test_unanswered_entry_is_an_error(self):
		wc = _client(lambda updates: {"update": []})
		with _Run(self, _targets(("Item", "UNI-1", "101")), TestAttributesOnly.DOCS, wc=wc) as (res, _):
			pass
		self.assertEqual((res["pushed"], res["errors"]), (0, 1))

	def test_request_failure_aborts_the_rest(self):
		n = cb._BATCH_SIZE + 2
		docs = {("Item", f"I{i}"): _item(name=f"I{i}") for i in range(n)}
		targets = _targets(*[("Item", f"I{i}", str(1000 + i)) for i in range(n)])
		wc = _client(MagicMock(side_effect=WooCommerceError("boom", status_code=403)))
		with patch.object(cb, "_push_with_retry", side_effect=lambda fn, **kw: fn()):
			with _Run(self, targets, docs, wc=wc) as (res, _):
				self.assertEqual(wc.batch_update_products.call_count, 1)
		self.assertEqual(res["errors"], cb._BATCH_SIZE)
		self.assertEqual(res["skipped_by_reason"], {"aborted": 2})
		self.assertEqual(res["pushed"], 0)

	def test_unresolved_attribute_taxonomy_is_an_error_and_not_sent(self):
		wc = _client()
		wc.get_attribute_ids.return_value = {"pa_caliber": 5}  # no pa_condition etc.
		with _Run(self, _targets(("Item", "UNI-1", "101")), TestAttributesOnly.DOCS, wc=wc) as (res, _):
			wc.batch_update_products.assert_not_called()
		self.assertEqual((res["pushed"], res["errors"]), (0, 1))

	def test_duplicate_product_id_is_pushed_once(self):
		targets = _targets(("Item", "UNI-1", "101"), ("Serial No", "SN-1", "101"))
		with _Run(self, targets, TestAttributesOnly.DOCS) as (res, wc):
			self.assertEqual(len(wc.batch_update_products.call_args.args[0]), 1)
		self.assertEqual(res["skipped_by_reason"], {"duplicate_product_id": 1})

	def test_nothing_to_do_makes_no_request(self):
		with _Run(self, [], {}) as (res, wc):
			wc.batch_update_products.assert_not_called()
		self.assertEqual((res["pushed"], res["skipped"], res["errors"]), (0, 0, 0))


class TestGateAndWiring(FrappeTestCase):
	def test_is_whitelisted(self):
		self.assertIn(cb.backfill_condition_attributes, frappe.whitelisted)

	def test_system_manager_only(self):
		frappe.set_user("Guest")
		try:
			with patch.object(cb, "_select_targets", side_effect=AssertionError("ran")):
				with self.assertRaises(frappe.PermissionError):
					cb.backfill_condition_attributes()
		finally:
			frappe.set_user("Administrator")

	def test_not_wired_into_patches(self):
		from pathlib import Path
		text = (Path(frappe.get_app_path("ffl_woo_sync")) / "patches.txt").read_text()
		self.assertNotIn("condition_backfill", text)


class TestSelectTargets(FrappeTestCase):
	"""The no-list sweep and the explicit lists resolve to the right products."""

	FIREARMS = {
		"UNI-1": {"name": "UNI-1", "uniform_sell_price": 1, "woo_product_id": "101", "woo_delisted": 0},
		"UNI-HIDDEN": {"name": "UNI-HIDDEN", "uniform_sell_price": 1, "woo_product_id": "102", "woo_delisted": 1},
		"UNI-NEVER": {"name": "UNI-NEVER", "uniform_sell_price": 1, "woo_product_id": "", "woo_delisted": 0},
		"PER-1": {"name": "PER-1", "uniform_sell_price": 0, "woo_product_id": "", "woo_delisted": 0},
	}

	def _select(self, serials=(), item_codes=(), get_all=()):
		with patch.object(cb, "_firearm_items", return_value=self.FIREARMS), \
			patch.object(cb, "_published_serials", return_value=[
				{"name": "SN-A", "item_code": "PER-1", "woo_id": "301"}]) as ps, \
			patch("frappe.get_all", return_value=list(get_all)):
			return cb._select_targets(list(serials), list(item_codes)), ps

	def test_sweep_takes_live_uniform_items_and_per_serial_serials(self):
		((targets, skipped), ps) = self._select()
		self.assertEqual(
			sorted((t["doctype"], t["name"]) for t in targets),
			[("Item", "UNI-1"), ("Serial No", "SN-A")])
		self.assertEqual(ps.call_args.args[0], ["PER-1"])  # per-serial items only
		self.assertEqual(skipped, [])

	def test_item_code_list_expands_per_serial_items_and_skips_non_firearms(self):
		((targets, skipped), _) = self._select(item_codes=["PER-1", "UNI-HIDDEN", "NOPE"])
		self.assertEqual([(t["doctype"], t["name"]) for t in targets], [("Serial No", "SN-A")])
		self.assertEqual(
			sorted((s["name"], s["reason"]) for s in skipped),
			[("NOPE", "not_a_firearm_item"), ("UNI-HIDDEN", "delisted")])

	def test_serial_list_skips_uniform_item_serials_and_unknowns(self):
		rows = [
			{"name": "SN-OK", "item_code": "PER-1", "woo_product_id": "401", "woo_delisted": 0},
			{"name": "SN-UNI", "item_code": "UNI-1", "woo_product_id": "402", "woo_delisted": 0},
			{"name": "SN-NOID", "item_code": "PER-1", "woo_product_id": "", "woo_delisted": 0},
			{"name": "SN-HID", "item_code": "PER-1", "woo_product_id": "403", "woo_delisted": 1},
		]
		((targets, skipped), _) = self._select(
			serials=["SN-OK", "SN-UNI", "SN-NOID", "SN-HID", "SN-GONE"], get_all=rows)
		self.assertEqual([t["name"] for t in targets], ["SN-OK"])
		self.assertEqual(
			{s["name"]: s["reason"] for s in skipped},
			{"SN-UNI": "uniform_item_use_item_code", "SN-NOID": "no_product_id",
			 "SN-HID": "delisted", "SN-GONE": "not_found"})
