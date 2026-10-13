'use strict';
// Pure kernel of scan-first receiving: every session
// line belongs to a bucket — an open Distributor Order (held for that order), an
// outside-POS Distributor Invoice, or plain 'stock' — and Submit books one receive
// per bucket. No DOM / Frappe.
const test = require('node:test');
const assert = require('node:assert');
const {
	rgBucketOf, rgGroupByBucket, rgCandKind, rgCandBucket, rgCandLabel,
	rgSessionQtyFor, rgInvoiceCandidate, rgHasRoom, rgBucketItem, rgSlotRows,
	rgApplyLineEdit, rgNeedsSerials, rgSerialGap,
} = require('../../ffl_core/page/receive_goods/receive_goods.js');

const DO = (name) => ({ kind: 'do', name });
const INV = (name) => ({ kind: 'invoice', name });

test('a line without a bucket, or with the stock bucket, is store stock', () => {
	assert.strictEqual(rgBucketOf({}), 'stock');
	assert.strictEqual(rgBucketOf({ bucket: 'stock' }), 'stock');
	assert.strictEqual(rgBucketOf({ bucket: '' }), 'stock');
	assert.strictEqual(rgBucketOf(null), 'stock');
});

test('an order and an invoice with the same name are different buckets', () => {
	assert.strictEqual(rgBucketOf({ bucket: DO('DO-2609-0003') }), 'do:DO-2609-0003');
	assert.strictEqual(rgBucketOf({ bucket: INV('RSR-4022') }), 'invoice:RSR-4022');
	assert.notStrictEqual(rgBucketOf({ bucket: DO('X') }), rgBucketOf({ bucket: INV('X') }));
});

test('grouping keeps each order and invoice apart from stock and preserves line order', () => {
	const lines = [
		{ item_code: 'A', bucket: DO('DO-1') },
		{ item_code: 'B' },
		{ item_code: 'C', bucket: INV('I-2') },
		{ item_code: 'D', bucket: DO('DO-1') },
	];
	const g = rgGroupByBucket(lines);
	assert.deepStrictEqual(Object.keys(g).sort(), ['do:DO-1', 'invoice:I-2', 'stock']);
	assert.deepStrictEqual(g['do:DO-1'].map((l) => l.item_code), ['A', 'D']);
	assert.deepStrictEqual(g.stock.map((l) => l.item_code), ['B']);
});

test('an empty session groups to nothing', () => {
	assert.deepStrictEqual(rgGroupByBucket([]), {});
	assert.deepStrictEqual(rgGroupByBucket(undefined), {});
});

test('a candidate is an order\'s unless it says it is an invoice\'s', () => {
	const order = { do_name: 'DO-1', line_name: 'L1' };
	const inv = { kind: 'invoice', invoice_name: 'I-1', label: '4022', do_name: '' };
	assert.strictEqual(rgCandKind(order), 'do');
	assert.strictEqual(rgCandKind(inv), 'invoice');
	assert.deepStrictEqual(rgCandBucket(order), DO('DO-1'));
	assert.deepStrictEqual(rgCandBucket(inv), INV('I-1'));
	assert.strictEqual(rgCandLabel(order), 'DO-1');
	assert.strictEqual(rgCandLabel(inv), '4022');
});

// A staged accessory line of 3 on an invoice: the first scan ticks the staged row (to the
// scanned qty), and every later scan of the item returns to the SAME line until it holds
// 3 — never to store stock or another order, never past the invoice.
test('scans of a multi-quantity invoice line keep counting up on that line', () => {
	const cand = { kind: 'invoice', invoice_name: 'I-1', line_name: 'IL1', outstanding: 3 };
	const staged = { item_code: 'AMMO', qty: 3, staged: true, ticked: false, do_line: 'IL1', do_meta: cand };
	let lines = [staged];
	assert.strictEqual(rgInvoiceCandidate(lines, null, 'AMMO'), cand); // first scan: the staged row
	Object.assign(staged, { qty: 1, ticked: true }); // ticked with the scanned 1
	assert.strictEqual(rgSessionQtyFor(lines, null, 'IL1'), 1);
	assert.strictEqual(rgInvoiceCandidate(lines, null, 'AMMO'), cand); // 2 left
	lines = [...lines, { item_code: 'AMMO', qty: 1, do_line: 'IL1', do_meta: cand, bucket: INV('I-1') }];
	assert.strictEqual(rgInvoiceCandidate(lines, null, 'AMMO'), cand); // 1 left
	// The card open for the third unit counts too: a fourth scan has no room.
	const pending = { do_line: 'IL1', qty: 1 };
	lines = [...lines, { item_code: 'AMMO', qty: 1, do_line: 'IL1', do_meta: cand, bucket: INV('I-1') }];
	assert.strictEqual(rgInvoiceCandidate(lines, null, 'AMMO'), null); // full: falls to the normal flow
	assert.strictEqual(rgInvoiceCandidate(lines.slice(0, 2), pending, 'AMMO'), null);
	// Another item, or an order's staged row, is not an invoice candidate.
	assert.strictEqual(rgInvoiceCandidate(lines, null, 'OTHER'), null);
	const orderRow = { item_code: 'AMMO', staged: true, ticked: false, do_line: 'DL', do_meta: { do_name: 'DO-1' } };
	assert.strictEqual(rgInvoiceCandidate([orderRow], null, 'AMMO'), null);
});

