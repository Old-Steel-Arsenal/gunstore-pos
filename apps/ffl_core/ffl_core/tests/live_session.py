"""A real ``requests.Session`` that fails where a reset/timeout would.

For tests that inspect ``frappe.get_traceback(with_context=True)``: the frames
that matter are requests' own (``Session.request`` holds our headers/json/auth
in named parameters for the whole call), and a MagicMock session never creates
them — that blind spot let a no-op redaction look green once.

Canned replies are built into Response objects UP FRONT: the adapter ends up
inside the traceback under assertion, so it must not carry a raw credential
string in an attribute (``Response.__repr__`` is just ``<Response [200]>``).
"""
from __future__ import annotations

import json as _json

import requests


class CannedOrBrokenAdapter(requests.adapters.HTTPAdapter):
	def __init__(self, canned=None, error: Exception | None = None):
		super().__init__()
		self.canned = {fragment: built_response(status, payload)
			for fragment, (status, payload) in (canned or {}).items()}
		# ConnectionError is what every ordinary network fault becomes; a
		# non-RequestException models a failure the client's except clauses do
		# not wrap, which is when requests' own frames reach the Error Log.
		self.error = error or requests.exceptions.ConnectionError("connection reset by peer")

	def send(self, request, **kwargs):
		for fragment, response in self.canned.items():
			if fragment in request.url:
				response.url = request.url
				response.request = request
				return response
		raise self.error


def built_response(status: int, payload) -> requests.Response:
	response = requests.Response()
	response.status_code = status
	response._content = (payload if isinstance(payload, bytes)
		else _json.dumps(payload).encode())
	return response


def live_session(canned=None, error: Exception | None = None,
		session: requests.Session | None = None) -> requests.Session:
	"""Mount the adapter on a fresh Session — or on ``session`` (a client's own
	``_build_session()`` output, so its auth/header setup is what gets tested)."""
	session = session or requests.Session()
	adapter = CannedOrBrokenAdapter(canned, error)
	session.mount("https://", adapter)
	session.mount("http://", adapter)
	return session
