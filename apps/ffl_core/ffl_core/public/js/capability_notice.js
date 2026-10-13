// ffl.capabilities — what this site can do without the Data Service, and the ONE
// "not set up" notice every surface shows when a capability is off (docs/data-service.md).
//
// The server decides (ffl_integrations.data_service.capabilities) and ships the answer
// as frappe.boot.ffl_capabilities = {catalog, category_tree, ffl_national_search,
// extension_updates, ai}; each value is a source name or 'off'. A page that refreshes
// calls ffl.capabilities.refresh(). An unknown key (ffl_integrations not installed, an
// old boot) is never 'off': the notice only shows when the server said so.
//
//   if (ffl.capabilities.isOff('catalog')) $area.html(ffl.capabilities.notice('catalog'));
//
// Pure kernel (state / isOff / noticeHtml) runs under Node (tests/js/capability_notice.test.js).
// The browser part also turns a CapabilityOff answer from any endpoint (HTTP 417) into the
// same notice, as an alert, instead of a red error dialog.
(function () {
	'use strict';

	const OFF = 'off';
	const SETUP_ROUTE = '/app/data-service-settings';
	const NOTICES = {
		catalog: 'Distributor catalog is not set up',
		category_tree: 'The category tree is not set up',
		ffl_national_search: "National FFL search is not set up — showing this store's FFL records only",
		extension_updates: 'Update service not configured',
		ai: 'The AI assistant is not connected',
	};

	function state(boot, name) {
		const caps = boot && boot.ffl_capabilities;
		if (!caps || typeof caps !== 'object' || !(name in caps)) return null;
		return caps[name];
	}

	function isOff(boot, name) {
		return state(boot, name) === OFF;
	}

	function escape(s) {
		return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
			{ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
	}

	// opts: translate (default: identity), canSetUp (the link; else "ask a manager"),
	// inline (a dropdown row).
	function noticeHtml(name, opts) {
		const o = opts || {};
		const t = typeof o.translate === 'function' ? o.translate : (s) => s;
		const text = t(NOTICES[name] || 'This feature is not set up');
		const action = o.canSetUp === false
			? `<span class="ffl-cap-ask">${escape(t('Ask a manager to set it up'))}</span>`
			: `<a class="ffl-cap-link" href="${SETUP_ROUTE}">${escape(t('Set it up'))}</a>`;
		const cls = o.inline ? 'ffl-cap-notice ffl-cap-inline' : 'ffl-cap-notice';
		return `<div class="${cls}" role="status" data-capability="${escape(name)}">`
			+ `<span class="ffl-cap-text">${escape(text)}</span> · ${action}</div>`;
	}

	const CSS = `
		.ffl-cap-notice { padding:8px 12px; border-radius:6px; font-size:12px; line-height:1.4;
			background:var(--bg-orange, #fff7ed); color:var(--text-on-orange, #9a3412); }
		.ffl-cap-notice a.ffl-cap-link { color:inherit; text-decoration:underline; font-weight:600; }
		.ffl-cap-notice.ffl-cap-inline { border-radius:0; text-align:center; }
	`;

	// A CapabilityOff answer (HTTP 417): which capability it names, and every server
	// message OTHER than the capability's own sentence (a registered error handler
	// makes frappe skip showing the messages, so the handler shows these itself).
	// Both shapes: v1 `_server_messages` (a JSON list of JSON strings) and v2
	// `messages` (a list of objects).
	function serverMessages(r) {
		let v1 = [];
		try { v1 = JSON.parse((r && r._server_messages) || '[]'); } catch (e) { v1 = []; }
		const v2 = r && r.messages;
		return [].concat(Array.isArray(v1) ? v1 : [], Array.isArray(v2) ? v2 : []);
	}

	function splitCapabilityOff(r) {
		const msgs = serverMessages(r);
		const drop = r && r.capability_off_message;
		const others = msgs.filter((m) => {
			let o = m;
			try { if (typeof m === 'string') o = JSON.parse(m); } catch (e) { o = m; }
			return !(drop && o && o.message === drop);
		});
		const name = r && r.capability_off in NOTICES ? r.capability_off : 'catalog';
		return { name, others };
	}

	// The handler: the notice every time (the previous notice alert is replaced, not
	// stacked or hidden), the other messages as frappe would have shown them. It
	// returns nothing and touches no request option, so frappe.call's own error
	// callback still runs and the caller can restore its UI. Frappe hands a global
	// error handler the answer only, not the request's options, so a silent call shows
	// the notice too (no wrapping of frappe internals to find out: upgrade-safe rule).
	function makeCapabilityOffHandler(deps) {
		let last = null;
		return function (r) {
			const { name, others } = splitCapabilityOff(r);
			if (others.length) deps.msgprint(others);
			if (last && typeof last.remove === 'function') last.remove();
			last = deps.showAlert(deps.notice(name));
		};
	}

	const api = { OFF, SETUP_ROUTE, NOTICES, state, isOff, noticeHtml, CSS, serverMessages,
		splitCapabilityOff, makeCapabilityOffHandler };
	if (typeof module !== 'undefined' && module.exports) module.exports = api;   // Node test
	if (typeof window === 'undefined') return;

	window.ffl = window.ffl || {};
	const boot = () => (window.frappe && frappe.boot) || {};
	const translate = (s) => (typeof window.__ === 'function' ? window.__(s) : s);
	const canSetUp = () => !!(window.frappe && frappe.user && frappe.user.has_role
		&& frappe.user.has_role('System Manager'));
	function injectCss() {
		if (typeof document === 'undefined' || document.getElementById('ffl-cap-css')) return;
		const style = document.createElement('style');
		style.id = 'ffl-cap-css';
		style.textContent = CSS;
		document.head.appendChild(style);
	}
	function notice(name, opts) {
		injectCss();
		return noticeHtml(name, Object.assign({ translate, canSetUp: canSetUp() }, opts || {}));
	}
	window.ffl.capabilities = Object.assign({}, api, {
		state: (name) => state(boot(), name),
		isOff: (name) => isOff(boot(), name),
		notice,
		// Re-read after Data Service Settings may have changed (pages that refresh).
		refresh() {
			return frappe.xcall('ffl_integrations.data_service.get_capabilities').then((caps) => {
				if (caps && frappe.boot) frappe.boot.ffl_capabilities = caps;
				return caps;
			}, () => null);
		},
	});

	// A CapabilityOff answer (HTTP 417) shows the notice, not a red error dialog: the
	// capability the endpoint named (``capability_off`` in the answer), else the
	// catalog (the quiet guard is the catalog selector).
	if (window.frappe && frappe.request && frappe.request.error_handlers) {
		const handlers = frappe.request.error_handlers;
		(handlers.CapabilityOff = handlers.CapabilityOff || []).push(makeCapabilityOffHandler({
			notice: (name) => notice(name),
			msgprint: (msgs) => frappe.msgprint(msgs),
			showAlert: (html) => frappe.show_alert({ message: html, indicator: 'orange' }, 10),
		}));
	}
})();
