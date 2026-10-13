'use strict';
// Pure kernel of the Storage Locations page: reading a scanned label, what a scan does on
// the Move tab, the map's cell states and search, the session's Undo bookkeeping, and the
// New Zone checks. No DOM / Frappe.
const test = require('node:test');
const assert = require('node:assert');
const {
	stgBuildIndex, stgResolveLocation, stgCellState, stgCellTitle, stgZoneSummary, stgSearchMap,
	stgScanPlan, stgPushSession, stgMarkUndone, stgCanUndo, stgValidateZone, stgValidateSides, stgPreviewLabels, stgZonePreviewText,
	stgFilterRows, stgNeedsAttention,
} = require('../../ffl_core/page/storage_map/storage_map.js');

const gun = (serial, item) => ({ serial, item_name: item });
const ZONES = [
	{
		name: 'TSA-A', zone_name: 'A', kind: 'Slots', disabled: 0,
		locations: [
			{ name: 'TSA-A1', label: 'A1', disabled: 0, guns: [gun('SN100', 'Glock 19')], qty: 0, items: 0 },
			{ name: 'TSA-A2', label: 'A2', disabled: 0, guns: [], qty: 0, items: 0 },
			{ name: 'TSA-A3', label: 'A3', disabled: 1, guns: [], qty: 0, items: 0 },
		],
	},
	{
		name: 'TSA-Rack', zone_name: 'Rack', kind: 'Open', disabled: 0,
		locations: [
			{ name: 'TSA-Rack 1', label: 'Rack 1', disabled: 0, guns: [], qty: 12, items: 2 },
			{ name: 'TSA-Rack 2', label: 'Rack 2', disabled: 0, guns: [gun('SN200', 'AR-15'), gun('SN201', 'AR-10')], qty: 0, items: 0 },
			{ name: 'TSA-Rack 3', label: 'Rack 3', disabled: 0, guns: [], qty: 0, items: 0 },
		],
	},
];

test('a label or a barcode finds the location, whatever the case and spaces', () => {
	const index = stgBuildIndex(ZONES);
	assert.strictEqual(stgResolveLocation(index, 'a1'), 'TSA-A1');
	assert.strictEqual(stgResolveLocation(index, ' TSA-A2 '), 'TSA-A2');
	assert.strictEqual(stgResolveLocation(index, 'rack 2'), 'TSA-Rack 2');
	assert.strictEqual(stgResolveLocation(index, 'SN100'), null); // a serial is not a location
	assert.strictEqual(stgResolveLocation(index, ''), null);
	assert.strictEqual(stgResolveLocation(undefined, 'A1'), null);
});

test('a scan picks the destination, or is put into it, or asks for a destination first', () => {
	const index = stgBuildIndex(ZONES);
	assert.deepStrictEqual(stgScanPlan(index, null, 'A2'), { action: 'destination', location: 'TSA-A2' });
	assert.deepStrictEqual(stgScanPlan(index, 'TSA-A2', 'Rack 1'), { action: 'destination', location: 'TSA-Rack 1' });
	assert.deepStrictEqual(stgScanPlan(index, 'TSA-A2', ' SN100 '), { action: 'move', code: 'SN100', to: 'TSA-A2' });
	assert.deepStrictEqual(stgScanPlan(index, null, 'SN100'), { action: 'need_destination' });
	assert.deepStrictEqual(stgScanPlan(index, 'TSA-A2', '   '), { action: 'none' });
});

test('cell states: a slot is free or holds its gun, an open place is free or in use, disabled wins', () => {
	const [a, rack] = ZONES;
	assert.deepStrictEqual(a.locations.map((l) => stgCellState(l, a.kind)), ['gun', 'free', 'disabled']);
	assert.deepStrictEqual(rack.locations.map((l) => stgCellState(l, rack.kind)), ['items', 'items', 'free']);
});

test('the cell tooltip names the guns and counts the items', () => {
	const [a, rack] = ZONES;
	assert.strictEqual(stgCellTitle(a.locations[0], 'Slots'), 'A1: Glock 19 (SN100)');
	assert.strictEqual(stgCellTitle(a.locations[1], 'Slots'), 'A2: free slot');
	assert.strictEqual(stgCellTitle(rack.locations[0], 'Open'), 'Rack 1: 2 items, 12 units');
	assert.strictEqual(stgCellTitle(rack.locations[2], 'Open'), 'Rack 3: empty');
	assert.strictEqual(stgCellTitle(a.locations[2], 'Slots'), 'A3: disabled');
});

