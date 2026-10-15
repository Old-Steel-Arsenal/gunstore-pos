"""The shipped local-catalog drivers. Importing this package registers each one
in ``sync.DRIVERS`` (``sync.load_drivers`` does it for every entry point)."""
from ffl_integrations.catalog_local.drivers import lipseys  # noqa: F401 — registers "lipseys"
from ffl_integrations.catalog_local.drivers import rsr  # noqa: F401 — registers "rsr"
from ffl_integrations.catalog_local.drivers import ss  # noqa: F401 — registers "ss"
