// POS "Charge exactly": type the total the customer pays (tax included) and the
// cart's line rates are worked back from it, pre-tax, in proportion to what each
// line costs now — the register-side twin of Create Order's box.
//
// Upgrade-safe, the pos_recycle_bin.js way: ERPNext's ItemCart is wrapped on its
// prototype (ffl.installClassHook), never edited; the stock totals markup is
// untouched and one row is appended before Checkout. Rates are written exactly
// as the POS rate field writes them (frappe.model.set_value on the row's `rate`,
// then cur_pos.update_cart_html), so ERPNext's own discount/tax recalculation runs
// and nothing here depends on how it taxes. The arithmetic (plan / nudge /
// shownTotal) is pure and Node-tested; the DOM wiring is browser-only.
(function (root, factory) {
	var api = factory();
	if (typeof module === 'object' && module.exports) {
		module.exports = api;                       // Node test
	} else {
		root.ffl = root.ffl || {};
		root.ffl.posChargeExactly = api;
	}
})(typeof self !== 'undefined' ? self : this, function () {
	'use strict';

	function flt(v) { var n = parseFloat(v); return isNaN(n) ? 0 : n; }
	function cents(v) { return Math.round(flt(v) * 100) / 100; }

	// The total the cart shows — ERPNext prefers rounded_total unless the site
	// disables rounding (pos_item_cart.update_totals_section).
	function shownTotal(doc, disableRounded) {
		return flt(disableRounded ? doc.grand_total : (doc.rounded_total || doc.grand_total));
	}

	// grand / net — what one pre-tax dollar becomes on the receipt right now
	// (tax, and any charge that scales with the net). 1 on an empty or untaxed cart.
	function taxFactor(doc, shown) {
		var net = flt(doc.net_total);
		return net > 0 && shown > 0 ? shown / net : 1;
	}

	// First pass: the pre-tax net that lands on `target`, spread over the lines by
	// their current amounts (evenly when every line is $0 — a $0 line beside priced
	// ones stays $0, e.g. the gun on a transfer ticket). A NEGATIVE line is a
	// credit the customer is owed (trade-in credit, rate = -value): it is never
	// touched — its amount comes off the net the priced lines must make up.
	// Rates are per unit, cents; the last priced line absorbs the spread's
	// rounding. Returns {rates: {rowname: rate}}.
	// A line whose amount is fixed: a credit the customer is owed (negative), or a
	// LOCKED line — a trade-in credit, ffl_locked_rate set, which the server
	// re-pins on every save (compliance/locked_lines).
	function isFixed(r) { return flt(r.rate) < 0 || flt(r.ffl_locked_rate) !== 0; }

	function plan(items, target, doc, disableRounded) {
		var all = (items || []).filter(function (r) { return r && r.name && flt(r.qty) > 0; });
		var rows = all.filter(function (r) { return !isFixed(r); });
		if (!rows.length || !(flt(target) > 0)) return { rates: {} };
		var factor = taxFactor(doc, shownTotal(doc, disableRounded));
		var fixed = all.filter(isFixed)
			.reduce(function (s, r) { return s + flt(r.qty) * flt(r.rate); }, 0);
		var net = cents(flt(target) / factor - fixed);
		if (net <= 0) return { rates: {} };
		var cur = rows.map(function (r) { return flt(r.qty) * flt(r.rate); });
		var sum = cur.reduce(function (s, v) { return s + v; }, 0);
		var left = net, rates = {};
		rows.forEach(function (r, i) {
			var last = i === rows.length - 1;
			var share = last ? left : cents(sum > 0 ? net * cur[i] / sum : net / rows.length);
			var rate = Math.max(0, cents(share / flt(r.qty)));
			rates[r.name] = rate;
			left = cents(left - rate * flt(r.qty));
		});
		return { rates: rates, net: net };
	}

	// Later passes: ERPNext has recalculated; move the priced line with the most
	// on it by the remaining difference (pre-tax, per unit). Returns {} when the
	// cart already shows the target or the miss is not a rounding-sized one.
	function nudge(items, target, doc, disableRounded) {
		var shown = shownTotal(doc, disableRounded);
		var diff = cents(flt(target) - shown);
		if (Math.abs(diff) < 0.005 || Math.abs(diff) > 5) return { rates: {} };
		var rows = (items || []).filter(function (r) { return r && r.name && flt(r.qty) > 0 && flt(r.rate) > 0 && !isFixed(r); });
		if (!rows.length) return { rates: {} };
		var row = rows.reduce(function (a, b) { return flt(b.qty) * flt(b.rate) > flt(a.qty) * flt(a.rate) ? b : a; });
		var step = cents(diff / taxFactor(doc, shown) / flt(row.qty));
		if (!step) step = diff > 0 ? 0.01 : -0.01;
		var rates = {};
		rates[row.name] = Math.max(0, cents(flt(row.rate) + step));
		return { rates: rates };
	}

	// ------------------------------------------------------------- browser wiring

	function apply(pos, target, disableRounded) {
		var frm = pos && pos.frm;
		if (!frm || !frm.doc) return Promise.resolve();
		var items = (frm.doc.items || []).filter(function (r) { return r.item_code; });
		if (!items.length) return Promise.resolve();
		var passes = 0;
		function step() {
			var p = passes++ === 0 ? plan(items, target, frm.doc, disableRounded)
				: nudge(items, target, frm.doc, disableRounded);
			var names = Object.keys(p.rates);
			if (!names.length || passes > 4) return Promise.resolve();
			return names.reduce(function (chain, name) {
				return chain.then(function () {
					var row = items.filter(function (r) { return r.name === name; })[0];
					if (!row || flt(row.rate) === p.rates[name]) return;
					return frappe.model.set_value(row.doctype, row.name, 'rate', p.rates[name]);
				});
			}, Promise.resolve()).then(step);
		}
		return step().then(function () {
			items.forEach(function (r) { pos.update_cart_html(r); });
			return shownTotal(frm.doc, disableRounded);
		});
	}

	function patch(ItemCart) {
		var proto = ItemCart && ItemCart.prototype;
		if (!proto || proto.__fflChargeExactly) return;
		proto.__fflChargeExactly = true;
		var original = proto.make_cart_totals_section;
		proto.make_cart_totals_section = function () {
			var out = original.apply(this, arguments);
			try { mount(this); } catch (e) { /* never break the cart */ }
			return out;
		};
		// The row follows Checkout: once the cashier is on the payment screen the
		// tender rows are keyed to the total, so prices must not move underneath.
		var toggle = proto.toggle_checkout_btn;
		if (typeof toggle === 'function') {
			proto.toggle_checkout_btn = function (show) {
				var out = toggle.apply(this, arguments);
				try { this.$totals_section.find('.ffl-charge-exactly').toggle(!!show); } catch (e) { /* ignore */ }
				return out;
			};
		}
	}

	function mount(cart) {
		var $checkout = cart.$totals_section && cart.$totals_section.find('.checkout-btn').first();
		if (!$checkout || !$checkout.length || cart.$totals_section.find('.ffl-charge-exactly').length) return;
		var $row = $(
			'<div class="ffl-charge-exactly" style="display:flex; align-items:center; justify-content:space-between; gap:8px; padding:4px 0 8px">'
			+ '<div>' + __('Charge exactly') + '</div>'
			+ '<input type="number" class="form-control input-xs ffl-charge-exactly-input" step="0.01" min="0" '
			+ 'style="width:120px; text-align:right" placeholder="' + __('total incl. tax') + '" '
			+ 'title="' + __('Type the total the customer pays; line prices are worked back from it before tax.') + '">'
			+ '</div>');
		$checkout.before($row);
		var $input = $row.find('input');
		$input.on('change', function () {
			var target = flt($input.val());
			$input.val('');
			var pos = window.cur_pos;
			if (!pos || !(target > 0)) return;
			if (pos.settings && pos.settings.allow_rate_change === 0) {
				frappe.show_alert({ message: __('This POS Profile does not allow rate changes.'), indicator: 'orange' });
				return;
			}
			if (pos.frm && pos.frm.doc && pos.frm.doc.ffl_counter_order) {
				// A counter pickup's 4473 ticket carries no money — the sale is on the
				// order it hands over. Pricing it would charge the customer twice.
				frappe.show_alert({ message: __('This is a pickup ticket for {0} — the money is on that order.',
					[pos.frm.doc.ffl_counter_order]), indicator: 'orange' });
				return;
			}
			var currency = pos.frm && pos.frm.doc && pos.frm.doc.currency;
			frappe.dom.freeze();
			apply(pos, target, cint(frappe.sys_defaults.disable_rounded_total))
				.then(function (landed) {
					if (landed != null && Math.abs(flt(landed) - target) >= 0.005) {
						frappe.show_alert({ message: __('Landed on {0}, not {1} — adjust a line by hand.',
							[format_currency(landed, currency), format_currency(target, currency)]), indicator: 'orange' });
					}
				})
				.catch(function (e) { console.error(e); })
				.then(function () { frappe.dom.unfreeze(); });
		});
	}

	function wire() {
		if (typeof window === 'undefined' || !window.frappe || typeof frappe.provide !== 'function') return;
		frappe.provide('erpnext.PointOfSale');
		var hook = window.ffl && ffl.installClassHook;
		if (hook) hook(erpnext.PointOfSale, 'ItemCart', patch);
	}
	if (typeof document !== 'undefined') {
		if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
		else wire();
	}

	return { plan: plan, nudge: nudge, shownTotal: shownTotal, taxFactor: taxFactor, patch: patch };
});
