"""Declare the ``catalog`` queue in common_site_config ``workers`` — no Frappe
context needed, so the queue-catalog container runs it before ``bench worker``:

    env/bin/python -m ffl_integrations.catalog_local.declare_queue sites/common_site_config.json

Frappe only accepts a queue listed there (background_jobs.get_queues_timeout),
and ``bench worker --queue catalog`` refuses to start otherwise; declaring it
here means the worker works before the first ``bench migrate``. Merged:
other queues, other keys and an operator's own catalog timeout stay.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

QUEUE = "catalog"
TIMEOUT_SECONDS = 1500


def merged(config: dict) -> tuple[dict, bool]:
	"""PURE: ``(config with the catalog queue declared, changed?)``."""
	workers = config.get("workers")
	if isinstance(workers, dict) and QUEUE in workers:
		return config, False
	out = dict(config)
	out["workers"] = dict(workers if isinstance(workers, dict) else {})
	out["workers"][QUEUE] = {"timeout": TIMEOUT_SECONDS}
	return out, True


def declare(path: str) -> bool:
	"""Write the merged config atomically (temp file + rename). True if changed."""
	config, mode = {}, 0o644
	if os.path.exists(path):
		with open(path) as fh:
			config = json.load(fh)
		mode = os.stat(path).st_mode & 0o777
	config, changed = merged(config)
	if changed:
		fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), suffix=".tmp")
		with os.fdopen(fd, "w") as fh:
			json.dump(config, fh, indent=1, sort_keys=True)
		os.chmod(tmp, mode)
		os.replace(tmp, path)
	return changed


if __name__ == "__main__":
	declare(sys.argv[1] if len(sys.argv) > 1 else "sites/common_site_config.json")
