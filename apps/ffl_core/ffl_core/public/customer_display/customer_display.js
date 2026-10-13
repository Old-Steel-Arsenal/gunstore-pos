// Customer display receiver. Standalone: subscribes to the BroadcastChannel and
// renders, reusing ERPNext POS ItemCart markup/classes. The POS window sends
// branding + the live desk.bundle.css href + the current theme, so this popup
// matches the desk's fonts/colours/light-dark theme. Money formatting comes from
// customer_display_logic.js (loaded just before this script).
(function () {
	'use strict';

	var CHANNEL = 'ffl-pos-customer-display';
	var THANKYOU_MS = 6000;
	var fmt = (window.FFLCustomerDisplay && window.FFLCustomerDisplay.formatMoney)
		|| function (v) { return '$' + (Number(v) || 0).toFixed(2); };

	var el = function (id) { return document.getElementById(id); };
	var screens = { welcome: el('screen-welcome'), cart: el('screen-cart'), thankyou: el('screen-thankyou') };
	var branding = { name: '', logo: '' };
	var thankyouTimer = null;

	function esc(s) {
		return String(s == null ? '' : s)
			.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
			.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
	}

	function show(name) {
		clearTimeout(thankyouTimer);
		Object.keys(screens).forEach(function (k) { screens[k].hidden = (k !== name); });
		if (name === 'thankyou') thankyouTimer = setTimeout(function () { show('welcome'); }, THANKYOU_MS);
	}

	// Pull in the desk stylesheet (hash-correct href from the POS window) so the
	// design tokens / fonts / theme match the desk. Appended after our own CSS so
	// the desk :root (including dark-theme overrides) wins.
	function injectDeskCss(href) {
		if (!href || document.getElementById('ffl-cd-desk-css')) return;
		var link = document.createElement('link');
		link.id = 'ffl-cd-desk-css';
		link.rel = 'stylesheet';
		link.href = href;
		document.head.appendChild(link);
	}

	// A customer-facing screen should be clean white, NOT follow the cashier's
	// dark desk theme. Force light regardless of the desk's data-theme; we still
	// load desk.bundle.css below, but only its light tokens apply.
	function applyTheme() {
		document.documentElement.setAttribute('data-theme', 'light');
		document.documentElement.setAttribute('data-theme-mode', 'light');
	}

	function applyBranding() {
		var logo = el('brand-logo');
		if (branding.logo) { logo.src = branding.logo; logo.hidden = false; }
		else { logo.hidden = true; }
		el('brand-name').textContent = branding.name || '';
		el('cart-label').textContent = branding.name || 'Item Cart';
	}

	function abbr(name) {
		var parts = String(name || '').split(/\s+/).filter(Boolean);
		var a = (parts[0] || '').charAt(0) + (parts.length > 1 ? parts[1].charAt(0) : '');
		return (a || '?').toUpperCase();
	}

	function itemRow(it) {
		var qtyText = it.qty + (it.uom ? (' ' + it.uom) : '');
		var unit = (it.qty && it.qty !== 1) ? ('@ ' + fmt(it.rate)) : '';
		return '<div class="cart-item-wrapper">'
			+ '<div class="item-image item-abbr">' + esc(abbr(it.name)) + '</div>'
			+ '<div class="item-name-desc"><div class="item-name">' + esc(it.name) + '</div></div>'
			+ '<div class="item-qty-rate">'
			+ '<div class="item-qty"><span>' + esc(qtyText) + '</span></div>'
			+ '<div class="item-rate-amount">'
			+ '<div class="item-rate">' + esc(fmt(it.amount)) + '</div>'
			+ (unit ? '<div class="item-amount">' + esc(unit) + '</div>' : '')
			+ '</div></div></div>';
	}

	function toggleRow(containerId, valId, value, visible) {
		el(containerId).hidden = !visible;
		if (visible) el(valId).textContent = fmt(value);
	}

	function renderCart(snap) {
		el('lines-body').innerHTML = snap.items.map(itemRow).join('<div class="seperator"></div>');

		var t = snap.totals;
		el('t-subtotal').textContent = fmt(t.subtotal);
		toggleRow('discount-container', 't-discount', t.discount, t.discount !== 0);
		toggleRow('tax-container', 't-tax', t.tax, t.tax !== 0);
		el('t-grand').textContent = fmt(t.grand_total);

		var paying = snap.stage === 'payment';
		el('pay-block').hidden = !paying;
		if (paying) {
			el('p-due').textContent = fmt(snap.payment.due);
			el('p-paid').textContent = fmt(snap.payment.paid);
			el('p-change').textContent = fmt(snap.payment.change);
		}
		show('cart');
	}

	function render(snap) {
		if (snap.stage === 'cart' || snap.stage === 'payment') renderCart(snap);
		else if (snap.stage === 'thankyou') show('thankyou');
		else show('welcome');
	}

	var bc = new BroadcastChannel(CHANNEL);
	bc.onmessage = function (e) {
		var msg = e.data || {};
		if (msg.type === 'branding') {
			branding = { name: msg.name || '', logo: msg.logo || '' };
			applyTheme();
			injectDeskCss(msg.deskCss);
			applyBranding();
		} else if (msg.type === 'snapshot') {
			render(msg);
		}
	};
	window.addEventListener('pagehide', function () { bc.close(); });

	// Initial paint, then ask any already-running sender to replay state.
	applyBranding();
	show('welcome');
	bc.postMessage({ type: 'ready' });
})();
