"""Distributor Order controller.

The DO is NON-submittable: it never leaves docstatus 0 and its lifecycle is
a `status` walk. `validate` is the enforcement backstop — every write (whitelisted
endpoint, poller, or a direct desk edit) passes through the pure state machine, so
an illegal jump is rejected no matter the caller. A new order is always born Draft.
"""
from __future__ import annotations

import frappe
from frappe.model.document import Document

from ffl_integrations.distributor import order_state


class DistributorOrder(Document):
	def before_insert(self):
		# Carrier service default from the provider (SS: Ground Premium, 2-Day
		# for handguns). Every creation path — counter, web router, a hand-made
		# draft — gets it here, so "blank" on a Draft always means "provider
		# has no choice", never "nobody looked". An empty label distinguishes
		# unset from Sports South's empty-code Ground Economy.
		if not (self.shipping_service or self.shipping_service_label):
			from ffl_integrations.distributor.api import _shipping_options_for
			default = next((o for o in _shipping_options_for(self) if o.get("default")), None)
			if default:
				self.shipping_service = default["code"]
				self.shipping_service_label = default["label"]

	def validate(self):
		self._enforce_status_machine()
		self._roll_up_total_cost()

	def _enforce_status_machine(self):
		before = self.get_doc_before_save()
		if before is None:
			# Birth: only ever as Draft — never insert straight into a live/terminal state.
			if self.status and self.status != order_state.DRAFT:
				frappe.throw(frappe._("A new Distributor Order must start as Draft."))
			self.status = order_state.DRAFT
			return
		if before.status and self.status and before.status != self.status:
			try:
				order_state.assert_transition(before.status, self.status)
			except ValueError as exc:
				frappe.throw(frappe._(str(exc)))

	def _roll_up_total_cost(self):
		self.total_cost = sum(
			(line.unit_cost or 0) * (line.wish_qty or 0) for line in (self.lines or []))
