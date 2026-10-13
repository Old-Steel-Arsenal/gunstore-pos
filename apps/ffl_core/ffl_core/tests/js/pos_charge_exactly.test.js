'use strict';
// POS "Charge exactly": the pre-tax net that lands on a typed total, spread over
// the cart by current amounts; a second pass nudges a rounding miss; the wrap of
// ERPNext's ItemCart leaves the stock totals markup alone.
const test = require('node:test');
const assert = require('node:assert');
const ce = require('../../public/js/pos_charge_exactly.js');

const TX = { net_total: 1000, grand_total: 1082.5, rounded_total: 1082.5 }; // 8.25%

test('one line: 1135.54 at 8.25% works back to 1049.00', () => {
	const p = ce.plan([{ name: 'r1', qty: 1, rate: 1000 }], 1135.54, TX, 1);
	assert.deepStrictEqual(p.rates, { r1: 1049 });
});

test('several lines share the net by their current amounts; the last absorbs rounding', () => {
	const items = [{ name: 'a', qty: 1, rate: 900 }, { name: 'b', qty: 2, rate: 50 }];
	const p = ce.plan(items, 1000, TX, 1);
	const net = Math.round(1000 / 1.0825 * 100) / 100;
	assert.strictEqual(p.rates.a, Math.round(net * 0.9 * 100) / 100);
	assert.strictEqual(Math.round((p.rates.a + 2 * p.rates.b) * 100) / 100, net);
});

test('all-$0 lines split evenly; a $0 line beside priced ones stays $0', () => {
	const even = ce.plan([{ name: 'a', qty: 1, rate: 0 }, { name: 'b', qty: 1, rate: 0 }], 200, { net_total: 0, grand_total: 0 }, 1);
	assert.deepStrictEqual(even.rates, { a: 100, b: 100 });
	const gun = ce.plan([{ name: 'gun', qty: 1, rate: 0 }, { name: 'fee', qty: 1, rate: 40 }], 50, { net_total: 40, grand_total: 40 }, 1);
	assert.deepStrictEqual(gun.rates, { gun: 0, fee: 50 });
});

test('empty cart, zero target or zero-qty rows plan nothing', () => {
	assert.deepStrictEqual(ce.plan([], 10, TX, 1).rates, {});
	assert.deepStrictEqual(ce.plan([{ name: 'a', qty: 1, rate: 5 }], 0, TX, 1).rates, {});
	assert.deepStrictEqual(ce.plan([{ name: 'a', qty: 0, rate: 5 }], 10, TX, 1).rates, {});
});

test('nudge moves the biggest priced line by the pre-tax miss, and stops when on target', () => {
	const items = [{ name: 'a', qty: 1, rate: 1049 }, { name: 'b', qty: 1, rate: 10 }];
	const off = { net_total: 1059, grand_total: 1146.37, rounded_total: 1146.37 };
	const n = ce.nudge(items, 1146.36, off, 1);
	assert.deepStrictEqual(n.rates, { a: 1048.99 });
	assert.deepStrictEqual(ce.nudge(items, 1146.37, off, 1).rates, {});
	assert.deepStrictEqual(ce.nudge(items, 900, off, 1).rates, {}, 'a big miss is not a rounding miss');
});

test('shownTotal follows the cart: rounded unless the site disables rounding', () => {
	const doc = { grand_total: 10.4, rounded_total: 10 };
	assert.strictEqual(ce.shownTotal(doc, 0), 10);
	assert.strictEqual(ce.shownTotal(doc, 1), 10.4);
});

test('the wrap keeps ERPNext markup and adds the row once, before Checkout', () => {
	let html = '';
	const $sec = {
		find(sel) {
			if (sel === '.checkout-btn') return { length: 1, first() { return this; }, before(h) { html += String(h); } };
			if (sel === '.ffl-charge-exactly') return { length: html.includes('ffl-charge-exactly') ? 1 : 0 };
			return { length: 0 };
		},
	};
	global.__ = (s) => s;
	global.$ = (h) => ({ find() { return { on() {} }; }, toString() { return h; } });
	class ItemCart { make_cart_totals_section() { this.$totals_section = $sec; this.made = (this.made || 0) + 1; return 'stock'; } }
	ce.patch(ItemCart); ce.patch(ItemCart);
	const cart = new ItemCart();
	assert.strictEqual(cart.make_cart_totals_section(), 'stock', 'ERPNext return value untouched');
	cart.make_cart_totals_section();
	assert.strictEqual(cart.made, 2);
	assert.strictEqual((html.match(/ffl-charge-exactly-input/g) || []).length, 1, 'one row, one input');
	assert.ok(html.includes('Charge exactly'));
});

test('a trade-in credit (negative line) is never touched; the priced lines make up the rest', () => {
	const doc = { net_total: 700, grand_total: 742, rounded_total: 742 }; // 6%
	const p = ce.plan([{ name: 'gun', qty: 1, rate: 1000 }, { name: 'credit', qty: 1, rate: -300 }], 800, doc, 1);
	assert.strictEqual(p.rates.credit, undefined, 'credit line untouched');
	// net needed 800/1.06 = 754.72; gun must cover 754.72 + 300
	assert.strictEqual(p.rates.gun, 1054.72);
	assert.deepStrictEqual(ce.plan([{ name: 'credit', qty: 1, rate: -300 }], 100, doc, 1).rates, {}, 'nothing priced to move');
});

test('a locked line (ffl_locked_rate, e.g. a positive deposit) is never moved, even when it is the biggest', () => {
	const doc = { net_total: 600, grand_total: 636, rounded_total: 636 }; // 6%
	const items = [{ name: 'acc', qty: 1, rate: 100 }, { name: 'deposit', qty: 1, rate: 500, ffl_locked_rate: 500 }];
	const p = ce.plan(items, 700, doc, 1);
	assert.strictEqual(p.rates.deposit, undefined, 'deposit untouched');
	// net needed 700/1.06 = 660.38; the accessory covers it less the fixed 500
	assert.strictEqual(p.rates.acc, 160.38);
	const n = ce.nudge(items, 636.01, doc, 1);
	assert.deepStrictEqual(Object.keys(n.rates), ['acc'], 'nudge leaves the deposit alone');
});

test('the row follows Checkout: hidden on the payment screen, back on Edit Cart', () => {
	const shown = [];
	const $sec = { find(sel) { return sel === '.ffl-charge-exactly' ? { toggle(v) { shown.push(v); }, length: 1 } : { length: 0, first() { return this; } }; } };
	class ItemCart { toggle_checkout_btn(show) { this.$totals_section = $sec; return 'stock:' + show; } }
	ce.patch(ItemCart);
	const cart = new ItemCart();
	assert.strictEqual(cart.toggle_checkout_btn(false), 'stock:false');
	cart.toggle_checkout_btn(true);
	assert.deepStrictEqual(shown, [false, true]);
});