test('the zone summary leaves disabled places out of the capacity', () => {
	assert.deepStrictEqual(stgZoneSummary(ZONES[0]), { used: 1, total: 2, free: 1, text: '1 of 2 used' });
	assert.deepStrictEqual(stgZoneSummary(ZONES[1]), { used: 2, total: 3, free: null, text: '2 of 3 in use' });
});

test('search finds a place by label, a gun by serial or model, and nothing typed means no filter', () => {
	assert.strictEqual(stgSearchMap(ZONES, ''), null);
	assert.deepStrictEqual([...stgSearchMap(ZONES, 'sn200')], ['TSA-Rack 2']);
	assert.deepStrictEqual([...stgSearchMap(ZONES, 'glock')], ['TSA-A1']);
	assert.deepStrictEqual([...stgSearchMap(ZONES, 'rack')].sort(), ['TSA-Rack 1', 'TSA-Rack 2', 'TSA-Rack 3']);
	assert.deepStrictEqual([...stgSearchMap(ZONES, 'zzz')], []);
});

test('the session list keeps newest first, caps its length, and Undo marks rather than removes', () => {
	let list = [];
	for (let i = 1; i <= 5; i++) list = stgPushSession(list, { move: `M${i}`, message: `m${i}` }, 3);
	assert.deepStrictEqual(list.map((e) => e.move), ['M5', 'M4', 'M3']);
	const after = stgMarkUndone(list, 'M4');
	assert.deepStrictEqual(after.map((e) => stgCanUndo(e)), [true, false, true]);
	assert.strictEqual(after[1].undone, true);
	assert.strictEqual(list[1].undone, undefined); // no input is mutated
	assert.strictEqual(stgCanUndo({ message: 'already there' }), false);
	assert.strictEqual(stgCanUndo(null), false);
});

test('New Zone is checked the way the server checks it', () => {
	assert.strictEqual(stgValidateZone({ zone_name: 'A', kind: 'Slots', count: 30 }), '');
	assert.strictEqual(stgValidateZone({ zone_name: 'Display Case 1', kind: 'Open', count: 5 }), '');
	assert.strictEqual(stgValidateZone({ zone_name: 'Rack 1', kind: 'Open' }), ''); // no count asked for
	assert.match(stgValidateZone({ zone_name: '', kind: 'Slots', count: 3 }), /short name/);
	assert.match(stgValidateZone({ zone_name: 'A/B', kind: 'Slots', count: 3 }), /short name/);
	assert.match(stgValidateZone({ zone_name: 'A', kind: 'Shelf', count: 3 }), /Slots or Open/);
	assert.match(stgValidateZone({ zone_name: 'A', kind: 'Slots', count: 0 }), /between 1 and 500/);
	assert.match(stgValidateZone({ zone_name: 'A', kind: 'Slots', count: 501 }), /between 1 and 500/);
	assert.match(stgValidateZone({ zone_name: 'A', kind: 'Slots', count: 2.5 }), /between 1 and 500/);
	assert.strictEqual(stgValidateZone({ zone_name: 'A', kind: 'Slots', count: 30, sides: 2 }), '');
	assert.strictEqual(stgValidateZone({ zone_name: 'A', kind: 'Slots', count: 30, sides: null }), ''); // empty = fit
	assert.match(stgValidateZone({ zone_name: 'A', kind: 'Slots', count: 30, sides: 51 }), /Sides go from 1 to 50/);
	assert.match(stgValidateZone({ zone_name: 'A', kind: 'Slots', count: 30, sides: -1 }), /Sides go from 1 to 50/);
	// editing an existing zone checks only its sides: a 600-slot zone can still be changed
	assert.strictEqual(stgValidateSides(2), '');
	assert.strictEqual(stgValidateSides(null), '');
	assert.match(stgValidateSides(2.5), /Sides go from 1 to 50/);
});

test('the label preview names slots A1 and open places Rack 1', () => {
	assert.deepStrictEqual(stgPreviewLabels('A', 'Slots', 3), ['A1', 'A2', 'A3']);
	assert.deepStrictEqual(stgPreviewLabels('Rack 1', 'Open', 30), ['Rack 1']); // an Open zone is one location
	assert.deepStrictEqual(stgPreviewLabels('A', 'Slots', 0), []);
	assert.strictEqual(stgPreviewLabels('A', 'Slots', 9999).length, 500);
});

