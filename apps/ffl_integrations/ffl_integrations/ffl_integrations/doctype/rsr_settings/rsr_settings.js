// RSR Settings form. One button, read-only against RSR: Test Connection
// proves the catalog service and the Direct Connect credentials.

frappe.ui.form.on('RSR Settings', {
	refresh(frm) {
		const $test = frm.add_custom_button(__('Test Connection'), function () {
			frappe.call({
				method: 'ffl_integrations.ffl_integrations.doctype.rsr_settings.rsr_settings.test_connection',
				freeze: true,
				freeze_message: __('Contacting RSR…'),
				callback(resp) {
					const checks = (resp.message || {}).checks || [];
					const rows = checks.map((c) => {
						const mark = c.ok === true ? '✅' : c.ok === false ? '❌' : '➖';
						return `<tr><td>${mark}</td><td>${frappe.utils.escape_html(c.label)}</td>` +
							`<td>${frappe.utils.escape_html(c.detail || '')}</td></tr>`;
					}).join('');
					frappe.msgprint({
						title: __('RSR connection'),
						message: `<table class="table table-sm"><tbody>${rows}</tbody></table>`,
					});
				},
			});
		});
		$test.removeClass('btn-default').addClass('btn-primary');
	},
});
