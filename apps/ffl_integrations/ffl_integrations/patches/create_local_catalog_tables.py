"""Create the local catalog's dc_* tables and declare the `catalog` queue.
after_migrate re-runs the same idempotent ensure() on every migrate."""
from __future__ import annotations


def execute():
	from ffl_integrations.catalog_local.schema import ensure

	ensure()