test('the Not placed filter searches the fields it is given', () => {
	const rows = [{ serial: 'SN1', item_name: 'Glock' }, { serial: 'SN2', item_name: 'Colt' }];
	assert.deepStrictEqual(stgFilterRows(rows, 'colt', 'serial', 'item_name').map((r) => r.serial), ['SN2']);
	assert.strictEqual(stgFilterRows(rows, '', 'serial').length, 2);
	assert.deepStrictEqual(stgFilterRows(undefined, 'x', 'serial'), []);
});

test('the Not placed badge adds up everything that still needs a place or a word', () => {
	assert.strictEqual(stgNeedsAttention({ guns: 2, items: 3, to_confirm: 1 }), 6);
	assert.strictEqual(stgNeedsAttention(undefined), 0);
});

test('the New Zone preview shows real labels, an example while the name is empty, one name for Open', () => {
	assert.strictEqual(stgZonePreviewText('A', 'Slots', 30), 'A1, A2, A3 … A30 (sides follow the shape on the map)');
	assert.strictEqual(stgZonePreviewText('B', 'Slots', 2, 1), 'B1, B2');
	assert.strictEqual(stgZonePreviewText('', 'Slots', 30), 'e.g. A + 30 → A1 … A30');
	assert.strictEqual(stgZonePreviewText('  ', 'Slots', 5), 'e.g. A + 30 → A1 … A30');
	assert.strictEqual(stgZonePreviewText('Rack 1', 'Open', 30), 'Rack 1');
	assert.strictEqual(stgZonePreviewText('', 'Open'), 'e.g. Rack 1');
	assert.strictEqual(stgZonePreviewText('A', 'Slots', 0), '');
});

test('the New Zone preview lists each side: odd / even by default, or in order', () => {
	assert.strictEqual(stgZonePreviewText('A', 'Slots', 30, 2),
		'Side 1: A1, A3, A5 … A29 · Side 2: A2, A4, A6 … A30');
	assert.strictEqual(stgZonePreviewText('A', 'Slots', 30, 2, 'Odd / even'),
		'Side 1: A1, A3, A5 … A29 · Side 2: A2, A4, A6 … A30');
	assert.strictEqual(stgZonePreviewText('A', 'Slots', 30, 2, 'In order'),
		'Side 1: A1, A2, A3 … A15 · Side 2: A16, A17, A18 … A30');
	assert.strictEqual(stgZonePreviewText('A', 'Slots', 30, 1), 'A1, A2, A3 … A30');     // one side: no split
	assert.strictEqual(stgZonePreviewText('A', 'Slots', 30, 0),                         // the map decides
		'A1, A2, A3 … A30 (sides follow the shape on the map)');
	assert.strictEqual(stgZonePreviewText('A', 'Slots', 5, 2), 'Side 1: A1, A3, A5 · Side 2: A2, A4');
	// three sides and more count in order, whatever the numbering says
	assert.strictEqual(stgZonePreviewText('A', 'Slots', 8, 4, 'Odd / even'),
		'Side 1: A1, A2 · Side 2: A3, A4 · Side 3: A5, A6 …');
	assert.strictEqual(stgZonePreviewText('A', 'Slots', 4, 3), 'Side 1: A1, A2 · Side 2: A3, A4'); // no empty side
});

// ------------------------------------------------------------- floor plan kernel

const P = require('../../ffl_core/page/storage_map/storage_map.js');
const PLAN = { name: 'SFP-1', width: 60, height: 40 };
const near = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);
const nearPt = (a, b, eps = 1e-9) => { near(a.x, b.x, eps); near(a.y, b.y, eps); };

test('resizing from any handle keeps the opposite edge or corner exactly where it was, at any turn', () => {
	const handles = [[-1, -1], [0, -1], [1, -1], [1, 0], [1, 1], [0, 1], [-1, 1], [-1, 0]];
	// local point of a handle (hx, hy) in plan feet
	const at = (s, hx, hy) => {
		const t = s.r * Math.PI / 180, c = Math.cos(t), n = Math.sin(t);
		const u = hx * s.w / 2, v = hy * s.h / 2;
		return { x: s.x + u * c - v * n, y: s.y + u * n + v * c };
	};
	for (const r of [0, 90, 37, 215]) {
		const s = { type: 'zone', x: 20, y: 15, w: 8, h: 3, r };
		for (const [hx, hy] of handles) {
			const target = at(s, hx * 1.7, hy * 2.3); // drag the handle outwards
			const out = P.stgpResize(s, hx, hy, target.x, target.y, 0);
			nearPt(at(out, -hx, -hy), at(s, -hx, -hy));           // the anchor did not move
			// the dragged side ends where the pointer is: (1 + k) / 2 of the old size from the anchor
			if (hx) near(out.w, s.w * (1 + 1.7) / 2); else near(out.w, s.w);
			if (hy) near(out.h, s.h * (1 + 2.3) / 2); else near(out.h, s.h);
			assert.strictEqual(out.r, r);
		}
	}
});