test('guns on an invoice are one staged row per unit; each scan takes the next', () => {
	const cand = { kind: 'invoice', invoice_name: 'I-1', line_name: 'IL2', outstanding: 2 };
	const mk = () => ({ item_code: 'GUN', qty: 1, staged: true, ticked: false, do_line: 'IL2', do_meta: cand });
	const a = mk(), b = mk();
	assert.strictEqual(rgInvoiceCandidate([a, b], null, 'GUN'), cand);
	a.ticked = true;
	assert.strictEqual(rgInvoiceCandidate([a, b], null, 'GUN'), cand);
	b.ticked = true;
	assert.strictEqual(rgInvoiceCandidate([a, b], null, 'GUN'), null); // both confirmed: a third gun is extra
});

test('an order/invoice bucket sends sell price, and the antique answer only once asked', () => {
	const base = { do_line: 'L1', qty: 1, serials: ['S1'], rate: 5, sell_price: 9, is_antique: 1, year_made: 1890 };
	const ticked = rgBucketItem({ ...base, staged: true, ticked: true });
	assert.strictEqual(ticked.sell_price, 9);
	assert.strictEqual(ticked.is_antique, 1);
	assert.strictEqual(ticked.year_made, 1890);
	const typed = rgBucketItem(base); // a row added by scanning (not staged)
	assert.strictEqual(typed.is_antique, 1);
	const unasked = rgBucketItem({ ...base, is_antique: 0, year_made: 0, staged: true, ticked: false });
	assert.strictEqual(unasked.sell_price, 9);
	assert.ok(!('is_antique' in unasked) && !('year_made' in unasked));
});

test('two lines of one item, or two invoices carrying it, are all counted: the first with room wins', () => {
	const c1 = { kind: 'invoice', invoice_name: 'I-1', line_name: 'A1', outstanding: 1 };
	const c2 = { kind: 'invoice', invoice_name: 'I-1', line_name: 'A2', outstanding: 2 };
	const c3 = { kind: 'invoice', invoice_name: 'I-2', line_name: 'B1', outstanding: 1 };
	const row = (c, qty) => ({ item_code: 'AMMO', qty, staged: true, ticked: true, do_line: c.line_name, do_meta: c });
	let lines = [row(c1, 1), row(c2, 1), row(c3, 1)]; // every staged row confirmed once
	assert.strictEqual(rgInvoiceCandidate(lines, null, 'AMMO'), c2); // A1 full, A2 has 1 more
	lines = [...lines, row(c2, 1)];
	assert.strictEqual(rgInvoiceCandidate(lines, null, 'AMMO'), null); // A1, A2, B1 all full
	// The second invoice takes over when the first is full.
	assert.strictEqual(rgInvoiceCandidate([row(c1, 1), row(c3, 0)], null, 'AMMO'), c3);
});

test('a remembered order line that the session has filled has no room', () => {
	const cand = { do_name: 'DO-1', line_name: 'L1', outstanding: 2 };
	const row = (qty) => ({ do_line: 'L1', qty, do_meta: cand });
	assert.strictEqual(rgHasRoom(cand, [row(1)], null), true);
	assert.strictEqual(rgHasRoom(cand, [row(1), row(1)], null), false);
	assert.strictEqual(rgHasRoom(cand, [row(1)], { do_line: 'L1', qty: 1 }), false); // the open card counts
	assert.strictEqual(rgHasRoom(null, [], null), false);
});

