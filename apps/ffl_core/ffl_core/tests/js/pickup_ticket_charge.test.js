'use strict';
// Complete Order on a counter pickup's 4473 ticket (POS Invoice.ffl_counter_order). The money is on the order the
// ticket hands over; a register copy re-priced to the gun's list price once asked to charge the customer again.
// The bundle is desk code that does not load under Node: this runs its setupPayrocPosHook block alone against
// stand-in frappe / POS controller objects and checks that a ticket never opens the card dialog or reaches the
// terminal, is saved back to the server's $0 and repainted, and still submits — while an ordinary card sale on the
// same harness does reach the card dialog (so the harness exercises the real path).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const kernel = require('../../public/js/pos_tender_guard.js');
const payrocCharges = require('../../public/js/payroc_charges.js');
const BUNDLE = fs.readFileSync(path.join(__dirname, '../../public/js/ffl_core.bundle.js'), 'utf8');

function flt(v) { const n = parseFloat(v); return isNaN(n) ? 0 : n; }

function zeroed(doc) {
	// what keep_ticket_free sends back
	doc.items.forEach((r) => { r.rate = 0; r.price_list_rate = 0; r.amount = 0; });
	doc.payments.forEach((p) => { p.amount = 0; p.base_amount = 0; });
	Object.assign(doc, { grand_total: 0, rounded_total: 0, paid_amount: 0, change_amount: 0 });
}

function harness(doc) {
	const seen = { calls: [], dialogs: [], alerts: [], saves: 0, submits: 0, repaints: [] };
	const frm = {
		doc,
		is_dirty: () => false,
		dirty() { frm._dirty = true; },
		save() { seen.saves++; if (doc.ffl_counter_order) zeroed(doc); return Promise.resolve(); },
		reload_doc: () => Promise.resolve(),
	};
	class Dialog {
		constructor(opts) {
			seen.dialogs.push(opts.title);
			this.handlers = {};
			this.$wrapper = { on: (evt, fn) => { this.handlers[evt] = fn; }, is: () => false };
		}
		show() { setTimeout(() => this.handlers['hidden.bs.modal'] && this.handlers['hidden.bs.modal'](), 0); }
		hide() {}
	}
	const frappe = {
		ui: { Dialog },
		call: async (opts) => {
			seen.calls.push(opts.method);
			if (opts.method === 'ffl_core.firearm.firearm_items_in') return { message: [] };
			return { message: {} };
		},
		db: { get_value: async () => ({ message: { outstanding_amount: 0 } }) },
		show_alert: (m) => { seen.alerts.push(m.message); },
		msgprint: (m) => { seen.alerts.push(m.message || m); },
		utils: { escape_html: (v) => String(v) },
	};
	const controller = {
		frm,
		cart: { load_invoice: () => seen.repaints.push('cart') },
		payment: {
			events: { submit_invoice: () => { seen.submits++; } },
			render_payment_section: () => seen.repaints.push('payment'),
			render_payment_mode_dom() {}, update_totals_section() {},
		},
	};
	const sandbox = {
		frappe, flt, cint: (v) => parseInt(v, 10) || 0,
		__: (s, args) => (args ? s.replace(/\{(\d+)\}/g, (_m, i) => args[+i]) : s),
		format_currency: (v) => `$${Number(v).toFixed(2)}`,
		document: { readyState: 'complete' },
		location: { hash: '#point-of-sale', pathname: '/app/point-of-sale' },
		setTimeout,
		erpnext: { PointOfSale: { Controller: function Controller() {} } },
		cur_pos: controller,
		ffl: {
			payrocCharges,
			pickupTicketState: kernel.pickupTicketState,
			negativeTicketState: kernel.negativeTicketState,
		},
	};
	sandbox.window = sandbox;
	const start = BUNDLE.indexOf('(function setupPayrocPosHook() {');
	assert.ok(start > 0, 'Payroc block not found in the bundle');
	const end = BUNDLE.indexOf('\n})();', start) + '\n})();'.length;
	vm.runInNewContext(BUNDLE.slice(start, end), sandbox);
	assert.ok(controller.payment.__payrocWrapped, 'the block wrapped Complete Order');
	return { seen, submit: () => controller.payment.events.submit_invoice() };
}

const REPRICED_TICKET = () => ({
	name: 'POS-T1', doctype: 'POS Invoice', ffl_counter_order: 'SI-1', is_return: 0,
	grand_total: 670, rounded_total: 670, paid_amount: 670, change_amount: 0,
	items: [{ item_code: 'GUN', rate: 670, price_list_rate: 670, amount: 670 }],
	payments: [{ mode_of_payment: 'Credit Card', amount: 670, base_amount: 670 }],
	payroc_charges: [],
});

test('a re-priced pickup ticket never opens the card dialog or the terminal, and submits at $0', async () => {
	const { seen, submit } = harness(REPRICED_TICKET());
	await submit();
	assert.deepStrictEqual(seen.dialogs, [], 'no card dialog');
	assert.ok(!seen.calls.some((m) => /payroc/.test(m)), `no terminal call: ${seen.calls}`);
	assert.strictEqual(seen.saves, 1, 'saved so the server pins it back to $0');
	assert.deepStrictEqual(seen.repaints, ['cart', 'payment'], 'the screen shows the server\'s $0');
	assert.ok(seen.alerts.some((m) => /pickup ticket for SI-1/.test(m)), 'the cashier is told why');
	assert.strictEqual(seen.submits, 1, 'Complete Order goes through');
});

test('a $0 pickup ticket submits without a save, a prompt or a terminal call', async () => {
	const doc = REPRICED_TICKET();
	zeroed(doc);
	const { seen, submit } = harness(doc);
	await submit();
	assert.deepStrictEqual([seen.dialogs, seen.saves, seen.alerts], [[], 0, []]);
	assert.ok(!seen.calls.some((m) => /payroc/.test(m)));
	assert.strictEqual(seen.submits, 1);
});

test('control: an ordinary card sale on the same harness reaches the card dialog', async () => {
	const doc = REPRICED_TICKET();
	doc.ffl_counter_order = '';
	const { seen, submit } = harness(doc);
	await submit();
	assert.deepStrictEqual(seen.dialogs, ['Card payment']);
	assert.strictEqual(seen.submits, 0, 'the cashier backed out of the dialog');
});
