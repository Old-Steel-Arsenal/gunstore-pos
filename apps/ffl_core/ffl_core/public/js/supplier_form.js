// Supplier-form FFL helpers.
// - "Search ATF" opens a dialog that searches the local ATF FFL Record
//   cache (populated by the monthly CSV loader) and fills supplier fields
//   from the selected row.
// - "Verify FFL" lands in Cut 6 (live ATF EZ Check). The button is added
//   here as a stub so its placement is consistent.

frappe.ui.form.on('Supplier', {
	refresh(frm) {
		if (!frm.doc.is_ffl_dealer) return;
		frm.add_custom_button(__('Search ATF'), () => openSearchDialog(frm), __('FFL'));
		if (!frm.is_new()) {
			frm.add_custom_button(__('Verify FFL'), () => verifyLive(frm), __('FFL'));
		}
	},
});

function openSearchDialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __('Search ATF FFL'),
		size: 'large',
		fields: [
			{label: __('FFL # or licensee name'), fieldname: 'q', fieldtype: 'Data', reqd: 1},
			{fieldtype: 'HTML', fieldname: 'results'},
		],
		primary_action_label: __('Search'),
		primary_action(values) {
			frappe.call({
				method: 'ffl_integrations.atf.search.search',
				args: {query: values.q, limit: 20},
				callback(r) { renderResults(dialog, frm, r.message || []); },
			});
		},
	});
	dialog.show();
}

function renderResults(dialog, frm, rows) {
	const $area = dialog.fields_dict.results.$wrapper;
	// National FFL search off (frappe.boot.ffl_capabilities): only this store's
	// FFL records were searched — the shared notice says so, above any rows.
	const notice = (window.ffl && ffl.capabilities && ffl.capabilities.isOff('ffl_national_search'))
		? ffl.capabilities.notice('ffl_national_search') : '';
	if (!rows.length) {
		$area.html(notice || `<p style="color:var(--text-muted)">${__('No matches.')}</p>`);
		return;
	}
	const html = `
		<table class="table table-sm" style="margin-top:8px">
			<thead><tr>
				<th>${__('FFL #')}</th><th>${__('Licensee')}</th>
				<th>${__('City, State')}</th><th>${__('Expiry')}</th><th></th>
			</tr></thead>
			<tbody>
				${rows.map((r, i) => `
					<tr>
						<td>${frappe.utils.escape_html(r.ffl_number)}</td>
						<td>${frappe.utils.escape_html(r.licensee_name || '')}<br>
							<span style="color:var(--text-muted)">${frappe.utils.escape_html(r.licensee_dba || '')}</span></td>
						<td>${frappe.utils.escape_html((r.city || '') + ', ' + (r.state || ''))}</td>
						<td>${r.expiry_date || ''}</td>
						<td><button class="btn btn-xs btn-primary" data-idx="${i}">${__('Pick')}</button></td>
					</tr>`).join('')}
			</tbody>
		</table>`;
	$area.html(notice + html);
	$area.find('button[data-idx]').on('click', (e) => {
		const idx = parseInt(e.currentTarget.dataset.idx, 10);
		applyRow(frm, rows[idx]);
		dialog.hide();
	});
}

function applyRow(frm, r) {
	frm.set_value('ffl_number', r.ffl_number);
	// ffl_license_type is a Select of 2-digit codes; the cache stores
	// the full label ("07 - Manufacturer of ..."), strip to the prefix.
	const code = (window.gunstore && window.gunstore.extractLicenseCode)
		? window.gunstore.extractLicenseCode(r.license_type)
		: (r.license_type || '').match(/^\s*(\d{2})/)?.[1] || '';
	frm.set_value('ffl_license_type', code);
	frm.set_value('licensee_name', r.licensee_name || '');
	frm.set_value('licensee_address', [r.street, r.street2, r.city, r.state, r.zip]
		.filter(Boolean).join(', '));
	if (r.expiry_date) frm.set_value('ffl_expiry_date', r.expiry_date);
	// Local cache fill is NOT a live verification — user must click 'Verify FFL'
	// to hit the live ATF EZ Check (Cut 6). Leave status Pending until then.
	frm.set_value('ez_check_status', 'Pending');
	// Don't update ez_check_last_run — that field tracks live ATF calls, not local cache fills.
	frappe.show_alert({
		message: __('Filled from ATF FFL Record. Click Verify FFL to confirm against the live ATF.'),
		indicator: 'orange',
	});
}

function verifyLive(frm) {
	frappe.call({
		method: 'ffl_integrations.atf.ez_check_api.verify_supplier_ffl',
		args: {supplier_name: frm.doc.name},
		freeze: true,
		freeze_message: __('Calling ATF...'),
		callback(r) {
			if (!r.message) return;
			const indicator = r.message.status === 'Verified' ? 'green'
				: r.message.status === 'Expired' ? 'orange' : 'red';
			frappe.show_alert({
				message: __('FFL status: {0}', [r.message.status]),
				indicator,
			});
			frm.reload_doc();
		},
	});
}

// Picking the formal directory link fills the license fields immediately
// (the server validate re-syncs authoritatively on save).
frappe.ui.form.on('Supplier', {
	atf_ffl_record(frm) {
		if (!frm.doc.atf_ffl_record) return;
		frappe.db.get_doc('ATF FFL Record', frm.doc.atf_ffl_record).then((rec) => {
			frm.set_value('ffl_number', rec.ffl_number);
			const code = (rec.license_type || '').match(/^\s*(\d{2})/)?.[1] || '';
			if (code) frm.set_value('ffl_license_type', code);
			if (rec.licensee_name) frm.set_value('licensee_name', rec.licensee_name);
			frm.set_value('licensee_address', [rec.street, rec.street2, rec.city,
				[rec.state, rec.zip].filter(Boolean).join(' ')].filter(Boolean).join(', '));
			if (rec.expiry_date) frm.set_value('ffl_expiry_date', rec.expiry_date);
		});
	},
});
