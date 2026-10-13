"""MCP Settings — the on/off switches for the remote MCP servers that AI
assistants (Claude, ChatGPT, any MCP client with OAuth) connect to.

Two surfaces, switched separately: the read-only one (accounting) and the full
one (writes, within each user's own role). The switches are enforced by the POS
itself (ffl_core.api.connector), and they own the OAuth settings the servers sign
in through: with either on, the authorization-server metadata and dynamic client
registration are on; with both off, they are off. Consent is never skipped —
every user approves on the POS. Re-applied on every migrate (sync_oauth), so a
deploy or a hand edit of OAuth Settings cannot drift from the switches.
"""
from __future__ import annotations

import frappe
from frappe.model.document import Document

SURFACES = {"cpa": "enable_cpa", "full": "enable_full"}
DOCTYPE = "MCP Settings"


def surface_enabled(surface) -> bool:
	field = SURFACES.get(surface) if isinstance(surface, str) else None
	if not field:
		return False
	try:
		return bool(frappe.db.get_single_value(DOCTYPE, field))
	except Exception:
		# Mid-deploy (new code, migrate not run yet): the switch reads off.
		return False


# The names the servers report (the MCP server's serverInfo) and that the skills
# and agents address tools by (mcp__gunstore-pos__*), so every install matches.
SERVER_NAMES = {"cpa": "gunstore-pos-cpa", "full": "gunstore-pos"}


def connector_urls() -> dict:
	"""The two server URLs and the Claude Code command for each."""
	base = frappe.utils.get_url().rstrip("/")
	out = {}
	for surface in SURFACES:
		url = f"{base}/connector/{surface}/mcp"
		out[f"{surface}_url"] = url
		out[f"{surface}_claude_code"] = (
			f"claude mcp add -s user --transport http {SERVER_NAMES[surface]} {url}")
	return out


def sync_oauth() -> None:
	"""OAuth Settings follow the switches; consent is never skipped (after_migrate)."""
	on = int(surface_enabled("cpa") or surface_enabled("full"))
	oauth = frappe.get_single("OAuth Settings")
	want = {"show_auth_server_metadata": on, "enable_dynamic_client_registration": on,
		"skip_authorization": 0}
	if any((oauth.get(k) or 0) != v for k, v in want.items()):
		oauth.update(want)
		oauth.flags.ignore_permissions = True
		oauth.save()
	# The older provider-level switch can also skip consent ("Auto").
	if frappe.db.get_single_value("OAuth Provider Settings", "skip_authorization") != "Force":
		frappe.db.set_single_value("OAuth Provider Settings", "skip_authorization", "Force")
	# A connector client (dynamically registered, owner Guest) never skips consent.
	for name in frappe.get_all("OAuth Client", filters={"owner": "Guest", "skip_authorization": 1},
			pluck="name"):
		frappe.db.set_value("OAuth Client", name, "skip_authorization", 0)


class MCPSettings(Document):
	def onload(self):
		# Shown the moment a box is ticked, from the site's own URL.
		for k, v in connector_urls().items():
			self.set(k, v)

	def validate(self):
		self.update(connector_urls())

	def on_update(self):
		sync_oauth()
