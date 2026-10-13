// FFL Acquisition — populate two dropdowns dynamically:
//   • Acquisition Type ← FastBound AcquireType SmartList (presets + custom),
//     cached on FastBound Settings; refresh via "Sync FastBound Lists".
//   • Receive Source   ← FFL Settings → Receive Sources (admin-edited list).
//
// Both are soft-coupled — calls fail silently if the back-end is unavailable.
frappe.ui.form.on('FFL Acquisition', {
	refresh(frm) {
		// Acquisition Type
		frappe.call({
			method: 'ffl_integrations.fastbound.client_api.get_acquire_types',
			callback(r) {
				const types = (r.message || []).slice();
				const cur = frm.doc.transaction_type;
				if (!types.length && !cur) return;  // not synced yet — leave field blank
				if (cur && !types.includes(cur)) types.unshift(cur);
				frm.set_df_property('transaction_type', 'options', ['', ...types].join('\n'));
				frm.refresh_field('transaction_type');
			},
			error() { /* FastBound integration unavailable */ },
		});
		// Receive Source
		frappe.call({
			method: 'ffl_core.api.receive_goods.get_receive_sources',
			callback(r) {
				const list = (r.message || []).slice();
				const cur = frm.doc.receive_source;
				if (!list.length && !cur) return;
				if (cur && !list.includes(cur)) list.unshift(cur);
				frm.set_df_property('receive_source', 'options', ['', ...list].join('\n'));
				frm.refresh_field('receive_source');
			},
		});
	},
});
