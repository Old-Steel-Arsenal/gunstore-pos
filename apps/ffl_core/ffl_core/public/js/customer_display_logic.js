// Pure, dependency-free logic for the POS customer display. Shared by the
// sender (POS desk window, via app_include_js) and the standalone display page
// (popup window, via <script src>), and unit-tested under Node. NO DOM, NO
// Frappe, NO top-level browser-global access — so the same file runs in all
// three contexts. We read only stable POS Invoice docfields here, never POS
// controller internals, so ERPNext upgrades that rename methods can't break us.
(function (factory) {
	'use strict';
	var api = factory();
	if (typeof module !== 'undefined' && module.exports) module.exports = api;   // Node test
	if (typeof window !== 'undefined') window.FFLCustomerDisplay = api;           // desk + popup
})(function () {
	'use strict';

	function flt(v) { var n = parseFloat(v); return isNaN(n) ? 0 : n; }

	// USD for a customer-facing screen. Standalone so the popup needs no Frappe.
	function formatMoney(value) {
		var n = flt(value);
		var neg = n < 0;
		var s = Math.abs(n).toFixed(2).split('.');
		s[0] = s[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
		return (neg ? '-$' : '$') + s[0] + '.' + s[1];
	}

	// cur_pos.frm.doc -> normalized snapshot. Only stable docfields.
	function buildSnapshot(doc) {
		doc = doc || {};
		var items = (doc.items || []).map(function (it) {
			return {
				name: it.item_name || it.item_code || '',
				qty: flt(it.qty),
				uom: it.uom || it.stock_uom || '',
				rate: flt(it.rate),
				amount: flt(it.amount),
			};
		});
		return {
			items: items,
			totals: {
				subtotal: flt(doc.net_total),
				discount: flt(doc.discount_amount),
				tax: flt(doc.total_taxes_and_charges),
				grand_total: flt(doc.grand_total),
			},
			payment: {
				due: flt(doc.grand_total),
				paid: flt(doc.paid_amount),
				change: flt(doc.change_amount),
			},
		};
	}

	// Decide the screen state. prevStage is the last stage the sender emitted.
	// A payment->empty transition is a completed sale; we STAY in 'thankyou'
	// while the cart stays empty. The receiver owns the brief thank-you ->
	// welcome timeout, so the sender must NOT flip to 'welcome' itself or it
	// would cut the thank-you short (the next poll would re-broadcast). A bare
	// cart clear that never paid goes straight to welcome.
	//
	// "payment" is gated on paid_amount > 0. In the default POS config paid_amount
	// is only seeded (to the grand total) at checkout, so this reliably means the
	// cashier reached the payment screen. A flow that sets a partial paid_amount
	// while the cart screen is still up (e.g. loyalty redemption) would show the
	// pay-block early — revisit this gate if such a flow is added.
	function deriveStage(prevStage, snapshot) {
		if (snapshot.items.length > 0) {
			return snapshot.payment.paid > 0 ? 'payment' : 'cart';
		}
		if (prevStage === 'payment' || prevStage === 'thankyou') return 'thankyou';
		return 'welcome';
	}

	return { buildSnapshot: buildSnapshot, deriveStage: deriveStage, formatMoney: formatMoney };
});
