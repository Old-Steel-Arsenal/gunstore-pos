"""Lipsey's client — login, the body-level auth check, the one re-login, the
customer check, and above all that an order whose answer never arrived is
never sent twice. Every call goes to a fake session (the API is IP-allowlisted)."""
from __future__ import annotations

import unittest
from datetime import date

from ffl_integrations.lipseys import api_client as lip
from ffl_integrations.lipseys.api_client import (
	LipseysAuthError, LipseysClient, LipseysConfig, LipseysConfigError, LipseysError,
	LipseysPlacementUnknown,
)
from ffl_integrations.tests import lipseys_synth as syn

ORDER = {"PONumber": "DO-TEST-0001", "DisableEmail": False,
	"Items": [{"ItemNo": syn.sku(1), "Quantity": 1}]}


def _client(routes, *, customer="", account="dealer"):
	cfg = LipseysConfig(email="dealer@example.com", password="pw-test", account=account,
		customer_number=customer, base_url="https://api.example.invalid/api/Integration")
	session = syn.FakeSession({"Authentication/Login": syn.login_ok(), **routes})
	return LipseysClient(cfg, session=session), session


class TestLogin(unittest.TestCase):
	def test_login_sends_credentials_in_the_body_and_our_user_agent(self):
		client, session = _client({})
		info = client.login()
		call = session.calls[0]
		self.assertEqual(call["path"], "Authentication/Login")
		self.assertEqual(call["body"]["Email"], "dealer@example.com")
		self.assertEqual(str(call["body"]["Password"]), "pw-test")
		self.assertNotIn("Token", call["headers"])
		self.assertTrue(call["headers"]["User-Agent"].startswith("opengunstore-lipseys/"))
		self.assertEqual(info["cusNo"], syn.CUSTOMER)

	def test_the_token_rides_in_the_token_header_and_login_happens_once(self):
		client, session = _client({"Items/ValidateItem": syn.validate(1)})
		client.validate_item(syn.sku(1))
		client.validate_item(syn.sku(1))
		self.assertEqual(session.paths(), ["Authentication/Login", "Items/ValidateItem",
			"Items/ValidateItem"])
		self.assertEqual(str(session.calls[1]["headers"]["Token"]), syn.TOKEN)
		self.assertEqual(session.calls[1]["body"], syn.sku(1))

	def test_refused_login_is_an_auth_error(self):
		client, _ = _client({})
		client.session.routes["Authentication/Login"] = syn.login_refused()
		with self.assertRaises(LipseysAuthError) as ctx:
			client.login()
		self.assertIn("Invalid email or password", str(ctx.exception))

	def test_missing_credentials_never_reach_the_wire(self):
		cfg = LipseysConfig(email="", password="")
		session = syn.FakeSession({})
		with self.assertRaises(LipseysConfigError):
			LipseysClient(cfg, session=session).login()
		self.assertEqual(session.calls, [])

	def test_a_login_for_another_customer_is_refused(self):
		client, _ = _client({}, customer="123456")
		with self.assertRaises(LipseysAuthError) as ctx:
			client.login()
		self.assertIn("123456", str(ctx.exception))
		self.assertIsNone(client._token)

	def test_matching_customer_number_passes(self):
		client, _ = _client({}, customer=syn.CUSTOMER)
		client.login()
		self.assertTrue(client._token)

	def test_config_repr_hides_the_password(self):
		cfg = LipseysConfig(email="dealer@example.com", password="pw-test")
		self.assertNotIn("pw-test", repr(cfg))
		self.assertIn("dealer@example.com", repr(cfg))


class TestBodyLevelAuth(unittest.TestCase):
	def test_expired_token_is_http_200_and_triggers_one_fresh_login(self):
		client, session = _client({"Items/ValidateItem": [syn.UNAUTHORIZED, syn.validate(1)]})
		data = client.validate_item(syn.sku(1))
		self.assertEqual(data["qty"], 5)
		self.assertEqual(session.paths(), ["Authentication/Login", "Items/ValidateItem",
			"Authentication/Login", "Items/ValidateItem"])

	def test_still_unauthorized_after_a_fresh_login_raises(self):
		client, session = _client({"Items/ValidateItem": [syn.UNAUTHORIZED, syn.UNAUTHORIZED]})
		with self.assertRaises(LipseysAuthError):
			client.validate_item(syn.sku(1))
		self.assertEqual(session.paths().count("Items/ValidateItem"), 2)

	def test_an_unknown_item_reads_none(self):
		client, _ = _client({"Items/ValidateItem": syn.envelope(None, success=False,
			errors=["Item not found"])})
		self.assertIsNone(client.validate_item("NOPE"))


