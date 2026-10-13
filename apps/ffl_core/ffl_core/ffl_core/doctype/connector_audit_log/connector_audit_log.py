"""Connector Audit Log — the durable record of every remote MCP connector call.

Written by ffl_core.api.connector.log_connector_call before each tool runs and
closed after it. A dedicated doctype rather than Activity Log: Activity Log is
purged after 90 days by Log Settings and on personal-data deletion, and this
trail must outlive both. Rows cannot be deleted.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class ConnectorAuditLog(Document):
	def on_trash(self):
		frappe.throw(_("{0} is a connector audit record — it cannot be deleted.").format(self.name))
