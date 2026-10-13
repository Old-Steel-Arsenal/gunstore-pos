"""Sports South ordering client — pure: envelope unwrapping, the auth-failure
shapes, dataset parsing, and the create-order calls against a fake session."""
from __future__ import annotations

import unittest

from ffl_integrations.sports_south import api_client as ss
from ffl_integrations.sports_south.api_client import (
	SSAuthError, SSConfig, SSError, SSOrdersClient, normalize_credits,
	normalize_detail_rows, parse_records, sniff_error, unwrap,
)

XMLNS = 'xmlns="http://webservices.theshootingwarehouse.com/smart/Orders.asmx"'


def env(inner: str, kind="string") -> str:
	esc = inner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") if kind == "string" else inner
	return f'<?xml version="1.0" encoding="utf-8"?>\r\n<{kind} {XMLNS}>{esc}</{kind}>'


class _Resp:
	def __init__(self, text, status=200):
		self.text, self.status_code = text, status


class FakeSession:
	def __init__(self, routes: dict):
		self.routes, self.calls = routes, []

	def post(self, url, data=None, timeout=None, headers=None):
		method = url.rsplit("/", 1)[-1]
		self.calls.append((method, dict(data or {})))
		body = self.routes[method]
		if callable(body):
			body = body(dict(data or {}))
		return _Resp(body) if isinstance(body, str) else body


def _client(routes, sandbox=False):
	cfg = SSConfig(customer_number="100001", username="100001", password="pw",
		source="99994" if sandbox else "100001", ordering_enabled=True)
	return SSOrdersClient(cfg, session=FakeSession(routes))


class TestEnvelope(unittest.TestCase):
	def test_unwrap_string_unescapes(self):
		kind, inner = unwrap(env("<NewDataSet />"))
		self.assertEqual((kind, inner), ("string", "<NewDataSet />"))

	def test_unwrap_self_closing_is_empty_string(self):
		self.assertEqual(unwrap(f'<?xml version="1.0"?>\r\n<string {XMLNS} />'), ("string", ""))

	def test_unwrap_int_and_boolean(self):
		self.assertEqual(unwrap(env("1119886", "int")), ("int", "1119886"))
		self.assertEqual(unwrap(env("true", "boolean")), ("boolean", "true"))

	def test_sniff_dataset_error(self):
		self.assertEqual(sniff_error("<NewDataSet><Table1><ERROR>Authentication Failed For User</ERROR></Table1></NewDataSet>"),
			"Authentication Failed For User")

	def test_sniff_bare_error_and_literals(self):
		self.assertEqual(sniff_error("<ERROR>USER NOT AUTHENTICATED!</ERROR>"), "USER NOT AUTHENTICATED!")
		self.assertEqual(sniff_error("AUTHFAIL"), "AUTHFAIL")
		self.assertEqual(sniff_error("System.Data.DataSet"), "System.Data.DataSet")
		self.assertIsNone(sniff_error("<NewDataSet />"))
		self.assertIsNone(sniff_error(""))

	def test_parse_records_empty_and_rows(self):
		self.assertEqual(parse_records("<NewDataSet />"), [])
		rows = parse_records("<NewDataSet><Table><ORITEM>25845</ORITEM><ORQTY>2</ORQTY>"
			"<ORQTYF>1</ORQTYF><ORPRC>1.00</ORPRC><ORPRCF>19.50</ORPRCF></Table></NewDataSet>")
		self.assertEqual(rows[0]["ORITEM"], "25845")
		d = normalize_detail_rows(rows)[0]
		self.assertEqual((d["sku"], d["ordered_qty"], d["filled_qty"], d["filled_price"]),
			("25845", 2, 1, 19.5))

	def test_normalize_credits_groups_header_and_detail(self):
		recs = parse_records(
			"<NewDataSet><Header><CRDNO>1234</CRDNO><CRDTOT>-153.50</CRDTOT><INVNO>7</INVNO>"
			"<PONBR>DO-2609-0001</PONBR><REASNM>CANCELLED</REASNM></Header>"
			"<Detail><CRDNO>1234</CRDNO><LINENUM>3</LINENUM><ITEMNO>62494</ITEMNO>"
			"<SQTY>-50</SQTY><SPRC>3.01</SPRC></Detail></NewDataSet>")
		c = normalize_credits(recs)
		self.assertEqual(len(c), 1)
		self.assertEqual(c[0]["po"], "DO-2609-0001")
		self.assertEqual(c[0]["total"], -153.5)
		self.assertEqual(c[0]["lines"], [{"sku": "62494", "qty": -50, "price": 3.01, "line_num": 3}])


