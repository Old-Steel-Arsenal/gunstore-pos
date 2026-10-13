// GunBroker Statement Reconciliation — filter bar + status colouring.
//
// Two ways in on purpose: attach the download, or paste the table off the
// screen. Neither statement has an API, and an operator who can see the table
// but cannot export it should still be able to reconcile the month.
// Pure colour rules, above the Frappe object and exported at the bottom, so
// node can exercise them without a browser (same shape as sales_report.js).

// Three colours for three kinds of work: red is money unaccounted for, orange
// is a month boundary and needs no action, green is done.
const GBSR_STATUS_COLOUR = {
	matched: "var(--green-600)",
	amount: "var(--red-600)",
	missing_in_pos: "var(--red-600)",
	missing_on_statement: "var(--red-600)",
	timing: "var(--orange-600)",
};

// The accrual state is the column that disagrees with the numbers. Both sides
// of a fee comparison are derived from the same order, so a journal that never
// posted — or was reversed on a cancellation and never re-raised — reconciles
// perfectly on the amounts. Anything but "posted" is red for that reason: the
// row can read Matched and still be the one worth stopping at. "posted" is
// left unstyled rather than painted green, so the eye lands on the exceptions
// instead of scanning a wall of green.
const GBSR_ACCRUAL_ALARM = new Set(["none", "cancelled", "reversed"]);

function gbsr_status_colour(status) {
	return GBSR_STATUS_COLOUR[status] || "";
}

function gbsr_accrual_colour(state) {
	// Membership, and nothing else. A row with no POS side carries "" and a
	// posted one carries "posted"; neither is in the set, so both come back
	// unpainted without a guard of their own. An early `if (!state)` here
	// looked like it was doing that job and was not — deleting it changed
	// nothing, which is the definition of a comment that lies.
	return GBSR_ACCRUAL_ALARM.has(state) ? "var(--red-600)" : "";
}

// Which column gets which colour. Pulled out of the formatter so node can
// reach it: with the mapping inline, deleting a whole branch left every test
// on the colour rules above perfectly green and the column unpainted.
function gbsr_cell_colour(fieldname, data) {
	if (!data) return "";
	if (fieldname === "status_label") return gbsr_status_colour(data.status);
	if (fieldname === "accrual") return gbsr_accrual_colour(data.accrual);
	return "";
}

function gbsr_emphasise(value, colour) {
	if (!colour) return value;
	return `<span style="color:${colour};font-weight:600">${value}</span>`;
}

// Guarded so node can require this file for the colour rules above; the
// browser has frappe and registers the report exactly as before.
if (typeof frappe !== "undefined") {
	frappe.query_reports["GunBroker Statement Reconciliation"] = {
		filters: [
			{
				fieldname: "statement_type",
				label: __("Statement"),
				fieldtype: "Select",
				// Newline-joined, like every other Select filter in this app.
				options: ["Fee Statement", "Sales Tax Statement"].join("\n"),
				default: "Fee Statement",
				reqd: 1,
			},
			{
				fieldname: "from_date",
				label: __("Statement Period From"),
				fieldtype: "Date",
				reqd: 1,
				default: frappe.datetime.add_months(frappe.datetime.month_start(), -1),
			},
			{
				fieldname: "to_date",
				label: __("Statement Period To"),
				fieldtype: "Date",
				reqd: 1,
				default: frappe.datetime.add_days(frappe.datetime.month_start(), -1),
			},
			{
				fieldname: "statement_file",
				label: __("Statement File (CSV)"),
				fieldtype: "Attach",
			},
			{
				fieldname: "statement_text",
				label: __("…or paste the table"),
				fieldtype: "Small Text",
			},
		],
		formatter(value, row, column, data, default_formatter) {
			value = default_formatter(value, row, column, data);
			return gbsr_emphasise(value, gbsr_cell_colour(column.fieldname, data));
		},
	};
}

if (typeof module !== "undefined" && module.exports) {
	module.exports = {
		GBSR_ACCRUAL_ALARM,
		gbsr_status_colour,
		gbsr_accrual_colour,
		gbsr_cell_colour,
		gbsr_emphasise,
	};
}