test('a resize snaps the size, never goes below a quarter foot, and never past the cap', () => {
	const s = { x: 10, y: 10, w: 4, h: 2, r: 0 };
	const out = P.stgpResize(s, 1, 0, 14.3, 10, 0.5);      // right edge dragged to 14.3 -> width 6.3 -> 6.5
	assert.strictEqual(out.w, 6.5);
	near(out.x - out.w / 2, 8);                            // left edge stays at 8
	assert.strictEqual(P.stgpResize(s, 1, 0, 7, 10, 0).w, 0.25);  // dragged past the anchor
	assert.strictEqual(P.stgpResize(s, 1, 0, 200, 10, 0, 60).w, 60);
});

test('turning snaps to 15° steps unless free, and angles stay in 0–360', () => {
	const s = { x: 0, y: 0, w: 2, h: 2, r: 0 };
	assert.strictEqual(P.stgpRotateTo(s, 0, -5).r, 0);          // knob straight up
	assert.strictEqual(P.stgpRotateTo(s, 5, 0).r, 90);
	assert.strictEqual(P.stgpRotateTo(s, 5, 0.6).r, 90);        // 96.8° -> 90
	assert.strictEqual(P.stgpRotateTo(s, -5, 0).r, 270);
	near(P.stgpRotateTo(s, 5, 0.6, true).r, 96.84, 0.01);
	assert.strictEqual(P.stgpAngle(-90), 270);
	assert.strictEqual(P.stgpAngle(720.004), 0);
	assert.strictEqual(P.stgpAngle(359.999), 0);
});

test('slots fill the zone in rows, numbered like a page, square-ish when rows are automatic', () => {
	assert.deepStrictEqual(P.stgpSlotGrid(15, 1, 30, 1), { rows: 1, cols: 30, down: false });
	assert.deepStrictEqual(P.stgpSlotGrid(15, 1, 30, 3), { rows: 3, cols: 10, down: false });
	assert.deepStrictEqual(P.stgpSlotGrid(30, 1, 30), { rows: 1, cols: 30, down: false });   // 1 × 1 cells
	assert.deepStrictEqual(P.stgpSlotGrid(15, 2, 30), { rows: 2, cols: 15, down: false });   // 1 × 1 cells
	assert.deepStrictEqual(P.stgpSlotGrid(4, 4, 1), { rows: 1, cols: 1, down: false });
	assert.deepStrictEqual(P.stgpSlotGrid(4, 4, 5, 9), { rows: 5, cols: 1, down: false });   // never more rows than slots
	const cells = P.stgpSlotCells(10, 2, 20, 2, true);
	assert.strictEqual(cells.length, 20);
	assert.deepStrictEqual(cells[0], { x: 0, y: 0, w: 1, h: 1 });               // slot 1 top-left
	assert.deepStrictEqual(cells[9], { x: 9, y: 0, w: 1, h: 1 });
	assert.deepStrictEqual(cells[10], { x: 0, y: 1, w: 1, h: 1 });              // slot 11 starts row 2
	assert.strictEqual(P.stgpSlotCells(10, 2, 7, 2).length, 7);                 // a short last row
});

