"""Dropship Brand Policy — per-brand fulfilment preference, optionally
scoped to ONE distributor.

Read by `distributor.policy.brand_policy_entry`: a `To Store` brand is never
drop-shipped direct to the buyer, it is procured into the shop first (C2). A
row with a distributor applies only to that house's catalog and beats a
blank (global) row for the same brand; `reason` is what the clerk sees.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class DropshipBrandPolicy(Document):
	def autoname(self):
		# Global rows keep the historical name (= brand); scoped rows get
		# "<brand> @ <distributor>" so one brand may carry one row per house.
		self.name = f"{self.brand} @ {self.distributor}" if self.distributor else self.brand

	def validate(self):
		dup = frappe.db.get_value(self.doctype, {
			"brand": self.brand,
			"distributor": self.distributor or ["is", "not set"],
			"name": ["!=", self.name]}, "name")
		if dup:
			frappe.throw(_("{0} already has a policy for {1} ({2}).").format(
				self.brand, self.distributor or _("all distributors"), dup))
