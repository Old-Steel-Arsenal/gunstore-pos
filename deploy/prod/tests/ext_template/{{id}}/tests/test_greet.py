"""The checks every extension owes: switched off, no role, repeat call."""
import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.extensions import clear_enabled_cache
from {{id}}.api.greet import greet, ping

APP = "{{id}}"


def _switch(enabled: int) -> None:
	frappe.db.set_value("POS Extension", APP, "enabled", enabled)
	clear_enabled_cache()


def _welcomed() -> bool:
	"""Add a customer; True when the after_insert hook welcomed them."""
	name = f"_Test {APP} {frappe.generate_hash(length=8)}"
	frappe.local.message_log = []
	frappe.get_doc({"doctype": "Customer", "customer_name": name}).insert(ignore_permissions=True)
	return any(f"Welcome, {name}!" in str(m) for m in frappe.local.message_log)


class TestGreet(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.extensions.registry import sync
		sync()                                               # the POS Extension row the store's switch lives on
		if not frappe.db.exists("Customer", {"customer_name": "_Test {{title}} Customer"}):
			frappe.get_doc({"doctype": "Customer", "customer_name": "_Test {{title}} Customer"}).insert(ignore_permissions=True)
		cls.customer = frappe.db.get_value("Customer", {"customer_name": "_Test {{title}} Customer"}, "name")

	def setUp(self):
		frappe.set_user("Administrator")
		_switch(1)

	def tearDown(self):
		frappe.set_user("Administrator")

	# Switched off (Settings → Extensions): every endpoint refuses, every hook does nothing.
	def test_switched_off_endpoints_refuse(self):
		_switch(0)
		with self.assertRaises(frappe.PermissionError):
			greet(self.customer)
		with self.assertRaises(frappe.PermissionError):
			ping()

	def test_switched_off_hook_does_nothing(self):
		_switch(0)
		self.assertFalse(_welcomed())
		_switch(1)
		self.assertTrue(_welcomed())

	# No role: a caller holding none of the endpoint's roles is refused.
	def test_without_a_counter_role_refuses(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			greet(self.customer)
		with self.assertRaises(frappe.PermissionError):
			ping()

	# Repeat call: a retry must be safe. greet only reads, so a second call answers the same
	# and writes nothing; an endpoint that writes proves here that a repeat does not write twice.
	def test_repeat_call_answers_the_same_and_writes_nothing(self):
		before = frappe.db.get_value("Customer", self.customer, "modified")
		self.assertEqual(greet(self.customer), greet(self.customer))
		self.assertEqual(frappe.db.get_value("Customer", self.customer, "modified"), before)

	def test_greets(self):
		self.assertEqual(greet(self.customer), {"message": "Hello _Test {{title}} Customer"})
		self.assertEqual(ping(), "pong")