test('slots alternate across the lines like house numbers unless the zone counts in order', () => {
	// odd / even (the default): 1 and 2 face each other, 3 and 4 next along
	const cells = P.stgpSlotCells(10, 2, 20, 2);
	assert.strictEqual(cells.length, 20);
	assert.deepStrictEqual(cells[0], { x: 0, y: 0, w: 1, h: 1 });               // slot 1 top-left
	assert.deepStrictEqual(cells[1], { x: 0, y: 1, w: 1, h: 1 });               // slot 2 facing it
	assert.deepStrictEqual(cells[2], { x: 1, y: 0, w: 1, h: 1 });               // slot 3 next along
	assert.deepStrictEqual(cells[19], { x: 9, y: 1, w: 1, h: 1 });
	// a short count: the odd side gets the extra slot, nothing doubles up
	const seven = P.stgpSlotCells(10, 2, 7, 2);
	assert.deepStrictEqual(seven[6], { x: 7.5, y: 0, w: 2.5, h: 1 });                 // slot 7: 4th on the odd side
	assert.strictEqual(new Set(seven.map((c) => `${c.x},${c.y}`)).size, 7);
	// in order: a line fills before the next starts
	const ordered = P.stgpSlotCells(10, 2, 20, 2, true);
	assert.deepStrictEqual(ordered[9], { x: 9, y: 0, w: 1, h: 1 });
	assert.deepStrictEqual(ordered[10], { x: 0, y: 1, w: 1, h: 1 });            // slot 11 starts row 2
	assert.deepStrictEqual(P.stgpSlotLine(28, 30, 2, false), { line: 0, at: 14 });
	// a 3-row grid (a display case) reads like a page even when the zone is odd / even
	const grid = P.stgpSlotCells(4, 3, 12, 3);
	assert.deepStrictEqual(grid[1], { x: 1, y: 0, w: 1, h: 1 });
	assert.deepStrictEqual(grid[4], { x: 0, y: 1, w: 1, h: 1 });
	assert.deepStrictEqual(P.stgpSlotLine(29, 30, 2, true), { line: 1, at: 14 });
});

test('a tall zone runs its slots in columns, top to bottom, so dragging it upright keeps its shape', () => {
	// the same 30 slots in 2 lines, drawn wide then tall: 2 rows of 15, then 2 columns of 15
	assert.deepStrictEqual(P.stgpSlotGrid(15, 2, 30, 2), { rows: 2, cols: 15, down: false });
	assert.deepStrictEqual(P.stgpSlotGrid(2, 15, 30, 2), { rows: 15, cols: 2, down: true });
	assert.deepStrictEqual(P.stgpSlotGrid(1, 30, 30), { rows: 30, cols: 1, down: true });   // auto: square cells
	const cells = P.stgpSlotCells(2, 15, 30, 2, true);
	assert.deepStrictEqual(cells[0], { x: 0, y: 0, w: 1, h: 1 });               // slot 1 top-left
	assert.deepStrictEqual(cells[1], { x: 0, y: 1, w: 1, h: 1 });               // slot 2 below it
	assert.deepStrictEqual(cells[14], { x: 0, y: 14, w: 1, h: 1 });
	assert.deepStrictEqual(cells[15], { x: 1, y: 0, w: 1, h: 1 });              // slot 16 starts column 2
	// odd / even down a tall rack: odd in the left column, even facing them
	const sides = P.stgpSlotCells(2, 15, 30, 2);
	assert.deepStrictEqual(sides[1], { x: 1, y: 0, w: 1, h: 1 });               // slot 2 beside slot 1
	assert.deepStrictEqual(sides[28], { x: 0, y: 14, w: 1, h: 1 });             // slot 29 bottom-left
	assert.deepStrictEqual(P.stgpSlotGrid(4, 4, 6, 2), { rows: 2, cols: 3, down: false }); // square = wide
});

test('moving snaps to the grid and never leaves the plan', () => {
	const s = { x: 10, y: 10, w: 4, h: 2, r: 0 };
	assert.deepStrictEqual(P.stgpMoveTo(s, 12.26, 9.8, PLAN, 0.5), Object.assign({}, s, { x: 12.5, y: 10 }));
	assert.deepStrictEqual(P.stgpMoveTo(s, -5, 99, PLAN, 0.5), Object.assign({}, s, { x: 0, y: 40 }));
	assert.strictEqual(P.stgpMoveTo(s, 12.26, 9.8, PLAN, 0).x, 12.26);
	assert.ok(P.stgpInside({ x: 60, y: 0 }, PLAN));
	assert.ok(!P.stgpInside({ x: 60.1, y: 0 }, PLAN));
});

