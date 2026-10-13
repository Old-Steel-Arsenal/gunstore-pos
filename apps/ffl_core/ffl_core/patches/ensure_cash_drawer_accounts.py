"""Cash drawer: create Cash Over/Short and Operating Bank for every company that
lacks them, and fill each company's default bank account when it is empty. Also adds the two POS Closing
Entry fields. Same code as the after_migrate hook (ffl_core.setup.cash_drawer.ensure); the patch makes the
one-time change visible in the patch log. Idempotent."""
from ffl_core.setup.cash_drawer import ensure


def execute():
	ensure()
