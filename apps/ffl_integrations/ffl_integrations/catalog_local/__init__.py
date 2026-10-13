"""Local distributor catalog — the in-POS alternative to the Data Service's
catalog (``Data Service Settings.catalog_source`` = Local).

The read side (``backend.LocalCatalog``) answers the exact method set and dict
shapes of ``tests/catalog_stub.CatalogStub``, so ``remote_catalog._backend()``
can hand it to every consumer unchanged. The write side (``store``) and the sync
framework (``sync``) are ports of the Data Service's store and
its driver base and lane runner to MariaDB and Frappe jobs. Tables are
plain SQL (``schema``), not DocTypes: a feed is tens of thousands of rows written
in bulk.

Distributor drivers live in ``drivers/`` and register in ``sync.DRIVERS`` when
imported (``sync.load_drivers``): RSR and Sports South.
"""
from __future__ import annotations

import frappe

SETTINGS = "Data Service Settings"
SOURCE_LOCAL = "Local"
SOURCE_SERVICE = "Data Service"
QUEUE = "catalog"


def is_local(strict: bool = False) -> bool | None:
	"""Is the catalog served from this site's own tables? Read per request
	(``get_single_value`` caches it until the next commit).

	``strict`` (the capability model): None when the setting cannot be read, so a
	database hiccup is "unknown", never a confirmed answer. A site migrated before
	the field existed is not Local either way."""
	if strict:
		try:
			if not frappe.get_meta(SETTINGS).has_field("catalog_source"):
				return False
			return frappe.db.get_single_value(SETTINGS, "catalog_source") == SOURCE_LOCAL
		except Exception:  # noqa: BLE001 — unread, not "not Local"
			return None
	try:
		return frappe.db.get_single_value(SETTINGS, "catalog_source") == SOURCE_LOCAL
	except Exception:  # noqa: BLE001 — a site migrated before the field existed
		return False
