// POS recycle bin — a "Deleted" entry in the Recent Orders status filter that
// lists deleted POS Invoice drafts (ffl_core.api.pos_recycle_bin) with a Restore
// button, so a clerk who hit "Delete Order" on the wrong draft puts it back on
// the spot (incident 2026-09-08, a past invoice).
//
// Upgrade-safe: ERPNext's PastOrderList is wrapped on its prototype (via
// ffl.installClassHook, the same trap pos_tender_guard.js uses for the
// Controller), never edited. Everything DOM-free lives in the pure API below
// so Node can pin the patch's behaviour without a browser.
(function (root, factory) {
	var api = factory();
	if (typeof module === 'object' && module.exports) {
		module.exports = api;                       // Node test
	} else {
		root.ffl = root.ffl || {};
		root.ffl.posRecycleBin = api;
	}
})(typeof self !== 'undefined' ? self : this, function () {
	'use strict';

	var DELETED = 'Deleted';
	var LIST_METHOD = 'ffl_core.api.pos_recycle_bin.list_deleted_invoices';
	var RESTORE_METHOD = 'ffl_core.api.pos_recycle_bin.restore_deleted_invoice';

	// Append the Deleted option to a Select control's newline-joined options
	// (idempotent — ERPNext may rebuild the filter section on each POS boot).
	function withDeletedOption(options) {
		var list = typeof options === 'string' ? options.split('\n') : (options || []).slice();
		if (list.indexOf(DELETED) === -1) list.push(DELETED);
		return list.join('\n');
	}

	// One card per deleted draft. `esc` is the HTML escaper, `fmt` the currency
	// formatter, `when` the datetime formatter, `ellipsis` the name clipper —
	// injected so the markup rule is testable without frappe globals. Class
	// names mirror ERPNext pos_past_order_list.js get_invoice_html (v16) so the
	// stock card CSS applies; EVERYTHING interpolated goes through esc.
	function deletedCardHtml(row, esc, fmt, when, ellipsis) {
		var deletedAt = when(row.deleted_at);
		var customer = (ellipsis || function (s) { return s; })(row.customer_name || '', 20);
		return '<div class="invoice-wrapper ffl-deleted-invoice" data-deleted-document="' + esc(row.deleted_document) + '"'
			+ ' data-invoice-name="' + esc(row.name) + '">'
			+ '<div class="invoice-name-customer">'
			+ '<div class="invoice-customer">' + esc(customer) + '</div>'
			+ '<div class="invoice-name">' + esc(row.name) + '</div>'
			+ '<div class="invoice-date text-muted small">' + esc(__('Deleted {0} by {1}', [deletedAt, row.deleted_by || ''])) + '</div>'
			+ '</div>'
			+ '<div class="invoice-total-date">'
			+ '<div class="invoice-total">' + esc(fmt(row.grand_total, row.currency)) + '</div>'
			+ '<button type="button" class="btn btn-xs btn-primary ffl-restore-btn">' + esc(__('Restore')) + '</button>'
			+ '</div>'
			+ '</div><div class="seperator"></div>';
	}

	function emptyHtml(esc) {
		return '<div class="no-item-wrapper text-muted" style="padding:var(--padding-md)">'
			+ esc(__('No deleted orders to restore.')) + '</div>';
	}

	function __(s, args) {
		var t = (typeof window !== 'undefined' && window.__) ? window.__(s, args) : s;
		if (args && t === s) t = s.replace(/\{(\d+)\}/g, function (_, i) { return args[+i]; });
		return t;
	}

	// Wrap ERPNext's PastOrderList prototype: the Deleted filter renders the bin,
	// every other status falls through to the stock refresh_list untouched.
	// `env` supplies frappe.call / formatters so the patch is testable with fakes.
	function applyPastOrderListPatch(L, env) {
		if (!L || L.prototype.__fflRecycleBinPatched) return false;
		var stockMakeFilter = L.prototype.make_filter_section;
		var stockRefresh = L.prototype.refresh_list;
		if (typeof stockMakeFilter !== 'function' || typeof stockRefresh !== 'function') return false;

		L.prototype.make_filter_section = function fflMakeFilterSection() {
			stockMakeFilter.apply(this, arguments);
			var sf = this.status_field;
			if (sf && sf.df) {
				sf.df.options = withDeletedOption(sf.df.options);
				if (typeof sf.refresh === 'function') sf.refresh();
			}
			bindRestore(this, env);
		};

		L.prototype.refresh_list = function fflRefreshList() {
			var status = this.status_field && this.status_field.get_value();
			if (status !== DELETED) return stockRefresh.apply(this, arguments);
			var me = this;
			if (me.events && typeof me.events.reset_summary === 'function') me.events.reset_summary();
			var search_term = me.search_field ? me.search_field.get_value() : '';
			me.$invoices_container.html('');
			return env.call({
				method: LIST_METHOD, freeze: true, args: { search_term: search_term },
				callback: function (r) {
					var rows = (r && r.message) || [];
					if (!rows.length) { me.$invoices_container.append(emptyHtml(env.esc)); return; }
					rows.forEach(function (row) {
						me.$invoices_container.append(deletedCardHtml(row, env.esc, env.fmt, env.when, env.ellipsis));
					});
				},
			});
		};

		L.prototype.__fflRecycleBinPatched = true;
		return true;
	}

	// Restore click: confirm → API → back to the Draft list with the invoice open.
	// Bound once per component; the card's own click must not bubble into
	// ERPNext's `.invoice-wrapper` handler (it would try to open a doc that
	// doesn't exist yet).
	function bindRestore(list, env) {
		if (!list.$invoices_container || list.__fflRestoreBound) return;
		list.__fflRestoreBound = true;
		list.$invoices_container.on('click', '.ffl-deleted-invoice', function (e) {
			e.stopImmediatePropagation();
		});
		list.$invoices_container.on('click', '.ffl-restore-btn', function (e) {
			e.stopImmediatePropagation();
			var $card = env.$(this).closest('.ffl-deleted-invoice');
			var deletedDocument = $card.attr('data-deleted-document');
			var invoiceName = $card.attr('data-invoice-name');
			env.confirm(__('Restore {0}?', [env.esc(invoiceName)]), function () {
				restoreDeleted(list, env, deletedDocument);
			});
		});
	}

	function restoreDeleted(list, env, deletedDocument) {
		return env.call({
			method: RESTORE_METHOD, freeze: true, args: { name: deletedDocument },
			callback: function (r) {
				var out = (r && r.message) || {};
				if (!out.name) return;
				env.alert(out.renamed
					? __('Restored as {0} (the original number {1} was already taken).', [out.name, out.original_name])
					: __('{0} restored.', [out.name]));
				// Back to Draft (its onchange refreshes the list), then open the doc.
				if (list.status_field && typeof list.status_field.set_value === 'function') {
					list.status_field.set_value('Draft');
				}
				if (list.events && typeof list.events.open_invoice_data === 'function') {
					list.events.open_invoice_data('POS Invoice', out.name);
				}
			},
		});
	}

	return {
		DELETED: DELETED,
		LIST_METHOD: LIST_METHOD,
		RESTORE_METHOD: RESTORE_METHOD,
		withDeletedOption: withDeletedOption,
		deletedCardHtml: deletedCardHtml,
		applyPastOrderListPatch: applyPastOrderListPatch,
		restoreDeleted: restoreDeleted,
	};
});

