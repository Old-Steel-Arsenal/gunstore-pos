'use strict';
// gunstore.extensions: only switched-on extensions
// render, action ids dedupe across extensions, a throwing condition hides.
const test = require('node:test');
const assert = require('node:assert');
const { createRegistry } = require('../../public/js/extensions_registry.js');

const boot = (m) => ({ gunshop_extensions: m });

test('register keeps order and last spec wins', () => {
	const r = createRegistry();
	r.register('b', { posActions: [{ id: 'x', label: 'X', onClick() {} }] });
	r.register('a', { posActions: [] });
	r.register('b', { posActions: [{ id: 'y', label: 'Y', onClick() {} }] });
	assert.deepStrictEqual(r.ids(), ['b', 'a']);
	assert.deepStrictEqual(r.get('b').posActions.map((a) => a.id), ['y']);
});

test('only enabled extensions contribute actions', () => {
	const r = createRegistry();
	r.register('on', { posActions: [{ id: 'on-1', label: 'On', onClick() {} }] });
	r.register('off', { posActions: [{ id: 'off-1', label: 'Off', onClick() {} }] });
	r.register('unknown', { posActions: [{ id: 'u-1', label: 'U', onClick() {} }] });
	const b = boot({ on: { enabled: true }, off: { enabled: false } });
	assert.deepStrictEqual(r.enabledIds(b), ['on']);
	assert.deepStrictEqual(r.actions('posActions', b).map((a) => [a.id, a.extension]), [['on-1', 'on']]);
	assert.deepStrictEqual(r.actions('posActions', {}), []);
});

test('action ids dedupe across extensions, first registration wins; bad entries dropped', () => {
	const r = createRegistry();
	r.register('a', { posActions: [{ id: 'dup', label: 'A', onClick() {} }, { label: 'no id', onClick() {} }] });
	r.register('b', { posActions: [{ id: 'dup', label: 'B', onClick() {} }, { id: 'no-click', label: 'N' }] });
	const b = boot({ a: { enabled: true }, b: { enabled: true } });
	assert.deepStrictEqual(r.actions('posActions', b).map((a) => a.label), ['A']);
});

test('condition: missing → applies; false → hidden; throwing → hidden', () => {
	const r = createRegistry();
	assert.strictEqual(r.applies({}, {}), true);
	assert.strictEqual(r.applies({ condition: (ctx) => !!ctx.invoice }, { invoice: null }), false);
	assert.strictEqual(r.applies({ condition: () => { throw new Error('boom'); } }, {}), false);
});

test('settingsLinks are readable even for a switched-off extension (the panel needs them)', () => {
	const r = createRegistry();
	r.register('x', { settingsLinks: [{ label: 'X Settings', doctype: 'X Settings' }] });
	assert.deepStrictEqual(r.settingsLinks('x'), [{ label: 'X Settings', doctype: 'X Settings' }]);
	assert.deepStrictEqual(r.settingsLinks('nope'), []);
});

test('isEnabled answers for an extension that never registered (its own Desk pages ask)', () => {
	const r = createRegistry();
	const b = boot({ acme_x: { enabled: true }, off: { enabled: false } });
	assert.strictEqual(r.isEnabled('acme_x', b), true);
	assert.strictEqual(r.isEnabled('off', b), false);
	assert.strictEqual(r.isEnabled('unknown', b), false);
	assert.strictEqual(r.isEnabled('acme_x', undefined), false);
	assert.deepStrictEqual(r.enabledIds(b), []);          // never registered → no actions, still "on"
});