test('fit shows the whole plan at the canvas aspect; zoom and pinch keep the focal point', () => {
	const v = P.stgpFit(PLAN, 1000, 500);
	near(v.w / v.h, 2);
	assert.ok(v.x <= -1 && v.x + v.w >= 61 && v.y <= -1 && v.y + v.h >= 41);
	const lim = P.stgpZoomLimits(PLAN);
	// zoom: plan point (10, 5) stays at the same fraction of the view
	const z = P.stgpZoomAt(v, 2, 10, 5, lim);
	near(z.w, v.w / 2);
	near((10 - z.x) / z.w, (10 - v.x) / v.w);
	near((5 - z.y) / z.h, (5 - v.y) / v.h);
	assert.strictEqual(P.stgpZoomAt(v, 1000, 0, 0, lim).w, lim.min);
	// pinch: fingers spread 100 -> 200 px around a moving midpoint; the plan point that was under
	// the old midpoint is under the new one
	const px = 1000, a0 = { x: 400, y: 200 }, b0 = { x: 500, y: 200 }, a1 = { x: 300, y: 250 }, b1 = { x: 500, y: 250 };
	const p = P.stgpPinch(v, a0, b0, a1, b1, px, lim);
	near(p.w, v.w / 2);
	const under = (view, m) => ({ x: view.x + m.x * view.w / px, y: view.y + m.y * view.w / px });
	nearPt(under(p, { x: 400, y: 250 }), under(v, { x: 450, y: 200 }));
	// one finger is a pan
	const pan = P.stgpPinch(v, a0, a0, { x: 300, y: 100 }, { x: 300, y: 100 }, px, lim);
	near(pan.w, v.w);
	near(pan.x, v.x + 100 * v.w / px);
});

test('undo keeps the last 100 snapshots of the draft — shapes, name and size — as copies', () => {
	let stack = [];
	const ed = { shapes: [{ x: 1 }], plan_name: 'Showroom', width: 60, height: 40, sel: 3, undo: ['x'] };
	stack = P.stgpPushUndo(stack, ed);
	ed.shapes[0].x = 2;
	ed.width = 40;
	assert.deepStrictEqual(JSON.parse(stack[0]), { shapes: [{ x: 1 }], plan_name: 'Showroom', width: 60, height: 40 });
	for (let i = 0; i < 150; i++) stack = P.stgpPushUndo(stack, { shapes: [{ i }], plan_name: 'P', width: 10, height: 10 });
	assert.strictEqual(stack.length, 100);
	assert.deepStrictEqual(JSON.parse(stack[99]).shapes, [{ i: 149 }]);
});

test('a zone is free when no plan has it, and the plan being drawn counts with its draft', () => {
	const zones = [{ name: 'Z-A' }, { name: 'Z-B' }, { name: 'Z-C' }];
	const plans = [
		{ name: 'P1', layout: { shapes: [{ type: 'zone', zone: 'Z-A' }, { type: 'wall' }] } },
		{ name: 'P2', layout: { shapes: [{ type: 'zone', zone: 'Z-B' }] } },
	];
	assert.deepStrictEqual(P.stgpUnplaced(zones, plans).map((z) => z.name), ['Z-C']);
	const draft = { plan: 'P1', shapes: [{ type: 'zone', zone: 'Z-C' }] };   // A taken off, C put on
	assert.deepStrictEqual(P.stgpUnplaced(zones, plans, draft).map((z) => z.name), ['Z-A']);
	assert.deepStrictEqual(P.stgpUnplaced(zones, []).length, 3);
});

test('Place all lays zones out in rows under what is drawn, inside the plan', () => {
	const zones = [
		{ name: 'A', kind: 'Slots', positions: 30 }, { name: 'B', kind: 'Slots', positions: 30 },
		{ name: 'C', kind: 'Slots', positions: 30 }, { name: 'D', kind: 'Slots', positions: 30 },
		{ name: 'R1', kind: 'Open', positions: 1 },
	];
	assert.deepStrictEqual(P.stgpZoneSize(zones[0], PLAN), { w: 15, h: 2 });
	assert.deepStrictEqual(P.stgpZoneSize(zones[4], PLAN), { w: 6, h: 3 });
	assert.deepStrictEqual(P.stgpZoneSize(zones[4], { width: 4, height: 4 }), { w: 4, h: 3 });   // never longer than the plan
	assert.deepStrictEqual(P.stgpZoneSize(zones[0], { width: 4, height: 4 }), { w: 2, h: 2 });
	const wall = { type: 'wall', x: 30, y: 2, w: 60, h: 0.5, r: 0 };
	const out = P.stgpAutoPlace(zones, PLAN, [wall]);
	assert.deepStrictEqual(out.map((s) => s.zone), ['A', 'B', 'C', 'D', 'R1']);
	out.forEach((s) => { assert.strictEqual(s.type, 'zone'); assert.ok(P.stgpInside(s, PLAN)); });
	assert.ok(out.every((s) => s.y - s.h / 2 >= 2.25 + 1 - 1e-9));            // below the wall
	assert.deepStrictEqual(out.slice(0, 3).map((s) => s.y), [4.25, 4.25, 4.25]); // three 15 ft bands fit a row
	assert.ok(out[3].y > out[0].y);                                            // the fourth wraps
	const box = (s) => P.stgpBounds([s]);
	for (let i = 0; i < out.length; i++) for (let j = i + 1; j < out.length; j++) {
		const a = box(out[i]), b = box(out[j]);
		assert.ok(a.maxX <= b.minX || b.maxX <= a.minX || a.maxY <= b.minY || b.maxY <= a.minY, `${i} overlaps ${j}`);
	}
});

