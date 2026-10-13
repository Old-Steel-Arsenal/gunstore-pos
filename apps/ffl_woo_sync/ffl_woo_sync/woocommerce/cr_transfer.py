"""C&R (Type 03) transfer helpers — re-export of the ffl_core implementation.

The module moved to ``ffl_core.compliance.cr_transfer`` when GunBroker became a
second channel that ships to collectors: whether a firearm may go straight to a
private address is a compliance question, and a compliance rule that exists once
per storefront is a compliance rule that will eventually disagree with itself.

Kept here as an explicit re-export so every WooCommerce import site
(``woocommerce/fulfillment.py``, ``woocommerce/webhook.py``, the front-end
invariant test that reads ``CR_REQUIRED_FIELDS`` / ``CR_DETAIL_KEYS``) is
unchanged, and so the Woo behaviour is unchanged: Woo does not pass the new
``license_field`` keyword, whose default is Woo's own ``ffl_license``.

Note for anyone writing a NEW test against these functions: patch the
implementation module (``ffl_core.compliance.cr_transfer``), not this one. A
name imported here is a second reference to the same object, so patching
``ffl_woo_sync…cr_transfer.nowdate`` would rebind only this alias and the real
call would still see the original.
"""
from __future__ import annotations

from ffl_core.compliance.cr_transfer import (  # noqa: F401  (re-export)
	CR_DETAIL_KEYS,
	CR_OPTIONAL_FIELDS,
	CR_REQUIRED_FIELDS,
	CR_VERIFICATION_FIELDNAMES,
	_clear_cr_verification,
	_persist_cr_details,
	_validate_cr_details,
	ensure_cr_ffl_record,
	parse_cr_details,
	reject_cr_details_on_non_cr_order,
)

__all__ = [
	"CR_DETAIL_KEYS",
	"CR_OPTIONAL_FIELDS",
	"CR_REQUIRED_FIELDS",
	"CR_VERIFICATION_FIELDNAMES",
	"_clear_cr_verification",
	"_persist_cr_details",
	"_validate_cr_details",
	"ensure_cr_ffl_record",
	"parse_cr_details",
	"reject_cr_details_on_non_cr_order",
]
