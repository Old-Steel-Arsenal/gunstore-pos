// ShipStation Settings client script.
//
// One action button: "Test Connection" — sanity-check the V2 API key by
// listing carriers. Runs the whitelisted Python entry-point and only surfaces
// what the server returns. No credentials touch the client.

frappe.ui.form.on('ShipStation Settings', {
	refresh(frm) {
		frm.add_custom_button(__('Test Connection'), () => testConnection(frm), __('ShipStation'));
	},
});

function testConnection(frm) {
	frappe.call({
		method: 'ffl_integrations.shipstation.api.test_connection',
		freeze: true,
		freeze_message: __('Calling ShipStation...'),
		callback(r) {
			if (r.message && r.message.ok) {
				frappe.show_alert({
					message: r.message.sandbox
						? __('ShipStation connection OK (sandbox) — {0} carrier(s).', [r.message.carrier_count])
						: __('ShipStation connection OK — {0} carrier(s).', [r.message.carrier_count]),
					indicator: 'green',
				});
				const whs = r.message.warehouses || [];
				if (whs.length && !frm.doc.ship_from_warehouse_id) {
					// create-shipment requires a ship-from — hand the ids over.
					const rows = whs.map((w) => `<tr><td><code>${frappe.utils.escape_html(w.warehouse_id || '')}</code></td>`
						+ `<td>${frappe.utils.escape_html(w.name || '')}${w.is_default ? ' <b>(default)</b>' : ''}</td></tr>`).join('');
					frappe.msgprint({
						title: __('Pick a Ship-From Warehouse'),
						message: `<p>${__('Ship-From Warehouse ID is empty. Copy one of these into the field and Save:')}</p>`
							+ `<table class="table table-bordered">${rows}</table>`,
					});
				}
			}
		},
	});
}
