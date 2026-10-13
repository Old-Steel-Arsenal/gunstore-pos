// Test Connection for the WooCommerce Settings single.
//
// One whitelisted probe (GET /products?per_page=1, read auth only), one button
// on the toolbar: it is the thing to press after every change on this page.
// Nothing here can write to a store.
const WOO_SETTINGS_SITES = {
	'WooCommerce Settings': 'retail',
};

Object.entries(WOO_SETTINGS_SITES).forEach(([doctype, site]) => {
	frappe.ui.form.on(doctype, {
		refresh(frm) {
			const $test = frm.add_custom_button(__('Test Connection'), () => {
				frappe.call({
					method: 'ffl_woo_sync.woocommerce.client_api.test_connection',
					args: { site },
					freeze: true,
					freeze_message: __('Contacting WooCommerce…'),
					callback(r) {
						const m = r.message || {};
						if (!m.ok) return;
						frappe.show_alert({
							message: __('WooCommerce connection OK: {0}', [m.store_url || frm.doc.store_url]),
							indicator: 'green',
						});
					},
				});
			});
			$test.removeClass('btn-default').addClass('btn-primary');
		},
	});
});