// Browser wiring: trap erpnext.PointOfSale.PastOrderList the instant ERPNext's
// lazy POS bundle assigns it (ffl.installClassHook from pos_tender_guard.js),
// with the same bounded fallback retry that module uses.
(function fflRecycleBinWire() {
	if (typeof window === 'undefined' || typeof module === 'object') return;
	var env = {
		call: function (opts) { return frappe.call(opts); },
		esc: function (s) { return frappe.utils.escape_html(s == null ? '' : String(s)); },
		fmt: function (v, c) { return format_currency(v, c) || 0; },
		when: function (d) { return frappe.datetime.str_to_user(d); },
		ellipsis: function (s, n) { return frappe.ellipsis(s, n); },
		confirm: function (msg, yes) { return frappe.confirm(msg, yes); },
		alert: function (msg) { return frappe.show_alert({ message: msg, indicator: 'green' }); },
		$: function (el) { return $(el); },
	};
	function apply(L) { ffl.posRecycleBin.applyPastOrderListPatch(L, env); }
	function fallbackRetry(left) {
		var ns = window.erpnext && erpnext.PointOfSale;
		if (ns && ns.PastOrderList) { apply(ns.PastOrderList); return; }
		if (left > 0) setTimeout(function () { fallbackRetry(left - 1); }, 250);
	}
	function wire() {
		if (!window.frappe || typeof frappe.provide !== 'function') return;
		frappe.provide('erpnext.PointOfSale');
		var hook = window.ffl && ffl.installClassHook;
		if (hook && hook(erpnext.PointOfSale, 'PastOrderList', apply)) return;
		fallbackRetry(60);
	}
	if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
	else wire();
})();
