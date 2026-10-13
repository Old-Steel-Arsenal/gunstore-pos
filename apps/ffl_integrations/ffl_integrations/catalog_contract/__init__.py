"""Catalog API conformance runner — see docs/catalog-api/README.md.

``run(...)`` is the operator entry point (bench execute); ``check(transport,
distributor, sku)`` is the pure core, driven by any transport that answers
``get(path, params)`` / ``post(path, payload)`` with ``(status, json)``.
The in-memory reference (tests/catalog_stub.py) and the live HTTP service
run through the same checks, which is what keeps contract, reference and
service from drifting apart."""
from .runner import check, run  # noqa: F401
