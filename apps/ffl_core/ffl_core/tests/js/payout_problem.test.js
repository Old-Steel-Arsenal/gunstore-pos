'use strict';
// The one client check of how the store paid someone (pos_tender_guard.js payoutProblem; server:
// cash_drawer.check_payout_method), shared by the register's negative ticket, the Receive Goods "Pay the seller"
// dialog and the Cash Drawer page. No DOM / Frappe.
const test = require('node:test');
const assert = require('node:assert');
const { payoutProblem, PAYOUT_METHODS, negativeTicketState } = require('../../public/js/pos_tender_guard.js');

const SERVER = ['Cash', 'Zelle', 'Check', 'ACH'];

test('cash needs nothing more; the bank methods need their reference', () => {
	assert.strictEqual(payoutProblem('Cash', '', SERVER), '');
	assert.strictEqual(payoutProblem('Zelle', 'ZL-9', SERVER), '');
	assert.strictEqual(payoutProblem('Check', ' ', SERVER), 'reference');
	assert.strictEqual(payoutProblem('ACH', null, SERVER), 'reference');
});

test('only a method the page was given is taken; without a list the mirror decides', () => {
	assert.strictEqual(payoutProblem('Venmo', 'x', SERVER), 'method');
	assert.strictEqual(payoutProblem(undefined, '', SERVER), 'method');
	assert.strictEqual(payoutProblem('Cash', '', ['Zelle']), 'method');
	assert.deepStrictEqual(PAYOUT_METHODS, SERVER);
	assert.strictEqual(payoutProblem('Zelle', 'Z', undefined), '');
});

test('the register\'s negative ticket asks the same check', () => {
	const ticket = (method, reference) => ({
		grand_total: -100, items: [{ item_code: 'TRADE-IN-CREDIT', amount: -100 }],
		ffl_trade_in_payout_method: method, ffl_trade_in_payout_reference: reference,
	});
	assert.strictEqual(negativeTicketState(ticket('Zelle', '')).problem, 'reference');
	assert.strictEqual(negativeTicketState(ticket('Venmo', 'x')).problem, 'method');
	assert.strictEqual(negativeTicketState(ticket('Cash', '')).problem, null);
});
