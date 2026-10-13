"""One-shot — move the GunBroker DevKeys off the Settings form into site config.

The DevKey identifies the software, not the store: GunBroker issues it to
whoever runs this POS and matches the User-Agent against the name it was
registered under. A settings page that shows it hands a platform credential to
every System Manager of every store, so the two Password fields are gone and
the client reads ``gunbroker_dev_key`` / ``gunbroker_sandbox_dev_key`` from
``frappe.conf`` instead (``bench --site <site> set-config …``).

A site that had a key on the form keeps working across the upgrade: the value
is copied into site_config.json and the encrypted row deleted. A site config
that already carries a key is never overwritten — the operator's explicit
setting wins over whatever was left on the form — but the form's copy is
deleted either way, because nothing reads it any more and a secret nobody
reads is a secret nobody rotates.

Runs after the doctype sync, so the fields no longer exist on the meta when
this executes; ``__Auth`` is keyed by fieldname alone and is read directly.
"""
from __future__ import annotations

import frappe
from frappe.installer import update_site_config
from frappe.utils.password import get_decrypted_password, remove_encrypted_password

SETTINGS = "GunBroker Settings"
FIELDS = {"dev_key": "gunbroker_dev_key", "sandbox_dev_key": "gunbroker_sandbox_dev_key"}


def execute(site_config_path: str | None = None) -> dict:
	"""Returns ``{conf_key: "moved" | "kept" | "absent"}`` — never a key value."""
	outcome = {}
	for field, conf_key in FIELDS.items():
		stored = (get_decrypted_password(SETTINGS, SETTINGS, field, raise_exception=False) or "").strip()
		if not stored:
			outcome[conf_key] = "absent"
			continue
		if frappe.conf.get(conf_key):
			outcome[conf_key] = "kept"
		else:
			update_site_config(conf_key, stored, site_config_path=site_config_path)
			outcome[conf_key] = "moved"
		remove_encrypted_password(SETTINGS, SETTINGS, field)
		# The masked placeholder a Password field leaves behind in tabSingles.
		frappe.db.delete("Singles", {"doctype": SETTINGS, "field": field})
	return outcome
