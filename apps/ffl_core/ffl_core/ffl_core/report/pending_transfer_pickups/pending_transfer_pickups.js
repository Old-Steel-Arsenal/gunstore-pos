// Pending Transfer Pickups — filter bar.
frappe.query_reports["Pending Transfer Pickups"] = {
	filters: [
		{
			fieldname: "customer",
			label: __("Customer"),
			fieldtype: "Link",
			options: "Customer",
		},
	],
};
