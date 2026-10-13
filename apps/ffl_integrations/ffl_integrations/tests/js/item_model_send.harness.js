// Executes public/js/item.js under a stub Desk, so the Item form's GunBroker
// buttons and the model Send dialog actually RUN in a test instead of only being read.
//
// Why: the form used to offer buttons to anything that was not a firearm, and now a
// uniform-price gun gets a Send dialog of its own (guns to offer, today's price with
// a promotion flagged, the category, shipping, GunBroker's paid promotions). Both
// halves of that are easy to get wrong without any error: a Send button on a gun
// priced one by one can only come back refused, a missing one is a model nobody can
// list, and a dialog that quietly drops the premium note is a person buying extra
// features without being told they cost money.
//
// It is NOT a DOM test. `frappe.prompt` records its field list and callback, and
// assertions are made on those and on the calls the form makes.
//
// Usage: node item_model_send.harness.js <path to item.js>
// Prints one JSON object; exit code 0 = every check passed.

'use strict';
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync(process.argv[2], 'utf8');
const checks = [];

function check(name, fn) {
	try {
		fn();
		checks.push({ name, ok: true });
	} catch (e) {
		checks.push({ name, ok: false, error: String(e && e.message || e) });
	}
}
function assert(cond, message) { if (!cond) throw new Error(message || 'assertion failed'); }
function eq(a, b, message) {
	if (JSON.stringify(a) !== JSON.stringify(b)) {
		throw new Error((message || 'not equal') + `: ${JSON.stringify(a)} !== ${JSON.stringify(b)}`);
	}
}

