// GunBroker Settings form.
//
// Two buttons, both read-only against GunBroker. Nothing here can create,
// change or end a listing — this form is where somebody decides whether the
// integration is safe to switch on, and a button that writes to a marketplace
// does not belong on that page.
//
// The buttons appear only once the integration is enabled: before that every
// call would come back "GunBroker is switched off", and offering an action
// whose only outcome is that message wastes the operator's time.

frappe.ui.form.on('GunBroker Settings', {
	refresh(frm) {
		_gbEnvironmentBanner(frm);
		_gbWebhookUrls(frm);

		if (!frm.doc.enabled) return;
		const GROUP = __('GunBroker');

		// On the toolbar itself, not in the group: it is the one thing to press
		// after every change on this page.
		const $test = frm.add_custom_button(__('Test Connection'), function () {
			frappe.call({
				method: 'ffl_integrations.gunbroker.client_api.test_connection',
				freeze: true,
				freeze_message: __('Contacting GunBroker…'),
				callback(resp) { _gbConnectionResult(resp.message || {}); },
			});
		});
		$test.removeClass('btn-default').addClass('btn-primary');

		frm.add_custom_button(__('Fetch Listing Defaults'), function () {
			frappe.call({
				method: 'ffl_integrations.gunbroker.client_api.fetch_listing_defaults',
				freeze: true,
				freeze_message: __('Reading the GunBroker account…'),
				callback(resp) {
					const m = resp.message || {};
					if (!m.ok) return;
					frappe.msgprint({
						title: __('GunBroker account defaults'),
						message: `<pre style="max-height:420px;overflow:auto">${
							frappe.utils.escape_html(JSON.stringify(m.defaults, null, 2))}</pre>`,
					});
				},
			});
		}, GROUP);
	},
});

// The sandbox is the one environment worth a standing note: nothing done on it
// reaches a buyer, and the form says so before anyone reads a sandbox result as
// real. The live marketplace is the ordinary working state and gets no banner —
// a red bar over the correct configuration reads as an error.
function _gbEnvironmentBanner(frm) {
	if (frm.doc.enabled && frm.doc.sandbox_mode) {
		frm.dashboard.set_headline(
			__('Sandbox — calls go to api.sandbox.gunbroker.com. Nothing here reaches real buyers.'),
			'blue');
		return;
	}
	frm.dashboard.clear_headline();
}

function _gbConnectionResult(m) {
	if (!m.ok) {
		frappe.msgprint({
			title: __('Could not connect to GunBroker'),
			message: `${frappe.utils.escape_html(m.error || __('No reason given.'))}
				<br><br>${__('Environment')}: <b>${m.sandbox ? __('sandbox') : __('LIVE')}</b>`,
			indicator: 'red',
		});
		return;
	}
	const rows = [
		[__('Environment'), m.sandbox ? __('Sandbox') : __('LIVE marketplace')],
		[__('API address'), m.base_url || '—'],
		[__('GunBroker server time'), m.server_time || '—'],
		[__('Signed in as'), m.account || '—'],
		[__('Payment gateway on the account'), m.gateway_enabled ? __('yes') : __('no')],
	];
	const warnings = (m.warnings || []).length
		? `<div class="alert alert-warning" style="margin-top:10px">${
			m.warnings.map((w) => frappe.utils.escape_html(w)).join('<br>')}</div>`
		: '';
	frappe.msgprint({
		title: __('GunBroker connection OK'),
		message: '<table class="table table-bordered"><tbody>' + rows.map(
			([k, v]) => `<tr><td style="width:45%">${k}</td><td>${frappe.utils.escape_html(String(v))}</td></tr>`,
		).join('') + '</tbody></table>' + warnings,
		indicator: 'green',
	});
}

// The five addresses GunBroker has to be told about, each pasted into its own row at
// gunbroker.com/account/webhooks. Built on the server from this site's own URL
// (onload), so a staging copy shows its own addresses.
function _gbWebhookUrls(frm) {
	const field = frm.fields_dict.webhook_urls;
	if (!field) return;
	const rows = (frm.doc.__onload && frm.doc.__onload.webhook_urls) || [];
	const html = rows.map(r =>
		`<div style="margin-bottom:6px"><b>${frappe.utils.escape_html(r.event)}</b><br>`
		+ `<code>${frappe.utils.escape_html(r.url)}</code></div>`).join('');
	field.$wrapper.html(
		`<p class="text-muted">${__('Add one webhook per event at gunbroker.com/account/webhooks, with the user name and password above.')}</p>`
		+ html);
}
