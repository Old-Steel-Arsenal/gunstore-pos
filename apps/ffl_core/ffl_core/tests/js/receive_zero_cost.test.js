'use strict';
// Pure kernel of the Receive Goods zero-cost tiers.
// No DOM / Frappe / __() — the page turns a code
// into a translated message; these pin the RULE.
//
// Why a front-end test at all when the server is the authority: the server
// refusing a payload the only receive UI cannot fix is exactly the blocker this
// front end exists to close. Each arm below is a card state a
// receiver can actually produce, and each one has to be answerable ON the card.
const test = require('node:test');
const assert = require('node:assert');
const {
	RG_ZERO_COST_REASONS,
	RG_ZERO_COST_OTHER,
	rgIsConsignmentAcquisitionType,
	rgLineCostGap,
} = require('../../ffl_core/page/receive_goods/receive_goods.js');

const PURCHASE = { is_transfer: false, acquisition_type: 'Purchase' };
const CONSIGN = { is_transfer: false, acquisition_type: 'Consignment' };
const TRANSFER = { is_transfer: true, acquisition_type: '' };

// ---- the vocabulary mirror (ffl_core.setup.zero_cost) ----

test('the reason list mirrors the owner-approved five, Other last', () => {
	assert.deepStrictEqual(RG_ZERO_COST_REASONS, [
		'Supplier gift',
		'Promotional item',
		'Warranty replacement',
		'Sample',
		'Other',
	]);
	assert.strictEqual(RG_ZERO_COST_OTHER, 'Other');
	assert.strictEqual(RG_ZERO_COST_REASONS[RG_ZERO_COST_REASONS.length - 1], RG_ZERO_COST_OTHER);
});

// ---- the consignment vocabulary predicate (ffl_core.firearm) ----

test('consignment is matched case- and whitespace-insensitively', () => {
	['Consignment', 'consignment', '  CONSIGNMENT  ', 'Consignment '].forEach((v) => {
		assert.strictEqual(rgIsConsignmentAcquisitionType(v), true, v);
	});
});

test('"Consignment Out" is OUR stock leaving — never a consignor intake', () => {
	// A substring test would read this as "somebody else owns it" and wave a $0
	// line through unconfirmed. Fixed set, same as the server's frozenset.
	assert.strictEqual(rgIsConsignmentAcquisitionType('Consignment Out'), false);
});

test('an empty / absent acquisition type is not consignment', () => {
	[undefined, null, '', '   ', 'Purchase', 'FFL Transfer'].forEach((v) => {
		assert.strictEqual(rgIsConsignmentAcquisitionType(v), false, String(v));
	});
});

// ---- tier 1: a customer transfer — forced $0, never asked ----

test('transfer: $0 with nothing ticked is fine (the server forces $0 anyway)', () => {
	assert.strictEqual(rgLineCostGap({ rate: 0 }, TRANSFER), '');
});

// ---- tier 2: a consignor's gun — legitimately $0, no confirmation ----

test('consignment intake: $0 needs no confirmation (m-3 must keep working)', () => {
	assert.strictEqual(rgLineCostGap({ rate: 0 }, CONSIGN), '');
	assert.strictEqual(
		rgLineCostGap({ rate: 0 }, { is_transfer: false, acquisition_type: '  consignment ' }), '');
});

// ---- tier 3: a real purchase with a cost ----

test('a real purchase with a cost passes', () => {
	assert.strictEqual(rgLineCostGap({ rate: 430 }, PURCHASE), '');
	assert.strictEqual(rgLineCostGap({ rate: '430' }, PURCHASE), '');
});

test('a cost AND the free tick contradict each other', () => {
	assert.strictEqual(
		rgLineCostGap({ rate: 430, zero_cost_confirmed: 1, zero_cost_reason: 'Sample' }, PURCHASE),
		'cost_with_free_tick');
});

test('a negative cost is refused', () => {
	assert.strictEqual(rgLineCostGap({ rate: -5 }, PURCHASE), 'negative');
});

// ---- tier 4: a real purchase that really was free — the escape hatch ----

