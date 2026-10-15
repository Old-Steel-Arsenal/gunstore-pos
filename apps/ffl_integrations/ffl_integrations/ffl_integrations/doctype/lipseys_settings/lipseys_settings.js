// Lipseys Settings form. One button, read-only against Lipsey's: Test
// Connection proves the catalog service and each filled-in login (sign in plus
// one small read) before anyone enables ordering.

frappe.ui.form.on('Lipseys Settings', {
	refresh(frm) {
		const $test = frm.add_custom_button(__('Test Connection'), function () {
			frappe.call({
				method: 'ffl_integrations.ffl_integrations.doctype.lipseys_settings.lipseys_settings.test_connection',
				freeze: true,
				freeze_message: __("Contacting Lipsey's…"),
				callback(resp) {
					const checks = (resp.message || {}).checks || [];
					const rows = checks.map((c) => {
						const mark = c.ok === true ? '✅' : c.ok === false ? '❌' : '➖';
						return `<tr><td>${mark}</td><td>${frappe.utils.escape_html(c.label)}</td>` +
							`<td>${frappe.utils.escape_html(c.detail || '')}</td></tr>`;
					}).join('');
					frappe.msgprint({
						title: __("Lipsey's connection"),
						message: `<table class="table table-sm"><tbody>${rows}</tbody></table>`,
					});
				},
			});
		});
		$test.removeClass('btn-default').addClass('btn-primary');
	},
});