test('Place all carries on from the top when the room runs out, instead of piling zones on the bottom edge', () => {
	const small = { width: 20, height: 10 };
	const wall = { type: 'wall', x: 10, y: 8, w: 20, h: 0.5, r: 0 };             // drawing reaches y = 8.25
	const zones = Array.from({ length: 12 }, (_, i) => ({ name: `R${i}`, kind: 'Open', positions: 1 }));
	const out = P.stgpAutoPlace(zones, small, [wall]);
	assert.strictEqual(out.length, 12);
	out.forEach((s) => assert.ok(P.stgpInside(s, small) && s.y + s.h / 2 <= small.height + 1e-9, `${s.zone} sticks out`));
	assert.ok(out.filter((s) => s.y === small.height).length === 0, 'none clamped onto the bottom edge');
	assert.strictEqual(out[0].y, 2.5);                                          // first one starts at the top
	assert.ok(new Set(out.slice(0, 4).map((s) => `${s.x},${s.y}`)).size === 4);  // and they do not coincide
});

test('new walls, doors, areas and labels start at the given point with their own sizes', () => {
	assert.deepStrictEqual(P.stgpNewShape('wall', 10.2, 5.4, PLAN), { type: 'wall', x: 10, y: 5.5, w: 10, h: 0.5, r: 0 });
	assert.strictEqual(P.stgpNewShape('label', 1, 1, PLAN).text, 'Label');
	assert.strictEqual(P.stgpNewShape('area', 1, 1, PLAN).text, '');
	assert.strictEqual(P.stgpNewShape('wall', 1, 1, { width: 6, height: 4 }).w, 6);   // never longer than the plan
	assert.ok(!('text' in P.stgpNewShape('door', 1, 1, PLAN)));
});

test('search hits are counted per plan, and for zones on no plan', () => {
	const plans = [
		{ name: 'P1', layout: { shapes: [{ type: 'zone', zone: 'TSA-A' }] } },
		{ name: 'P2', layout: { shapes: [] } },
	];
	const zones = ZONES;   // TSA-A (on P1) and TSA-Rack (on no plan)
	assert.deepStrictEqual(P.stgpPlanHits(plans, zones, stgSearchMap(zones, 'glock')), { none: 0, P1: 1, P2: 0 });
	// both AR rifles sit in one location (Rack 2): one hit
	assert.deepStrictEqual(P.stgpPlanHits(plans, zones, stgSearchMap(zones, 'ar-1')), { none: 1, P1: 0, P2: 0 });
	assert.deepStrictEqual(P.stgpPlanHits(plans, zones, stgSearchMap(zones, 'rack')), { none: 3, P1: 0, P2: 0 });
	assert.deepStrictEqual(P.stgpPlanHits(plans, zones, null), { none: 0 });
});

test('text stays level near flat or upright, and follows a slanted shape along its long side', () => {
	assert.deepStrictEqual(P.stgpTextFrame(2, 1.5, 0), { w: 2, h: 1.5, turn: -0 });
	assert.deepStrictEqual(P.stgpTextFrame(2, 1.5, 10), { w: 2, h: 1.5, turn: -10 });   // level on screen
	assert.deepStrictEqual(P.stgpTextFrame(2, 1.5, 90), { w: 1.5, h: 2, turn: -90 });   // stood on end: sides swap
	assert.deepStrictEqual(P.stgpTextFrame(2, 1.5, 270), { w: 1.5, h: 2, turn: -270 });
	assert.deepStrictEqual(P.stgpTextFrame(2, 1.5, 180), { w: 2, h: 1.5, turn: -180 });
	assert.deepStrictEqual(P.stgpTextFrame(8, 3, 37), { w: 8, h: 3, turn: 0 });         // slanted: along the case
	assert.deepStrictEqual(P.stgpTextFrame(8, 3, 215), { w: 8, h: 3, turn: 180 });      // ... never upside down
	// a slanted tall shape runs its text along the long side, readable either way up
	for (const r of [30, 60, 120, 150, 210, 300]) {
		const f = P.stgpTextFrame(3, 8, r);
		assert.deepStrictEqual([f.w, f.h], [8, 3]);
		assert.strictEqual(P.stgpTextTurn(r + f.turn), 0, `${r}: upside down`);
	}
});

