"""Credentials that stay out of Error Log tracebacks.

Frappe records an Error Log through ``frappe.get_traceback(with_context=True)``
(frappe/utils/error.py), and every HTTP 500 out of a whitelisted method goes
there (frappe/app.py handle_exception → log_error_snapshot). That formatter
(traceback_with_variables) prints EVERY frame's locals — ours and requests'
alike, one object level deep — and redacts by variable NAME only:
password/passwd/secret/token/key/pwd, plus the same words as top-level keys of
a plain dict (frappe.utils._get_traceback_sanitizer). So a Merchant API Key in
``headers``, a ``Password`` in a JSON body, a consumer secret in
``Session.auth`` all landed in the database in clear text on the next
connection reset. An earlier fix closed that for Payroc; this module is the one
carrier for every integration.

What the formatter honours — measured on the bench, not read from docs:

  value in a dict / CaseInsensitiveDict  → the value's ``__repr__``   (Secret works)
  object with an explicit ``__repr__``   → that repr                   (RedactedBasicAuth, RedactedRepr)
  bare ``str`` local, tuple member       → raw text, repr IGNORED      (rely on the name blocklist)
  dataclass-generated repr               → attributes walked raw       (hence RedactedRepr + repr=False)
  ``dict`` SUBCLASS                      → items walked, repr IGNORED  (never carry a secret that way)

Boundaries left open on purpose: the serialised wire body (``prepare_body``'s
``body``, http.client's ``_buffer``) is plain text and only in the traceback
when the failure is inside those frames — every ordinary network fault is
raised by HTTPAdapter.send above them. Composed strings are plain
``str``: wrap the RESULT (``Secret(f"Bearer {jwt}")``), not the
ingredient. Likewise a credential that requests REJECTS (leading/trailing
whitespace, a newline) is raised on from ``prepare_headers`` /
``check_header_validity`` with the raw value in a bare str local — a
misconfiguration that fails once, at setup.
"""
from __future__ import annotations

from requests.auth import HTTPBasicAuth

REDACTED = "<redacted>"

# Substring match, case-insensitive, on a field/header/dict-key NAME.
_CREDENTIAL_MARKERS = ("authorization", "key", "token", "secret", "password", "pwd")


def is_credential_name(name: str) -> bool:
	"""True for names that carry a credential (``api_key``, ``X-AccessToken``,
	``consumer_secret``, ``Password`` …) — the field-name half of the contract."""
	lowered = (name or "").lower()
	return any(marker in lowered for marker in _CREDENTIAL_MARKERS)


class Secret(str):
	"""A ``str`` for every consumer, opaque to every printer.

	Encodes, compares, serialises and urlencodes exactly like the plain string
	it wraps (json.dumps and urlencode read the text, not the repr). Inside any
	mapping the traceback formatter prints ``<redacted>`` for it. Wrap the value
	that goes on the wire: ``{"Authorization": Secret(f"Bearer {jwt}")}``.
	"""

	__slots__ = ()

	def __repr__(self) -> str:
		return REDACTED


class RedactedBasicAuth(HTTPBasicAuth):
	"""``auth=`` for requests that prints as nothing.

	A bare ``(user, password)`` tuple sits in ``Session.request``'s ``auth``
	local — and on ``Session.auth`` for the life of the session, which the
	formatter expands on every requests frame — with both halves raw.

	Not tuple-shaped on purpose: ``session.auth == (k, s)`` is False, it does
	not unpack and is not hashable. Read ``.username`` / ``.password``.
	"""

	def __repr__(self) -> str:
		return f"{type(self).__name__}({REDACTED})"


class RedactedRepr:
	"""Mixin for ``@dataclass(repr=False)`` config objects.

	The generated dataclass repr is not "custom" to the formatter, which walks
	the attributes itself and prints credential fields raw whenever a config is
	a direct local (ffl_integrations.integration_gate reads one). An explicit
	``__repr__`` is honoured; this one masks by field name.

	Secret VALUES only: the identifier half of a pair (``username``,
	``customer_number``, ``dealer_account``) prints, on purpose — it is what
	tells two accounts apart when reading the log, and it opens nothing alone.
	"""

	def __repr__(self) -> str:
		parts = ", ".join(
			f"{name}={REDACTED}" if is_credential_name(name) else f"{name}={value!r}"
			for name, value in vars(self).items())
		return f"{type(self).__name__}({parts})"
