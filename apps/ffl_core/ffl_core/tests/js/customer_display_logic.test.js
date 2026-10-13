'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { buildSnapshot, deriveStage, formatMoney } = require('../../public/js/customer_display_logic.js');

test('formatMoney formats USD with thousands separators', () => {
	assert.strictEqual(formatMoney(0), '$0.00');
	assert.strictEqual(formatMoney(639), '$639.00');
	assert.strictEqual(formatMoney(1234.5), '$1,234.50');
	assert.strictEqual(formatMoney(1234567.89), '$1,234,567.89');
	assert.strictEqual(formatMoney(-40), '-$40.00');
	assert.strictEqual(formatMoney(null), '$0.00');
});

test('buildSnapshot maps an empty doc to zeroed totals', () => {
	const snap = buildSnapshot({});
	assert.deepStrictEqual(snap.items, []);
	assert.deepStrictEqual(snap.totals, { subtotal: 0, discount: 0, tax: 0, grand_total: 0 });
	assert.deepStrictEqual(snap.payment, { due: 0, paid: 0, change: 0 });
});

test('buildSnapshot maps items and totals from the POS doc', () => {
	const snap = buildSnapshot({
		items: [
			{ item_name: 'Glock 19', item_code: 'G19', qty: 1, uom: 'Nos', rate: 599, amount: 599 },
			{ item_code: 'MAG-9', qty: 2, stock_uom: 'Nos', rate: 20, amount: 40 },
		],
		net_total: 639, discount_amount: 0, total_taxes_and_charges: 0, grand_total: 639,
		paid_amount: 0, change_amount: 0,
	});
	assert.deepStrictEqual(snap.items, [
		{ name: 'Glock 19', qty: 1, uom: 'Nos', rate: 599, amount: 599 },
		{ name: 'MAG-9', qty: 2, uom: 'Nos', rate: 20, amount: 40 },  // name falls back to item_code, uom to stock_uom
	]);
	assert.strictEqual(snap.totals.grand_total, 639);
});

test('deriveStage: cart vs payment vs welcome vs thankyou', () => {
	const empty = buildSnapshot({});
	const cart = buildSnapshot({ items: [{ item_code: 'X', qty: 1, rate: 5, amount: 5 }], grand_total: 5 });
	const paying = buildSnapshot({ items: [{ item_code: 'X', qty: 1, rate: 5, amount: 5 }], grand_total: 5, paid_amount: 5 });

	assert.strictEqual(deriveStage(null, empty), 'welcome');
	assert.strictEqual(deriveStage('welcome', cart), 'cart');
	assert.strictEqual(deriveStage('cart', paying), 'payment');
	// Cart emptied after reaching payment => a completed sale.
	assert.strictEqual(deriveStage('payment', empty), 'thankyou');
	// Bare manual clear (never paid) => straight to welcome, no thanks.
	assert.strictEqual(deriveStage('cart', empty), 'welcome');
	// Stays in thankyou while empty; the receiver's own timer returns to welcome.
	assert.strictEqual(deriveStage('thankyou', empty), 'thankyou');
});