class TestPlacementNeverRetried(unittest.TestCase):
	def _place(self, answer):
		client, session = _client({"Order/APIOrder": answer})
		return client, session

	def test_a_timeout_after_sending_is_an_unknown_outcome_sent_once(self):
		client, session = self._place(syn.TIMEOUT)
		with self.assertRaises(LipseysPlacementUnknown):
			client.place(lip.EP_API_ORDER, ORDER)
		self.assertEqual(session.paths().count("Order/APIOrder"), 1)

	def test_a_5xx_is_an_unknown_outcome(self):
		client, session = self._place(syn.Resp({"message": "boom"}, status=502))
		with self.assertRaises(LipseysPlacementUnknown):
			client.place(lip.EP_API_ORDER, ORDER)
		self.assertEqual(session.paths().count("Order/APIOrder"), 1)

	def test_an_unreadable_answer_is_an_unknown_outcome(self):
		client, _ = self._place(syn.Resp(b"<html>gateway</html>"))
		with self.assertRaises(LipseysPlacementUnknown):
			client.place(lip.EP_API_ORDER, ORDER)

	def test_a_connect_timeout_never_left_so_it_is_a_plain_error(self):
		client, _ = self._place(syn.CONNECT_TIMEOUT)
		with self.assertRaises(LipseysError) as ctx:
			client.place(lip.EP_API_ORDER, ORDER)
		self.assertNotIsInstance(ctx.exception, LipseysPlacementUnknown)
		self.assertFalse(ctx.exception.maybe_sent)

	def test_a_4xx_was_refused_so_it_is_definite(self):
		client, _ = self._place(syn.Resp({"message": "blocked"}, status=403))
		with self.assertRaises(LipseysError) as ctx:
			client.place(lip.EP_API_ORDER, ORDER)
		self.assertNotIsInstance(ctx.exception, LipseysPlacementUnknown)
		self.assertTrue(ctx.exception.terminal)

	def test_unauthorized_did_nothing_so_the_order_is_sent_after_a_fresh_login(self):
		client, session = self._place([syn.UNAUTHORIZED, syn.api_order(syn.order_line(syn.sku(1)))])
		env = client.place(lip.EP_API_ORDER, ORDER)
		self.assertTrue(env["success"])
		self.assertEqual(session.paths(), ["Authentication/Login", "Order/APIOrder",
			"Authentication/Login", "Order/APIOrder"])

	def test_a_timeout_on_the_resend_after_relogin_is_still_unknown_and_not_retried(self):
		client, session = self._place([syn.UNAUTHORIZED, syn.TIMEOUT])
		with self.assertRaises(LipseysPlacementUnknown):
			client.place(lip.EP_API_ORDER, ORDER)
		self.assertEqual(session.paths().count("Order/APIOrder"), 2)


class TestOneDay(unittest.TestCase):
	def test_date_is_sent_as_the_docs_spell_it(self):
		self.assertEqual(lip.one_day_param(date(2026, 7, 4)), "7/4/2026")
		client, session = _client({"Shipping/OneDay": syn.one_day(syn.shipment("8000001"))})
		rows = client.one_day(date(2026, 10, 9))
		self.assertEqual(session.calls[-1]["body"], "10/9/2026")
		self.assertEqual(rows[0]["invoiceNumber"], "8000001")

	def test_an_unsuccessful_day_raises_so_the_cursor_stays(self):
		client, _ = _client({"Shipping/OneDay": syn.envelope(None, success=False, errors=["x"])})
		with self.assertRaises(LipseysError):
			client.one_day(date(2026, 10, 9))

	def test_an_empty_day_is_an_empty_list(self):
		client, _ = _client({"Shipping/OneDay": syn.one_day()})
		self.assertEqual(client.one_day(date(2026, 10, 9)), [])


class TestProbe(unittest.TestCase):
	def test_probe_signs_in_and_reads_once(self):
		client, session = _client({"Items/ValidateItem": syn.envelope(None, success=False)})
		check = lip.probe(client)
		self.assertTrue(check["ok"])
		self.assertIn(syn.CUSTOMER, check["detail"])
		self.assertEqual(session.paths(), ["Authentication/Login", "Items/ValidateItem"])

	def test_probe_reports_a_refused_login(self):
		client, _ = _client({})
		client.session.routes["Authentication/Login"] = syn.login_refused()
		check = lip.probe(client)
		self.assertFalse(check["ok"])

	def test_probe_without_credentials_is_not_applicable(self):
		check = lip.probe(LipseysClient(LipseysConfig(email="", password=""),
			session=syn.FakeSession({})))
		self.assertIsNone(check["ok"])


if __name__ == "__main__":
	unittest.main()
