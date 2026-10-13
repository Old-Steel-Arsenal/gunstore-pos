'use strict';
// POS recycle bin: the "Deleted" filter must
// render the bin from ffl_core's endpoint, leave every other status on
// ERPNext's own refresh_list, and a Restore must land the clerk back on the
// Draft list with the restored invoice open.
const test = require('node:test');
const assert = require('node:assert');
const bin = require('../../public/js/pos_recycle_bin.js');

function fakeEnv(calls, responses) {
	return {
		call(opts) { calls.push(['call', opts.method, opts.args]); const r = responses[opts.method]; if (opts.callback) opts.callback({ message: r }); return Promise.resolve(); },
		esc: (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])),
		fmt: (v, c) => `${c} ${v}`,
		when: (d) => `@${d}`,
		ellipsis: (s, n) => (s.length > n ? s.slice(0, n) + '...' : s),
		confirm: (msg, yes) => { calls.push(['confirm', msg]); yes(); },
		alert: (msg) => calls.push(['alert', msg]),
		$: () => ({}),
	};
}

// Minimal stand-in for erpnext.PointOfSale.PastOrderList (pos_past_order_list.js).
function fakeListClass(calls) {
	return class {
		constructor(status) {
			this.status_field = { df: { options: 'Draft\nPaid' }, _v: status, get_value() { return this._v; }, set_value(v) { this._v = v; calls.push(['status', v]); }, refresh() { calls.push(['status.refresh']); } };
			this.search_field = { get_value: () => 'alex' };
			this.html = [];
			this.$invoices_container = { html: (h) => { this.html = [h]; }, append: (h) => this.html.push(h), on: (ev, sel) => calls.push(['on', sel]) };
			this.events = { reset_summary: () => calls.push(['reset_summary']), open_invoice_data: (d, n) => calls.push(['open', d, n]) };
		}
		make_filter_section() { calls.push(['stock.make_filter_section']); }
		refresh_list() { calls.push(['stock.refresh_list']); }
	};
}

test('withDeletedOption appends once', () => {
	assert.strictEqual(bin.withDeletedOption('Draft\nPaid'), 'Draft\nPaid\nDeleted');
	assert.strictEqual(bin.withDeletedOption('Draft\nDeleted'), 'Draft\nDeleted');
	assert.strictEqual(bin.withDeletedOption(['Draft']), 'Draft\nDeleted');
});

test('patch adds the option, binds restore, and is idempotent', () => {
	const calls = [];
	const L = fakeListClass(calls);
	assert.strictEqual(bin.applyPastOrderListPatch(L, fakeEnv(calls, {})), true);
	assert.strictEqual(bin.applyPastOrderListPatch(L, fakeEnv(calls, {})), false);
	const l = new L('Draft');
	l.make_filter_section();
	assert.strictEqual(l.status_field.df.options, 'Draft\nPaid\nDeleted');
	assert.deepStrictEqual(calls.filter((c) => c[0] === 'on').map((c) => c[1]), ['.ffl-deleted-invoice', '.ffl-restore-btn']);
	assert.ok(calls.some((c) => c[0] === 'stock.make_filter_section'));
});

test('non-Deleted statuses fall through to the stock refresh_list', () => {
	const calls = [];
	const L = fakeListClass(calls);
	bin.applyPastOrderListPatch(L, fakeEnv(calls, {}));
	new L('Draft').refresh_list();
	assert.deepStrictEqual(calls, [['stock.refresh_list']]);
});

test('Deleted status renders the bin from the ffl endpoint with the search term', () => {
	const calls = [];
	const L = fakeListClass(calls);
	const row = { deleted_document: 'dd1', name: 'ACC-PSINV-2026-00001', customer_name: 'alex <sample>', grand_total: 3084.04, currency: 'USD', deleted_by: 'cashier@example.com', deleted_at: '2026-09-08 17:51:11' };
	bin.applyPastOrderListPatch(L, fakeEnv(calls, { [bin.LIST_METHOD]: [row] }));
	const l = new L('Deleted');
	l.refresh_list();
	assert.deepStrictEqual(calls[0], ['reset_summary']);
	assert.deepStrictEqual(calls[1], ['call', bin.LIST_METHOD, { search_term: 'alex' }]);
	assert.ok(!calls.some((c) => c[0] === 'stock.refresh_list'));
	const html = l.html.join('');
	assert.ok(html.includes('data-deleted-document="dd1"'));
	assert.ok(html.includes('alex &lt;sample&gt;'));           // escaped
	assert.ok(bin.deletedCardHtml({ ...row, customer_name: 'x'.repeat(30) }, fakeEnv([], {}).esc, () => '', () => '', (s, n) => s.slice(0, n) + '...').includes('x'.repeat(20) + '...'));
	assert.ok(bin.deletedCardHtml(row, fakeEnv([], {}).esc, () => '<b>', () => '', null).includes('&lt;b&gt;'));  // fmt output escaped too
	assert.ok(html.includes('USD 3084.04'));
	assert.ok(html.includes('ffl-restore-btn'));
	assert.ok(html.includes('Deleted @2026-09-08 17:51:11 by cashier@example.com'));
});

test('empty bin shows a placeholder, not a blank list', () => {
	const calls = [];
	const L = fakeListClass(calls);
	bin.applyPastOrderListPatch(L, fakeEnv(calls, { [bin.LIST_METHOD]: [] }));
	const l = new L('Deleted');
	l.refresh_list();
	assert.ok(l.html.join('').includes('No deleted orders'));
});

test('restore goes back to Draft and opens the restored invoice; renamed is announced', () => {
	const calls = [];
	const L = fakeListClass(calls);
	const env = fakeEnv(calls, { [bin.RESTORE_METHOD]: { name: 'ACC-PSINV-2026-00002', original_name: 'ACC-PSINV-2026-00001', renamed: true } });
	bin.applyPastOrderListPatch(L, env);
	const l = new L('Deleted');
	bin.restoreDeleted(l, env, 'dd1');
	assert.deepStrictEqual(calls[0], ['call', bin.RESTORE_METHOD, { name: 'dd1' }]);
	assert.ok(calls[1][1].includes('00002') && calls[1][1].includes('00001'));
	assert.deepStrictEqual(calls.slice(2), [['status', 'Draft'], ['open', 'POS Invoice', 'ACC-PSINV-2026-00002']]);
});
