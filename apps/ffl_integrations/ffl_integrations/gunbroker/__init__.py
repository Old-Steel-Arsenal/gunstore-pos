"""GunBroker sales channel.

Third channel for the same shared inventory, after the WooCommerce storefront
and the counter. Lives in ffl_integrations because that is where external
vendor clients live (FastBound / ShipStation / RSR / Payroc), and because
ffl_woo_sync's notion of a "site" is a hard-coded registry (today only the retail
store) that raises on anything else — by design, so a future channel fails closed
rather than half-working.

Hard rule for this package: it NEVER imports ffl_woo_sync, and ffl_woo_sync
never imports it. Anything the two channels genuinely share lives in ffl_core.
A CI test asserts both directions.
"""