class TestCreateOrderCalls(unittest.TestCase):
	def test_add_header_returns_web_order_and_sends_booleans(self):
		c = _client({"AddHeader": env("1119886", "int")})
		n = c.add_header(po="DO-1", ship_to={"name": "Joe", "line1": "1 Main", "city": "Springfield",
			"state": "TX", "zip": "70001-1234", "phone": "(972) 555-1212"},
			ship_via="G", adult_signature=False, signature=True, insurance=True)
		self.assertEqual(n, 1119886)
		method, data = c.session.calls[0]
		self.assertEqual(method, "AddHeader")
		self.assertEqual(data["Signature"], "true")
		self.assertEqual(data["AdultSignature"], "false")
		self.assertEqual(data["ShipToZip"], "70001")
		self.assertEqual(data["ShipToPhone"], "9725551212")
		self.assertEqual(data["Source"], "100001")
		self.assertEqual(data["CustomerOrderNumber"], "0")

	def test_sandbox_sends_source_99994(self):
		c = _client({"GetUnsubmittedOrders": env("<NewDataSet />")}, sandbox=True)
		c.get_unsubmitted_orders()
		self.assertEqual(c.session.calls[0][1]["Source"], "99994")

	def test_add_header_zero_is_terminal(self):
		c = _client({"AddHeader": env("0", "int")})
		with self.assertRaises(SSError) as ctx:
			c.add_header(po="DO-1", ship_to={}, ship_via="G", adult_signature=False,
				signature=False, insurance=False)
		self.assertTrue(ctx.exception.terminal)

	def test_auth_failure_shapes_raise_auth_error(self):
		for body in (env("<NewDataSet><Table1><ERROR>Authentication Failed For User</ERROR></Table1></NewDataSet>"),
				env("<ERROR>USER NOT AUTHENTICATED!</ERROR>"), env("AUTHFAIL")):
			c = _client({"GetDetail": body})
			with self.assertRaises(SSAuthError):
				c.get_detail(1)

	def test_add_detail_and_submit_booleans(self):
		c = _client({"AddDetail": env("true", "boolean"), "Submit": env("false", "boolean")})
		self.assertTrue(c.add_detail(1119886, sku="25845", qty=2, order_price=19.5,
			customer_item="ITEM-1", customer_desc="A thing"))
		self.assertFalse(c.submit(1119886))
		_, data = c.session.calls[0]
		self.assertEqual(data["SSItemNumber"], 25845)
		self.assertEqual(data["OrderPrice"], "19.50")

	def test_credits_date_format_pinned(self):
		c = _client({"GetCreditsByDate": env("<NewDataSet />")})
		with self.assertRaises(SSError):
			c.get_credits_by_date("2026-09-07")
		self.assertEqual(c.get_credits_by_date("20260907"), [])

	def test_silent_methods_prove_auth_first(self):
		c = _client({"GetUnsubmittedOrders": env("<NewDataSet />"),
			"ShipCheck": env("<NewDataSet />")})
		self.assertEqual(c.ship_check("70001", "120"), [])
		self.assertEqual([m for m, _ in c.session.calls], ["GetUnsubmittedOrders", "ShipCheck"])

	def test_http_5xx_is_retryable_4xx_terminal(self):
		c = _client({"Submit": _Resp("boom", 503)})
		with self.assertRaises(SSError) as ctx:
			c.submit(1)
		self.assertFalse(ctx.exception.terminal)
		c = _client({"Submit": _Resp("nope", 400)})
		with self.assertRaises(SSError) as ctx:
			c.submit(1)
		self.assertTrue(ctx.exception.terminal)

	def test_probe_reports_without_raising(self):
		c = _client({"GetUnsubmittedOrders": env("AUTHFAIL"), "GetByPONumber": env("<NewDataSet />")})
		checks = ss.probe_connection(c)
		self.assertEqual([x["ok"] for x in checks], [False, True])
