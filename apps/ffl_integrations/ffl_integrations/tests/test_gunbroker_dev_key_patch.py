"""The upgrade that takes the DevKeys off the Settings form.

A site that had its keys on the form must come out of `bench migrate` still
able to talk to GunBroker, with nothing left in the database that a settings
page could show. The site config is written to a scratch file here so the test
never touches the real site_config.json; ``frappe.conf`` is restored on exit.
"""
from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils.password import get_decrypted_password, set_encrypted_password

from ffl_integrations.patches.move_gunbroker_dev_keys_to_site_config import (
	FIELDS,
	SETTINGS,
	execute,
)


def _stored(field: str) -> str:
	return get_decrypted_password(SETTINGS, SETTINGS, field, raise_exception=False) or ""


class TheDevKeysMoveOffTheForm(FrappeTestCase):
	def setUp(self):
		fd, self.path = tempfile.mkstemp(suffix=".json")
		os.write(fd, b"{}")
		os.close(fd)
		self.addCleanup(os.remove, self.path)
		conf = patch.dict(frappe.local.conf)
		conf.start()
		self.addCleanup(conf.stop)
		for conf_key in FIELDS.values():
			frappe.local.conf.pop(conf_key, None)

	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def _written(self) -> dict:
		with open(self.path) as f:
			return json.load(f)

	def test_a_key_on_the_form_lands_in_site_config_and_leaves_the_form(self):
		set_encrypted_password(SETTINGS, SETTINGS, "prod-secret", "dev_key")
		set_encrypted_password(SETTINGS, SETTINGS, "sandbox-secret", "sandbox_dev_key")
		outcome = execute(site_config_path=self.path)
		self.assertEqual(outcome, {
			"gunbroker_dev_key": "moved", "gunbroker_sandbox_dev_key": "moved"})
		self.assertEqual(self._written(), {
			"gunbroker_dev_key": "prod-secret", "gunbroker_sandbox_dev_key": "sandbox-secret"})
		self.assertEqual(_stored("dev_key"), "")
		self.assertEqual(_stored("sandbox_dev_key"), "")
		self.assertFalse(frappe.db.exists("Singles", {"doctype": SETTINGS, "field": "dev_key"}))

	def test_an_operator_set_site_config_wins_over_the_form(self):
		"""The explicit setting is the one somebody chose; the form's copy is
		whatever was left behind. It is still deleted — nothing reads it."""
		frappe.local.conf["gunbroker_dev_key"] = "operator-set"
		set_encrypted_password(SETTINGS, SETTINGS, "stale-form-key", "dev_key")
		outcome = execute(site_config_path=self.path)
		self.assertEqual(outcome["gunbroker_dev_key"], "kept")
		self.assertEqual(self._written(), {})
		self.assertEqual(_stored("dev_key"), "")

	def test_a_form_with_no_keys_writes_nothing(self):
		for field in FIELDS:
			frappe.db.delete("__Auth", {"doctype": SETTINGS, "name": SETTINGS, "fieldname": field})
		outcome = execute(site_config_path=self.path)
		self.assertEqual(set(outcome.values()), {"absent"})
		self.assertEqual(self._written(), {})

	def test_running_it_again_is_a_no_op(self):
		set_encrypted_password(SETTINGS, SETTINGS, "prod-secret", "dev_key")
		execute(site_config_path=self.path)
		with open(self.path, "w") as f:
			f.write("{}")
		self.assertEqual(set(execute(site_config_path=self.path).values()), {"absent"})
		self.assertEqual(self._written(), {})