test('the eleven bad receipts: $0 with nothing ticked is refused', () => {
	// This is the exact shape of the 11 real purchases that went in at $0 over
	// three months and became indistinguishable from the Lifecards.
	assert.strictEqual(rgLineCostGap({ rate: 0 }, PURCHASE), 'unconfirmed_zero');
	assert.strictEqual(rgLineCostGap({ rate: '' }, PURCHASE), 'unconfirmed_zero');
	assert.strictEqual(rgLineCostGap({}, PURCHASE), 'unconfirmed_zero');
});

test('ticking free without a reason is refused', () => {
	assert.strictEqual(
		rgLineCostGap({ rate: 0, zero_cost_confirmed: 1 }, PURCHASE), 'no_reason');
	assert.strictEqual(
		rgLineCostGap({ rate: 0, zero_cost_confirmed: 1, zero_cost_reason: '   ' }, PURCHASE),
		'no_reason');
});

test('"Other" without the written explanation is refused', () => {
	assert.strictEqual(
		rgLineCostGap({ rate: 0, zero_cost_confirmed: 1, zero_cost_reason: 'Other' }, PURCHASE),
		'other_needs_note');
	assert.strictEqual(
		rgLineCostGap(
			{ rate: 0, zero_cost_confirmed: 1, zero_cost_reason: 'Other', zero_cost_note: '  ' },
			PURCHASE),
		'other_needs_note');
});

test('THE POINT OF THIS PAGE: a genuinely free purchase goes through', () => {
	// The Trailblazer Lifecards. Without this arm the only receive UI has no
	// path for a real $0 acquisition at all.
	assert.strictEqual(
		rgLineCostGap(
			{ rate: 0, zero_cost_confirmed: 1, zero_cost_reason: 'Supplier gift' }, PURCHASE),
		'');
	assert.strictEqual(
		rgLineCostGap(
			{
				rate: 0, zero_cost_confirmed: 1, zero_cost_reason: 'Other',
				zero_cost_note: 'Range demo unit the rep left behind.',
			},
			PURCHASE),
		'');
});

test('every listed reason is accepted on its own except Other', () => {
	RG_ZERO_COST_REASONS.forEach((reason) => {
		const gap = rgLineCostGap(
			{ rate: 0, zero_cost_confirmed: 1, zero_cost_reason: reason }, PURCHASE);
		assert.strictEqual(gap, reason === RG_ZERO_COST_OTHER ? 'other_needs_note' : '', reason);
	});
});

// ---- fail-closed coercion (the server's flt/cint half) ----

test('a tick with no reason behind it does not open the escape hatch', () => {
	// The tick alone is not the confirmation — the reason is. Named for what
	// this actually asserts: the string-typed tick is a separate case, below.
	assert.strictEqual(
		rgLineCostGap({ rate: 0, zero_cost_confirmed: 1, zero_cost_reason: '' }, PURCHASE),
		'no_reason');
});

test('a string-typed tick is read as ticked here, and the server is the stricter half', () => {
	// JS truthiness says "0"/"false" are ticked; the server's cint() says "0" is
	// NOT. The page cannot build this payload (its checkbox yields 0|1), so the
	// divergence only shows up for API/import callers — and it falls the safe
	// way: this side would let the line through, the server still refuses it as
	// an unconfirmed $0. Pinned so the asymmetry is a recorded decision rather
	// than a surprise for whoever next compares the two halves.
	['0', 'false'].forEach((raw) => {
		assert.strictEqual(
			rgLineCostGap(
				{ rate: 0, zero_cost_confirmed: raw, zero_cost_reason: 'Supplier gift' },
				PURCHASE),
			'', raw);
	});
});

test('an unreadable cost falls to the confirm-or-enter arm, not through it', () => {
	['$500', 'abc', undefined].forEach((raw) => {
		assert.strictEqual(rgLineCostGap({ rate: raw }, PURCHASE), 'unconfirmed_zero', String(raw));
	});
});

test('a transfer line is never blocked by a stale confirmation', () => {
	assert.strictEqual(
		rgLineCostGap({ rate: 500, zero_cost_confirmed: 1 }, TRANSFER), '');
});