test('names wrap at spaces, and a word longer than a line is cut across lines', () => {
	assert.deepStrictEqual(P.stgpWrap('Display Case 1', 8), ['Display', 'Case 1']);
	assert.deepStrictEqual(P.stgpWrap('Display Case 1', 20), ['Display Case 1']);
	assert.deepStrictEqual(P.stgpWrap('Springfield', 4), ['Spri', 'ngfi', 'eld']);
	assert.deepStrictEqual(P.stgpWrap('  a   b ', 3), ['a b']);
	assert.deepStrictEqual(P.stgpWrap('', 5), []);
});

test('a name takes the biggest size its shape holds, on as many lines as it needs', () => {
	// wide box: one line at the cap
	assert.deepStrictEqual(P.stgpFitText('Display Case 1', 300, 40, 14, 8), { px: 14, lines: ['Display Case 1'] });
	// narrow tall box (a case stood on end): wraps instead of cutting
	const tall = P.stgpFitText('Display Case 1', 60, 160, 14, 8);
	assert.ok(tall.lines.length >= 2 && tall.lines.join(' ') === 'Display Case 1', JSON.stringify(tall));
	assert.ok(tall.px >= 8 && tall.px <= 14);
	// every line fits the width it was sized for
	tall.lines.forEach((line) => assert.ok(line.length <= Math.floor((60 * 0.9) / (tall.px * 0.6))));
	assert.deepStrictEqual(tall.lines, ['Display', 'Case 1']);                 // words kept whole
	// one line at a slightly smaller size beats two lines a size up
	assert.deepStrictEqual(P.stgpFitText('Rack 1', 51, 100, 14, 8).lines, ['Rack 1']);
	// a single long word in a tiny box is cut across lines rather than hidden
	assert.deepStrictEqual(P.stgpFitText('Springfield', 30, 100, 14, 8).lines.join(''), 'Springfield');
	// too small even at the minimum: the last line is cut short
	const cut = P.stgpFitText('Display Case Number One', 50, 12, 14, 8);
	assert.strictEqual(cut.px, 8);
	assert.strictEqual(cut.lines.length, 1);
	assert.ok(cut.lines[0].endsWith('…'));
	assert.strictEqual(P.stgpFitText('Safe 1', 6, 40, 14, 8), null);            // not even a few letters
	assert.strictEqual(P.stgpFitText('', 100, 100, 14, 8), null);
});

test('text is turned so it never reads upside down', () => {
	assert.strictEqual(P.stgpTextTurn(0), 0);
	assert.strictEqual(P.stgpTextTurn(90), 0);
	assert.strictEqual(P.stgpTextTurn(91), 180);
	assert.strictEqual(P.stgpTextTurn(270), 180);
	assert.strictEqual(P.stgpTextTurn(271), 0);
	assert.strictEqual(P.stgpTextTurn(-180), 180);
});

test('a plan name and size are checked, and shrinking cannot strand a shape outside', () => {
	assert.strictEqual(P.stgpCheckPlan({ plan_name: 'Showroom', width: 60, height: 40 }, []), '');
	assert.match(P.stgpCheckPlan({ plan_name: ' ', width: 60, height: 40 }, []), /name/);
	assert.match(P.stgpCheckPlan({ plan_name: 'x'.repeat(41), width: 60, height: 40 }, []), /name/);
	assert.match(P.stgpCheckPlan({ plan_name: '<img src=x onerror=alert(1)>', width: 60, height: 40 }, []), /no < or >/);
	assert.match(P.stgpCheckPlan({ plan_name: 'A', width: 3, height: 40 }, []), /between 4 and 500/);
	assert.match(P.stgpCheckPlan({ plan_name: 'A', width: 'abc', height: 40 }, []), /between/);
	const shapes = [{ x: 50, y: 10 }, { x: 5, y: 5 }];
	assert.match(P.stgpCheckPlan({ plan_name: 'A', width: 40, height: 40 }, shapes), /^1 shape/);
	assert.strictEqual(P.stgpCheckPlan({ plan_name: 'A', width: 50, height: 40 }, shapes), '');
	// a 50 ft wall cannot stay on a 30 × 30 plan even though its centre is inside
	assert.match(P.stgpCheckPlan({ plan_name: 'A', width: 30, height: 30 }, [{ x: 15, y: 1, w: 50, h: 0.5 }]), /longer than the plan/);
});
