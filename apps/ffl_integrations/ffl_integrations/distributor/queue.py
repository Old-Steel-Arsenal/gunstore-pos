"""Single choke point for distributor background work.

Everything the distributor layer defers is enqueued here, so a hosting
pressure event is one constant away from moving all distributor jobs onto a
dedicated worker without touching call sites.
"""
from __future__ import annotations

import frappe

# Flip to a dedicated worker/queue name here if RSR polling ever starves the
# shared "long" queue. Every distributor enqueue reads this one constant.
DISTRIBUTOR_QUEUE = "long"


def enqueue_distributor(method, *, job_name=None, timeout=600, **kwargs):
	return frappe.enqueue(
		method, queue=DISTRIBUTOR_QUEUE, job_name=job_name, timeout=timeout, **kwargs)
