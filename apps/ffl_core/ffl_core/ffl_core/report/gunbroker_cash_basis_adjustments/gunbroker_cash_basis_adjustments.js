// GunBroker Cash Basis Adjustments — filter bar + section styling.
//
// The date pair defaults to LAST month, not this one: the schedule is a close
// artefact, and the overwhelmingly common run is "the month that just ended".
// Defaulting to the current month would hand the reader a half-finished period
// that looks like a finished one.
frappe.query_reports["GunBroker Cash Basis Adjustments"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			reqd: 1,
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "period_start",
			label: __("Period Start"),
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.add_months(frappe.datetime.month_start(), -1),
		},
		{
			fieldname: "period_end",
			label: __("Period End"),
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.add_days(frappe.datetime.month_start(), -1),
		},
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (!data) return value;
		if (data.row_kind === "section") {
			return `<span style="font-weight:700">${value}</span>`;
		}
		if (data.row_kind === "subtotal") {
			return `<span style="font-weight:600;color:var(--text-color)">${value}</span>`;
		}
		// A short or over payment is the one thing on this page a reader is
		// meant to stop at: the cheque and the invoice disagree, and items 5
		// and 7 follow the cheque.
		if (column.fieldname === "difference" && Math.abs(data.difference || 0) >= 0.01) {
			return `<span style="color:var(--orange-600);font-weight:600">${value}</span>`;
		}
		return value;
	},
};
