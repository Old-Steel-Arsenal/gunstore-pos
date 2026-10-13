"""Distributor controller — one row per wholesale supplier integration (RSR = #1).
`code` autonames; `provider` binds it to a registered DistributorProvider. The
website drop-ship layer validates and reacts to its own fields (markup, stock,
lead time) through doc_events (``house_settings.validate_distributor`` /
``on_distributor_update``)."""
from __future__ import annotations

import frappe
from frappe.model.document import Document


class Distributor(Document):
	def before_insert(self):
		# New houses join at the END of the priority order (planner columns /
		# default-house pick sort "priority asc"). Without this a fresh row's 0
		# would jump it in FRONT of every explicitly-ordered incumbent.
		if not self.priority:
			row = frappe.db.sql("select max(priority) from `tabDistributor`")
			self.priority = (row[0][0] or 0) + 1