// P1: a staged gun the clerk opened in the line editor and marked Antique / year made
// has been asked and answered; only a row nobody touched has its blank 0 withheld.
test('an antique answer given in the line editor is sent, though the row was only staged', () => {
	const staged = { do_line: 'L1', qty: 1, serials: ['S1'], rate: 5, staged: true, ticked: false,
		is_firearm: true, has_serial_no: true, is_antique: 0, year_made: 0 };
	assert.ok(!('is_antique' in rgBucketItem(staged))); // untouched: withheld
	rgApplyLineEdit(staged, { qty: 1, rate: 5, serials: ['S1'], fa: { is_antique: 1, year_made: 1890 } });
	const sent = rgBucketItem(staged);
	assert.strictEqual(sent.is_antique, 1);
	assert.strictEqual(sent.year_made, 1890);
	// Saying "not antique" in the editor is an answer too (an explicit 0, not silence).
	const no = { do_line: 'L2', qty: 1, serials: ['S2'], staged: true, ticked: false, is_firearm: true, is_antique: 1 };
	rgApplyLineEdit(no, { qty: 1, rate: 5, serials: ['S2'], fa: { is_antique: 0, year_made: 0 } });
	assert.strictEqual(rgBucketItem(no).is_antique, 0);
});

test('a save that only changes cost still marks the row as edited', () => {
	const row = { do_line: 'L1', qty: 1, rate: 5, serials: [], staged: true, ticked: false };
	rgApplyLineEdit(row, { qty: 1, rate: 7, serials: [], fa: null });
	assert.strictEqual(row.rate, 7);
	assert.strictEqual(row.edited, true);
});

// P2: serial-tracked Items that are not firearms take serials everywhere firearms do.
test('a serial-tracked non-firearm needs its serials, and a save never drops them', () => {
	const item = { is_firearm: false, has_serial_no: true, qty: 2, serials: ['A', 'B'] };
	assert.strictEqual(rgNeedsSerials(item), true);
	assert.strictEqual(rgNeedsSerials({ is_firearm: false, has_serial_no: false }), false);
	assert.strictEqual(rgNeedsSerials({ is_firearm: true, has_serial_no: false }), true);
	assert.strictEqual(rgSerialGap(item, 2, ['A', 'B']), '');
	assert.strictEqual(rgSerialGap(item, 2, ['A']), 'need_count');
	assert.strictEqual(rgSerialGap({ is_firearm: false, has_serial_no: false }, 2, []), '');
	// A used firearm (firearm, not serial-tracked): qty 1 and exactly one serial.
	const used = { is_firearm: true, has_serial_no: false };
	assert.strictEqual(rgSerialGap(used, 2, ['A']), 'used_qty');
	assert.strictEqual(rgSerialGap(used, 1, []), 'used_serial');
	assert.strictEqual(rgSerialGap(used, 1, ['A']), '');
	// Saving the editor with the serial box absent (or only cost changed) keeps what is there.
	const row = { ...item, do_line: 'L3', rate: 1, staged: true, ticked: false };
	rgApplyLineEdit(row, { qty: 2, rate: 9, serials: undefined, fa: null });
	assert.deepStrictEqual(row.serials, ['A', 'B']);
	rgApplyLineEdit(row, { qty: 2, rate: 9, serials: ['C', 'D'], fa: null });
	assert.deepStrictEqual(row.serials, ['C', 'D']);
});

// Storage locations: where each line goes in the store rides to the server with it.
test('a line carries its storage location into the bucket call, and a blank one is blank', () => {
	assert.strictEqual(rgBucketItem({ do_line: 'L1', qty: 1, serials: [], storage_location: 'TSA-A5' }).storage_location, 'TSA-A5');
	assert.strictEqual(rgBucketItem({ do_line: 'L1', qty: 1, serials: [] }).storage_location, '');
});

test('every gun line with a start location becomes one slot-planning row, in receipt order', () => {
	const rows = rgSlotRows([
		{ storage_location: 'TSA-A1', serials: ['S1', 'S2', 'S3'] },
		{ storage_location: 'TSA-Rack 1', serials: [] },           // loose stock: no slots
		{ serials: ['S4'] },                                        // no location chosen
		{ storage_location: 'TSA-A9', serials: ['S5'] },
	]);
	assert.deepStrictEqual(rows, [
		{ key: 'line0', start: 'TSA-A1', count: 3 },
		{ key: 'line3', start: 'TSA-A9', count: 1 },
	]);
	assert.deepStrictEqual(rgSlotRows(undefined), []);
});
