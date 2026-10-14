"""The ``before_request`` half of the GunBroker webhooks.

Kept apart from :mod:`webhooks` because a ``before_request`` hook is imported on
the first request of every worker, whatever URL it is: this module imports only
``frappe``, so the GunBroker machinery is not pulled in by (or able to break) a
request that has nothing to do with it.
"""
from __future__ import annotations

import frappe

MODULE_PATH = "ffl_integrations.gunbroker.webhooks"

# The URLs we hand GunBroker. Its subscription form refuses an endpoint path of 50
# characters or more and `/api/method/<MODULE_PATH>.<slug>` runs to 58-74, so
# hooks.py maps each `gb.<slug>` onto the real method
# (``override_whitelisted_methods``).
ALIAS_PREFIX = "/api/method/gb."

# Where the stripped Authorization header is parked in ``frappe.local.flags``.
STASH = "gunbroker_webhook_authorization"


def shield_basic_auth() -> None:
	"""``before_request`` hook: keep Frappe's API-key check away from our Basic header.

	Frappe's ``validate_auth`` runs right after ``before_request`` hooks and treats
	any two-part ``Authorization`` header as credentials for one of its own users,
	raising ``AuthenticationError`` (its own 401) when they are not. For the webhook
	URLs the credentials are GunBroker's, so the header is removed from the request
	and handed to the endpoint through ``frappe.local.flags`` instead.

	Does nothing for any other URL; never raises (it runs on every request).
	"""
	try:
		request = frappe.local.request
		# Two cheap string tests: this runs on every request of the site.
		path = request.path
		if not path.startswith(ALIAS_PREFIX) and f"/{MODULE_PATH}." not in path:
			return
		frappe.local.flags[STASH] = request.environ.pop("HTTP_AUTHORIZATION", None) or ""
	except Exception:  # noqa: BLE001 — a hook on every request must not break any of them
		pass