// ------------------------------------------------------------------ stub Desk
function boot({ doc, enabled = true, responses = {} }) {
	const state = { buttons: [], calls: [], prompts: [], msgprints: [] };
	const sandbox = {
		console,
		__: (s, args) => String(s).replace(/\{(\d+)\}/g, (_, i) => (args || [])[+i]),
		cint: (v) => parseInt(v, 10) || 0,
		flt: (v) => parseFloat(v) || 0,
		format_currency: (v) => '$' + Number(v).toFixed(2),
		frappe: {
			ui: { form: { on: (_doctype, handlers) => { state.handlers = handlers; } } },
			call: (opts) => {
				state.calls.push(opts);
				const reply = responses[opts.method];
				if (reply !== undefined && opts.callback) opts.callback({ message: reply });
			},
			prompt: (fields, callback, title, label) => state.prompts.push({ fields, callback, title, label }),
			confirm: () => {},
			msgprint: (m) => state.msgprints.push(m),
			show_alert: () => {},
			utils: { escape_html: (s) => String(s).replace(/[&<>"']/g, (c) => '&#' + c.charCodeAt(0) + ';') },
			datetime: { str_to_user: (s) => s },
		},
	};
	sandbox.window = { ffl: { switches: { gunbrokerEnabled: () => enabled } } };
	vm.createContext(sandbox);
	vm.runInContext(source, sandbox);
	const frm = {
		doc, is_new: () => false, reload_doc: () => {},
		add_custom_button(label, fn, group) {
			const chain = { removeClass() { return chain; }, addClass() { return chain; } };
			state.buttons.push({ label, fn, group });
			return chain;
		},
	};
	state.handlers.refresh(frm);
	state.frm = frm;
	state.labels = () => state.buttons.map((b) => b.label);
	state.click = (label) => state.buttons.find((b) => b.label === label).fn();
	return state;
}

const MODEL = { name: 'CZ75', is_firearm: 1, uniform_sell_price: 1, gb_item_id: '', gb_delisted: 0 };
const AMMO = { name: 'AMMO-9', is_firearm: 0, uniform_sell_price: 0, gb_item_id: '', gb_delisted: 0 };
const PER_GUN = { name: 'COLT', is_firearm: 1, uniform_sell_price: 0, gb_item_id: '', gb_delisted: 0 };

const OFFER = {
	is_model: true, title: 'CZ 75', sellable: 3, price: 399, regular_price: 499, on_sale: true,
	sale_to: '2026-10-20', category: { id: 3026, name: 'Semi Auto Pistols', ambiguous: false },
	shipping: { own: 0, default: 34.99, free: false, effective: 34.99 },
	premium: { featured: false, showcase: true, sponsored_onsite: false, view_counter: false },
};

function field(prompt, fieldname) { return prompt.fields.find((f) => f.fieldname === fieldname); }
function text(prompt) { return prompt.fields.map((f) => f.options || '').join(' '); }

// ----------------------------------------------------------------- the checks
check('nothing is drawn when GunBroker is switched off', () => {
	eq(boot({ doc: MODEL, enabled: false }).labels(), []);
});

check('a gun priced one by one gets no button here (its guns list from the Serial No form)', () => {
	eq(boot({ doc: PER_GUN }).labels(), []);
});

check('a uniform-price gun that is not listed gets Send, and only Send', () => {
	eq(boot({ doc: MODEL }).labels(), ['Send to GunBroker']);
});

check('a listed uniform-price gun gets the live-listing buttons and no Send', () => {
	const s = boot({ doc: { ...MODEL, gb_item_id: '16087299' } });
	eq(s.labels(), ['Reprice on GunBroker', 'Sync GunBroker Quantity', 'GunBroker Status', 'End GunBroker Listing']);
});

check('ammunition keeps its shipping-only prompt and sends no listing terms', () => {
	const s = boot({ doc: AMMO, responses: { 'ffl_integrations.gunbroker.client_api.listing_ship_cost': { own: 0, default: 19.99 } } });
	eq(s.labels(), ['Send to GunBroker']);
	s.click('Send to GunBroker');
	eq(s.calls[0].method, 'ffl_integrations.gunbroker.client_api.listing_ship_cost');
	eq(s.prompts[0].fields.map((f) => f.fieldname), ['ship_cost']);
	s.prompts[0].callback({ ship_cost: 19.99 });
	const push = s.calls.find((c) => c.method.endsWith('push_item_now'));
	eq(push.args, { item_code: 'AMMO-9', relist: 0 });
});

check('Send on a model asks the server for the offer first', () => {
	const s = boot({ doc: MODEL, responses: { 'ffl_integrations.gunbroker.client_api.item_listing_offer': OFFER } });
	s.click('Send to GunBroker');
	eq(s.calls[0].method, 'ffl_integrations.gunbroker.client_api.item_listing_offer');
	eq(s.calls[0].args, { item_code: 'CZ75' });
	eq(s.prompts.length, 1);
});

check('the dialog shows guns to offer, the price with the promotion flagged, and the category', () => {
	const s = boot({ doc: MODEL, responses: { 'ffl_integrations.gunbroker.client_api.item_listing_offer': OFFER } });
	s.click('Send to GunBroker');
	const html = text(s.prompts[0]);
	assert(/Guns to offer<\/b><\/td><td>3</.test(html), 'sellable count missing: ' + html);
	assert(html.includes('$399.00') && html.includes('on sale') && html.includes('$499.00'), 'sale price not flagged');
	assert(html.includes('3026 Semi Auto Pistols'), 'category missing');
});

check('a price with no promotion is not flagged as a sale', () => {
	const offer = { ...OFFER, on_sale: false, price: 499, sale_to: '' };
	const s = boot({ doc: MODEL, responses: { 'ffl_integrations.gunbroker.client_api.item_listing_offer': offer } });
	s.click('Send to GunBroker');
	assert(!text(s.prompts[0]).includes('on sale'));
});

check('an unreadable count and an ambiguous category are said out loud', () => {
	const offer = { ...OFFER, sellable: null, category: { id: 0, name: '', ambiguous: true } };
	const s = boot({ doc: MODEL, responses: { 'ffl_integrations.gunbroker.client_api.item_listing_offer': offer } });
	s.click('Send to GunBroker');
	const html = text(s.prompts[0]);
	assert(html.includes('could not be worked out'), 'unreadable count hidden');
	assert(html.includes('none configured'), 'missing category hidden');
});

check('the four paid promotions are offered, pre-ticked from the Item, with the GunBroker-charges-extra note', () => {
	const s = boot({ doc: MODEL, responses: { 'ffl_integrations.gunbroker.client_api.item_listing_offer': OFFER } });
	s.click('Send to GunBroker');
	const p = s.prompts[0];
	for (const name of ['premium_featured', 'premium_showcase', 'premium_sponsored_onsite', 'premium_view_counter']) {
		assert(field(p, name) && field(p, name).fieldtype === 'Check', name + ' missing');
	}
	eq([field(p, 'premium_featured').default, field(p, 'premium_showcase').default], [0, 1]);
	const html = text(p);
	assert(html.includes('GunBroker charges extra'), 'cost note missing');
	assert(html.includes('only be chosen now, when the listing is created'), 'only-at-creation note missing');
	assert(html.includes('never buys them'), 'machine-relist note missing');
});

check('Send posts a fixed-price listing with all four promotion flags, so unticking clears', () => {
	const s = boot({ doc: MODEL, responses: { 'ffl_integrations.gunbroker.client_api.item_listing_offer': OFFER } });
	s.click('Send to GunBroker');
	s.prompts[0].callback({ ship_cost: 34.99, premium_featured: 1, premium_showcase: 0,
		premium_sponsored_onsite: 0, premium_view_counter: 1 });
	const push = s.calls.find((c) => c.method.endsWith('push_item_now'));
	eq(push.args.item_code, 'CZ75');
	eq(JSON.parse(push.args.listing), { type: 'Fixed Price', premium: {
		featured: 1, showcase: 0, sponsored_onsite: 0, view_counter: 1 } });
	assert(!('ship_cost' in push.args), 'the store default must not be saved as the Item\'s own rate');
});

check('a different shipping rate is sent so it is saved on the Item', () => {
	const s = boot({ doc: MODEL, responses: { 'ffl_integrations.gunbroker.client_api.item_listing_offer': OFFER } });
	s.click('Send to GunBroker');
	s.prompts[0].callback({ ship_cost: 12.5 });
	eq(s.calls.find((c) => c.method.endsWith('push_item_now')).args.ship_cost, 12.5);
});

check('a model taken down by hand is re-listed on purpose (relist=1)', () => {
	const s = boot({ doc: { ...MODEL, gb_delisted: 1 }, responses: { 'ffl_integrations.gunbroker.client_api.item_listing_offer': OFFER } });
	s.click('Send to GunBroker');
	s.prompts[0].callback({ ship_cost: 34.99 });
	eq(s.calls.find((c) => c.method.endsWith('push_item_now')).args.relist, 1);
});

console.log(JSON.stringify({ checks }, null, 1));
process.exit(checks.every((c) => c.ok) ? 0 : 1);
