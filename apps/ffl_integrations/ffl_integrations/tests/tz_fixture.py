"""The one fixture that can see a host-zone bug: SITE on A, HOST on B.

Every defect in this family shipped for the same reason — code
asked the *host* (`datetime.astimezone()` with no argument, `date.today()`,
`datetime.now()`) for what is a *site* fact, and on the machine it was written
on the two zones happened to agree. A test that leaves the host zone alone goes
green on exactly the machines that hide the bug, which is worse than no test:
it is a standing claim that the case is covered.

So this forces them apart. Anything that converts an instant into a wall clock
the site reasons about must stay correct under it.

Lives in its own module rather than inside one test file because three test
modules now need it (`test_catalog_service`, `test_timezone_family`,
`test_distributor_watchdog`) — the same "one implementation, several callers"
rule the fix itself follows.
"""
from __future__ import annotations

import contextlib
import os
import time
from unittest import mock

import frappe


@contextlib.contextmanager
def site_tz(site: str, *, os_tz: str):
	"""Run the block with the SITE on `site` and the HOST process on `os_tz`.

	`os_tz` is keyword-only and has no default on purpose: naming the host zone
	is the whole point, and a default would let a caller quietly re-create the
	blind spot this fixture exists to remove.
	"""
	previous_tz = os.environ.get("TZ")
	os.environ["TZ"] = os_tz
	time.tzset()
	# `create=True`: the site-free harness's `frappe.utils` may not define it, and
	# patching it into existence is exactly what a site would have done.
	getter = mock.patch.object(frappe.utils, "get_system_timezone",
		create=True, return_value=site)
	getter.start()
	try:
		yield
	finally:
		getter.stop()
		if previous_tz is None:
			os.environ.pop("TZ", None)
		else:
			os.environ["TZ"] = previous_tz
		time.tzset()
