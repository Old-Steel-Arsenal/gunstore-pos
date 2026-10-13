// MCP Settings: the primary action opens the audit trail.
frappe.ui.form.on("MCP Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Connector Audit Log"), () => {
			frappe.set_route("List", "Connector Audit Log");
		}).addClass("btn-primary");
	},
});
