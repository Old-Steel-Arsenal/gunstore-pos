"""ffl_core.credentials — what the Error Log formatter actually prints.

Every assertion here is against ``frappe.get_traceback(with_context=True)``,
the exact function Error Log is written from; the carriers are only as good
as what that formatter does with them, and its behaviour was measured, not
assumed (see the module docstring for the matrix).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import patch
from urllib.parse import urlencode

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from ffl_core.credentials import (
	REDACTED,
	RedactedBasicAuth,
	RedactedRepr,
	Secret,
	is_credential_name,
)
from ffl_core.tests.live_session import live_session


def _mark(tag: str) -> str:
	"""A credential-shaped test value built at runtime, so no secret-scanner
	sees a literal ``password="…"`` in source (GitGuardian flagged the first
	cut of this file for exactly that)."""
	return f"MARK-{tag}-must-not-leak"


@dataclass(repr=False)
class _Config(RedactedRepr):
	api_key: str
	base_url: str


def _traceback_of(fn) -> str:
	try:
		fn()
	except Exception:
		# Newer Frappe renders the locals ("with_context") only in developer mode, and a site that is not in it
		# prints none. These tests pin what that rendering does with our carriers, so ask for it.
		with patch.dict(frappe.local.conf, {"developer_mode": 1}):
			return frappe.get_traceback(with_context=True)
	raise AssertionError("expected the call to raise")


class SecretSemantics(FrappeTestCase):
	def test_behaves_as_the_plain_string_everywhere_it_is_consumed(self):
		s = Secret("p w")
		self.assertEqual(s, "p w")
		self.assertEqual(json.dumps({"Password": s}), '{"Password": "p w"}')
		self.assertEqual(urlencode({"Password": s}), "Password=p+w")
		self.assertEqual(s.encode("latin-1"), b"p w")
		self.assertFalse(Secret(""))            # config "is it set" checks still work
		self.assertIs(type(str(s)), str)

	def test_is_credential_name(self):
		for name in ("api_key", "X-AccessToken", "consumer_secret", "Password", "Authorization", "pwd"):
			self.assertTrue(is_credential_name(name), name)
		for name in ("base_url", "Accept", "Username", "timeout"):
			self.assertFalse(is_credential_name(name), name)


class WhatTheFormatterPrints(FrappeTestCase):
	"""One test per row of the measured matrix."""

	def test_secret_inside_a_dict_prints_redacted(self):
		def boom():
			# Not "Authorization": newer Frappe blanks a dict value under a key like that itself, before the
			# value's own repr is asked, so the Secret would never be what is under test.
			headers = {"X-Session": Secret(_mark("dict"))}
			raise RuntimeError
		tb = _traceback_of(boom)
		self.assertNotIn(_mark("dict"), tb)
		self.assertIn(REDACTED, tb)

	def test_secret_on_session_headers_prints_redacted(self):
		def boom():
			s = requests.Session()
			s.headers["API-Key"] = Secret(_mark("session-header"))
			raise RuntimeError
		self.assertNotIn(_mark("session-header"), _traceback_of(boom))

	def test_bare_str_local_is_NOT_protected_by_secret(self):
		"""Documented limit: a top-level str local prints raw whatever its
		repr says — only its NAME can save it. Pinned so nobody "simplifies"
		a `token_*` rename away on the strength of the value being a Secret."""
		def boom():
			bare = Secret(_mark("bare"))
			raise RuntimeError
		self.assertIn(_mark("bare"), _traceback_of(boom))

	def test_basic_auth_object_prints_redacted_as_local_and_on_session(self):
		def boom():
			auth = RedactedBasicAuth(_mark("auth-user"), _mark("auth-pass"))
			s = requests.Session()
			s.auth = auth
			raise RuntimeError
		tb = _traceback_of(boom)
		self.assertNotIn(_mark("auth-user"), tb)
		self.assertNotIn(_mark("auth-pass"), tb)

	def test_bare_tuple_auth_is_NOT_protected(self):
		"""The shape RedactedBasicAuth replaces, pinned as a leak."""
		def boom():
			s = requests.Session()
			s.auth = (_mark("tuple-user"), "")
			raise RuntimeError
		self.assertIn(_mark("tuple-user"), _traceback_of(boom))

	def test_redacted_repr_config_as_direct_local_and_as_attribute(self):
		class Holder:
			def __init__(self):
				self.config = _Config(api_key=_mark("cfg-attr"), base_url="u")
		def boom():
			cfg = _Config(api_key=_mark("cfg-local"), base_url="u")
			holder = Holder()
			raise RuntimeError
		tb = _traceback_of(boom)
		self.assertNotIn(_mark("cfg-local"), tb)
		self.assertNotIn(_mark("cfg-attr"), tb)
		self.assertIn("base_url='u'", tb)      # non-credential fields still diagnose

	def test_plain_dataclass_config_is_NOT_protected(self):
		"""The shape RedactedRepr replaces, pinned as a leak."""
		@dataclass
		class Plain:
			api_key: str
		def boom():
			cfg = Plain(api_key=_mark("plain-dc"))
			raise RuntimeError
		self.assertIn(_mark("plain-dc"), _traceback_of(boom))


class ThroughARealRequestsStack(FrappeTestCase):
	"""The frames the carriers exist for."""

	def test_secret_header_survives_session_request_frame(self):
		session = live_session()
		def boom():
			session.get("https://example.invalid/x",
				headers={"Authorization": Secret("Basic MARK-stack-header")})
		tb = _traceback_of(boom)
		self.assertIn("requests/sessions.py", tb)
		self.assertNotIn(_mark("stack-header"), tb)

	def test_secret_json_and_form_values_survive_session_request_frame(self):
		session = live_session()
		tb = _traceback_of(lambda: session.post("https://example.invalid/x",
			json={"Password": Secret(_mark("stack-json"))}))
		self.assertIn("requests/sessions.py", tb)
		self.assertNotIn(_mark("stack-json"), tb)
		tb = _traceback_of(lambda: session.post("https://example.invalid/x",
			data={"Password": Secret(_mark("stack-form"))}))
		self.assertNotIn(_mark("stack-form"), tb)

	def test_redacted_basic_auth_survives_session_request_frame(self):
		session = live_session()
		tb = _traceback_of(lambda: session.get("https://example.invalid/x",
			auth=RedactedBasicAuth(_mark("stack-auth"), "")))
		self.assertIn("requests/sessions.py", tb)
		self.assertNotIn(_mark("stack-auth"), tb)

	def test_carriers_do_not_change_what_goes_on_the_wire(self):
		session = live_session({"/echo": (200, {"ok": True})})
		sent = []
		original = session.send
		session.send = lambda request, **kw: (sent.append(request), original(request, **kw))[1]
		session.post("https://example.invalid/echo",
			headers={"Authorization": Secret("Bearer real-jwt")},
			auth=RedactedBasicAuth("user", "pass"),
			data={"Password": Secret("real pw")})
		req = sent[-1]
		# auth= wins over a supplied Authorization header in requests, as before
		self.assertEqual(req.headers["Authorization"], "Basic dXNlcjpwYXNz")
		self.assertEqual(req.body, "Password=real+pw")
		session.post("https://example.invalid/echo",
			headers={"Authorization": Secret("Bearer real-jwt")},
			json={"Password": Secret("real pw")})
		req = sent[-1]
		self.assertEqual(req.headers["Authorization"], "Bearer real-jwt")
		self.assertEqual(json.loads(req.body), {"Password": "real pw"})
