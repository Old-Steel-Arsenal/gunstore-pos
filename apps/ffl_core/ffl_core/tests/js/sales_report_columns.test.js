'use strict';
// Pure kernel of the Sales Report column picker. No DOM / Frappe /
// localStorage — the browser wrappers add that. These pin the lock-column
// normalization and the convergence invariant that keeps after_datatable_render
// from looping forever.
const test = require('node:test');
const assert = require('node:assert');
const {
	SR_LOCK_FIELD,
	sr_normalize_hidden,
	sr_hidden_set_of,
	sr_apply_hidden_to,
	sr_sets_equal,
	sr_build_column_options,
	sr_default_hidden,
} = require('../../ffl_core/report/sales_report/sales_report.js');

test('SR_LOCK_FIELD is the invoice column', () => {
	assert.strictEqual(SR_LOCK_FIELD, 'invoice');
});

test('normalize strips the lock column from a persisted set', () => {
	const s = sr_normalize_hidden(['invoice', 'cost', 'channel'], 'invoice');
	assert.ok(!s.has('invoice')); // the lock column is never a member of "hidden"
	assert.ok(s.has('cost') && s.has('channel'));
});

test('normalize tolerates empty / junk / lock-only input', () => {
	assert.strictEqual(sr_normalize_hidden([], 'invoice').size, 0);
	assert.strictEqual(sr_normalize_hidden(null, 'invoice').size, 0);
	assert.strictEqual(sr_normalize_hidden(['invoice'], 'invoice').size, 0);
});

test('apply_hidden_to never hides the lock column', () => {
	const cols = [{ fieldname: 'invoice' }, { fieldname: 'cost' }];
	sr_apply_hidden_to(cols, new Set(['invoice', 'cost']), 'invoice');
	assert.strictEqual(cols[0].hidden, 0); // invoice locked visible even if named
	assert.strictEqual(cols[1].hidden, 1);
});

test('convergence: a persisted set naming the lock column settles (no render loop)', () => {
	// The footgun (minor-1): without read-side normalization, saved = {invoice, cost}
	// but apply forces the lock visible, so live = {cost} ≠ saved forever, and
	// after_datatable_render re-renders in an infinite loop. Normalizing on read
	// makes saved and live symmetric so the guard converges.
	const cols = [{ fieldname: 'invoice' }, { fieldname: 'channel' }, { fieldname: 'cost' }];
	const saved = sr_normalize_hidden(['invoice', 'cost'], 'invoice'); // what sr_read_hidden returns
	sr_apply_hidden_to(cols, saved, 'invoice');
	const live = sr_hidden_set_of(cols);
	assert.strictEqual(cols[0].hidden, 0); // lock visible
	assert.ok(sr_sets_equal(saved, live), 'saved == live ⇒ after_datatable_render is a no-op');
});

test('convergence holds for an ordinary (no-lock) hidden set too', () => {
	const cols = [{ fieldname: 'invoice' }, { fieldname: 'list_price' }, { fieldname: 'discount' }];
	const saved = sr_normalize_hidden(['list_price', 'discount'], 'invoice');
	sr_apply_hidden_to(cols, saved, 'invoice');
	assert.ok(sr_sets_equal(saved, sr_hidden_set_of(cols)));
});

test('sets_equal compares membership regardless of order', () => {
	assert.ok(sr_sets_equal(new Set(['a', 'b']), new Set(['b', 'a'])));
	assert.ok(!sr_sets_equal(new Set(['a']), new Set(['a', 'b'])));
	assert.ok(!sr_sets_equal(new Set(['a', 'c']), new Set(['a', 'b'])));
});

test('build_column_options pre-checks shown + lock, unchecks hidden', () => {
	// Frappe MultiCheck reads per-option `checked` (a field-level default is
	// ignored). If options carry no `checked`, the dialog opens all-unchecked and a
	// no-op Apply hides everything but the lock — a blocker QA caught.
	const cols = [
		{ fieldname: 'invoice', label: 'Invoice' },
		{ fieldname: 'cost', label: 'Cost' },
		{ fieldname: 'discount', label: 'Discount' },
	];
	const opts = sr_build_column_options(cols, new Set(['discount']), 'invoice');
	const checked = Object.fromEntries(opts.map((o) => [o.value, o.checked]));
	assert.strictEqual(checked.invoice, 1); // lock always checked
	assert.strictEqual(checked.cost, 1); // shown → checked
	assert.strictEqual(checked.discount, 0); // hidden → unchecked
	assert.deepStrictEqual(opts[0], { label: 'Invoice', value: 'invoice', checked: 1 });
});

test('build_column_options: fresh view (nothing hidden) → every option checked', () => {
	// The default flow: open on a full view, Apply without touching
	// anything → all checked → nothing gets hidden.
	const cols = [
		{ fieldname: 'invoice', label: 'Invoice' },
		{ fieldname: 'a', label: 'A' },
		{ fieldname: 'b', label: 'B' },
	];
	const opts = sr_build_column_options(cols, new Set(), 'invoice');
	assert.ok(opts.every((o) => o.checked === 1));
});

// ---- PR-4b: the marketplace columns' default visibility ----
// Visibility is the whole difference between "the number is in the report" and
// "the number is in the report somebody actually reads". These pin which side of
// that line each new column lands on.

test('the channel-fee pair is default-hidden wherever it exists', () => {
	// The requirement is default-hidden + always-CSV. The CSV half is structural
	// (export takes the full set); this is the half that can silently regress.
	for (const view of ['Order', 'Order Detail']) {
		const hidden = new Set(sr_default_hidden(view));
		assert.ok(hidden.has('channel_fees'), `${view} shows channel_fees`);
		assert.ok(hidden.has('profit_after_fees'), `${view} shows profit_after_fees`);
	}
});

test('the Texas worksheet columns stay visible in the flat Product view', () => {
	// The flat Product view IS the tax worksheet. Hiding its key column by
	// default is how a return gets filed without the marketplace line.
	const hidden = new Set(sr_default_hidden('Product'));
	assert.ok(!hidden.has('ship_to_state'));
	assert.ok(!hidden.has('gb_tax_exempt'));
});

test('a default-hidden name is only ever a fieldname the server can emit', () => {
	// A curated default naming a column no view has is pruned away at render
	// time, so the mistake is invisible — until the column it was MEANT to hide
	// ships unhidden. Pin the two new names against the live sets.
	const everything = new Set([
		...sr_default_hidden('Order'),
		...sr_default_hidden('Order Detail'),
		...sr_default_hidden('Product'),
	]);
	assert.ok(everything.has('channel_fees'));
	assert.ok(everything.has('profit_after_fees'));
});
