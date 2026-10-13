'use strict';
const test = require('node:test');
const assert = require('node:assert');

// The form script registers itself with frappe.ui.form.on at load; give it a stub.
const handlers = {};
global.__ = (s, args) => (args ? s.replace(/\{(\d+)\}/g, (_, i) => args[i]) : s);
global.frappe = {
	ui: { form: { on: (_dt, h) => Object.assign(handlers, h) } },
	validated: true,
	boot: {},
	call: () => { throw new Error('frappe.call not stubbed'); },
	confirm: () => { throw new Error('frappe.confirm not stubbed'); },
};
const { fflEraseGate } = require('../../ffl_core/doctype/ffl_settings/ffl_settings.js');

function form({ saved = 1, now = 0 } = {}) {
	const frm = {
		doc: { store_customer_id_numbers: now },
		__fflIdsSaved: !!saved,
		saves: 0,
		save() { this.saves += 1; },
		set_value(f, v) { this.doc[f] = v; },
	};
	return frm;
}
function stub({ total = 5, deferred = false } = {}) {
	const out = { calls: 0, confirms: [] };
	let release;
	global.frappe.validated = true;
	global.frappe.call = () => {
		out.calls += 1;
		return deferred
			? new Promise((res) => { release = () => res({ message: { total } }); })
			: Promise.resolve({ message: { total } });
	};
	global.frappe.confirm = (msg, yes, no) => out.confirms.push({ msg, yes, no });
	out.release = () => release();
	return out;
}

test('before_save is the gate and returns the promise the save waits on', () => {
	assert.strictEqual(typeof handlers.before_save, 'function');
	stub();
	assert.strictEqual(typeof handlers.before_save(form()).then, 'function');
});

test('unticked over stored IDs: the save is stopped and the confirm names the count and what stays', async () => {
	const s = stub({ total: 7 });
	const frm = form();
	await fflEraseGate(frm);
	assert.strictEqual(global.frappe.validated, false);
	assert.strictEqual(s.confirms.length, 1);
	assert.match(s.confirms[0].msg, /erases 7 stored ID number\(s\) and date\(s\) of birth/);
	assert.match(s.confirms[0].msg, /Attached ID copies, change history and deleted records are kept/);
	assert.strictEqual(frm.saves, 0);
});

test('a Ctrl+S before any count is back still waits for it (the race)', async () => {
	const s = stub({ total: 3, deferred: true });
	const frm = form();
	const gate = fflEraseGate(frm);
	assert.strictEqual(global.frappe.validated, true);   // count not back: nothing decided yet
	let done = false;
	gate.then(() => { done = true; });
	await Promise.resolve();
	assert.strictEqual(done, false);                      // the save is still held
	s.release();
	await gate;
	assert.strictEqual(global.frappe.validated, false);
	assert.strictEqual(s.confirms.length, 1);
});

test('Yes marks it confirmed and saves; that save is not asked again', async () => {
	const s = stub();
	const frm = form();
	await fflEraseGate(frm);
	s.confirms[0].yes();
	assert.strictEqual(frm.__fflEraseOk, true);
	assert.strictEqual(frm.saves, 1);
	global.frappe.validated = true;
	assert.strictEqual(fflEraseGate(frm), undefined);
	assert.strictEqual(global.frappe.validated, true);
	assert.strictEqual(s.calls, 1);
});

test('No (or closing the dialog) ticks the box again', async () => {
	const s = stub();
	const frm = form();
	await fflEraseGate(frm);
	s.confirms[0].no();
	assert.strictEqual(frm.doc.store_customer_id_numbers, 1);
	assert.strictEqual(frm.saves, 0);
});

test('nothing stored: no confirm and the save goes ahead', async () => {
	const s = stub({ total: 0 });
	await fflEraseGate(form());
	assert.strictEqual(global.frappe.validated, true);
	assert.strictEqual(s.confirms.length, 0);
});

test('only on -> off asks: off -> off, on -> on and off -> on never touch the server', () => {
	const s = stub();
	for (const [saved, now] of [[0, 0], [1, 1], [0, 1]]) {
		assert.strictEqual(fflEraseGate(form({ saved, now })), undefined);
	}
	assert.strictEqual(s.calls, 0);
	assert.strictEqual(global.frappe.validated, true);
});

test('a failed count request fails the save instead of erasing unasked', async () => {
	stub();
	global.frappe.call = () => Promise.reject(new Error('boom'));
	await assert.rejects(fflEraseGate(form()), /boom/);
});

test('ticking the box again forgets an earlier Yes', () => {
	const frm = form();
	frm.__fflEraseOk = true;
	handlers.store_customer_id_numbers(frm);
	assert.strictEqual(frm.__fflEraseOk, false);
});
