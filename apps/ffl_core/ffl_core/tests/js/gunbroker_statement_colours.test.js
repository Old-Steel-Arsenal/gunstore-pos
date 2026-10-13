'use strict';
// Colour rules for the statement reconciliation grid. No DOM, no Frappe — the
// report file guards its `frappe.query_reports` registration so node can
// require it for these.
//
// The accrual colour is the one that carries weight. Both sides of a fee
// comparison are derived from the same GunBroker order, so a journal that
// never posted — or was reversed on a cancellation and never re-raised —
// reconciles perfectly on the amounts and shows as Matched. This column is the
// only thing on the row that disagrees, which is exactly why it has to be the
// thing the eye lands on.
const test = require('node:test');
const assert = require('node:assert');
const {
	GBSR_ACCRUAL_ALARM,
	gbsr_status_colour,
	gbsr_accrual_colour,
	gbsr_cell_colour,
	gbsr_emphasise,
} = require('../../ffl_core/report/gunbroker_statement_reconciliation/gunbroker_statement_reconciliation.js');

test('an accrual that never posted is red', () => {
	assert.strictEqual(gbsr_accrual_colour('none'), 'var(--red-600)');
});

test('a reversed or cancelled accrual is red', () => {
	// The two states a human leaves behind after unwinding one by hand.
	assert.strictEqual(gbsr_accrual_colour('reversed'), 'var(--red-600)');
	assert.strictEqual(gbsr_accrual_colour('cancelled'), 'var(--red-600)');
});

test('a posted accrual is left unpainted, not painted green', () => {
	// A wall of green trains the eye to skim the column, which is the opposite
	// of what an exception marker is for.
	assert.strictEqual(gbsr_accrual_colour('posted'), '');
});

test('a row with no POS side says nothing about an accrual', () => {
	// "On statement, not in POS" has no order behind it to have accrued
	// anything; colouring that blank cell red would invent a second finding.
	// This falls out of set membership rather than a guard — pinned here so a
	// future "helpful" default (colour anything not posted) has to break it.
	assert.strictEqual(gbsr_accrual_colour(''), '');
	assert.strictEqual(gbsr_accrual_colour(undefined), '');
	assert.ok(!GBSR_ACCRUAL_ALARM.has(''));
	assert.ok(!GBSR_ACCRUAL_ALARM.has(undefined));
});

test('every alarm state is one the server actually emits', () => {
	// The server writes exactly none/cancelled/reversed/posted. A state in this
	// set that the server never produces is a rule that never fires, and one
	// the server produces that is missing from it is a red row that renders
	// plain — both silent.
	assert.deepStrictEqual([...GBSR_ACCRUAL_ALARM].sort(),
		['cancelled', 'none', 'reversed']);
	assert.ok(!GBSR_ACCRUAL_ALARM.has('posted'));
});

test('the status colours still map their five states', () => {
	assert.strictEqual(gbsr_status_colour('matched'), 'var(--green-600)');
	assert.strictEqual(gbsr_status_colour('amount'), 'var(--red-600)');
	assert.strictEqual(gbsr_status_colour('missing_in_pos'), 'var(--red-600)');
	assert.strictEqual(gbsr_status_colour('missing_on_statement'), 'var(--red-600)');
	assert.strictEqual(gbsr_status_colour('timing'), 'var(--orange-600)');
});

test('an unknown status is left alone rather than guessed at', () => {
	assert.strictEqual(gbsr_status_colour('something_new'), '');
});

test('emphasise wraps only when there is a colour', () => {
	assert.strictEqual(gbsr_emphasise('none', ''), 'none');
	assert.strictEqual(gbsr_emphasise('none', 'var(--red-600)'),
		'<span style="color:var(--red-600);font-weight:600">none</span>');
});

// ---- which column actually gets painted --------------------------------
// The rules above are only half of it. Deleting a whole branch of the
// column mapping left every one of them green and the column plain on the
// page, so the mapping is its own function with its own tests.

test('the accrual column is painted from the accrual state', () => {
	assert.strictEqual(
		gbsr_cell_colour('accrual', { status: 'matched', accrual: 'reversed' }),
		'var(--red-600)');
});

test('the status column is still painted from the status', () => {
	assert.strictEqual(
		gbsr_cell_colour('status_label', { status: 'timing', accrual: 'posted' }),
		'var(--orange-600)');
});

test('the two columns do not read each other', () => {
	// A Matched row with a missing accrual: green on the left, red on the
	// right. Crossing the wires would hide exactly the case the column is for.
	const row = { status: 'matched', accrual: 'none' };
	assert.strictEqual(gbsr_cell_colour('status_label', row), 'var(--green-600)');
	assert.strictEqual(gbsr_cell_colour('accrual', row), 'var(--red-600)');
});

test('no other column is painted', () => {
	const row = { status: 'amount', accrual: 'none' };
	for (const field of ['order_id', 'difference', 'pos_amount', 'reference']) {
		assert.strictEqual(gbsr_cell_colour(field, row), '', field);
	}
});

test('a totals or empty row is left alone', () => {
	assert.strictEqual(gbsr_cell_colour('accrual', null), '');
	assert.strictEqual(gbsr_cell_colour('status_label', undefined), '');
});
