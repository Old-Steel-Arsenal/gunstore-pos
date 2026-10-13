// Cash Drawer Weekly — one row per day for the bookkeeper (export to CSV from the menu).
// A day whose count was far enough off to need a reason is flagged red.
frappe.query_reports["Cash Drawer Weekly"] = {
	filters: [
		{
			fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company", reqd: 1,
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "from_date", label: __("From"), fieldtype: "Date", reqd: 1,
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -6),
		},
		{ fieldname: "to_date", label: __("To"), fieldtype: "Date", reqd: 1, default: frappe.datetime.get_today() },
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (!data) return value;
		if (data.row_kind === "note" || data.row_kind === "check") {
			return `<span style="font-weight:${data.row_kind === "check" ? 600 : 400}">${value}</span>`;
		}
		if (data.flagged && ["variance", "reason", "day"].includes(column.fieldname)) {
			return `<span style="color:var(--red-600);font-weight:600">${value}</span>`;
		}
		return value;
	},
};
