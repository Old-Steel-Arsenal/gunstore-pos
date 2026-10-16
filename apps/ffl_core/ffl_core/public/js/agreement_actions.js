// Waivers and agreements at the counter (rules: ffl_core/agreements.py).
//
//   ffl.agreements.request(customer, template?) — the Request agreement dialog:
//     • Show QR code (default): the store's tablet, or the customer's phone, scans it
//       and signs on the page /agreement/sign. The link works once, for 72 hours.
//     • Email link: the same link, sent to the customer's email address.
//     • Record paper copy (managers): opens the customer's Pending copy, where the
//       signed scan is attached and recorded.
//   ffl.agreements.recordPaper(frm) — the paper-copy dialog on a Customer Agreement.
// The counter panel's agreement chips (customer_flags.js) and the Customer form's
// Agreements section open request().
(function () {
	if (typeof window === 'undefined' || typeof module === 'object') return;
	window.ffl = window.ffl || {};
	var M = 'ffl_core.agreements.';
	var esc = function (s) { return frappe.utils.escape_html(s == null ? '' : String(s)); };
	var managers = ['Sales Manager', 'System Manager'];
	function isManager() { return managers.some(function (r) { return frappe.user.has_role(r); }); }

	function showQr(r, customerName) {
		var d = new frappe.ui.Dialog({
			title: __('Scan to sign'),
			fields: [{ fieldtype: 'HTML', fieldname: 'qr', options:
				'<div style="text-align:center">'
				+ '<p>' + __('{0}: scan with the store tablet or the customer\'s phone.', ['<b>' + esc(customerName) + '</b>']) + '</p>'
				+ '<img src="' + r.qr_svg + '" alt="QR" style="width:280px; max-width:100%; image-rendering:pixelated">'
				+ '<p class="text-muted small" style="margin-top:8px">' + __('The link works once, until {0}.', [esc(frappe.datetime.str_to_user(r.token_expires))]) + '</p>'
				+ '<p><a href="' + esc(r.sign_url) + '" target="_blank" rel="noopener">' + __('Open on this screen') + '</a></p>'
				+ '</div>' }],
			primary_action_label: __('Done'),
			primary_action: function () { d.hide(); },
		});
		d.show();
		return d;
	}

	// The pending copy may still have a working link — the customer could be signing
	// on the tablet right now. Ask before a new link cancels it.
	function send(customer, template, deliver, replace) {
		return frappe.call({ method: M + 'request_agreement', freeze: true,
			args: { customer: customer, template: template, deliver: deliver, replace: replace ? 1 : 0 } })
			.then(function (r) {
				var m = r.message || {};
				if (!m.link_open) return m;
				return new Promise(function (resolve) {
					frappe.confirm(__('A signing link for this agreement is still open (until {0}); the customer may be signing it now. Make a new one? The open link will stop working.',
						[esc(frappe.datetime.str_to_user(m.token_expires))]),
					function () { resolve(send(customer, template, deliver, true)); });
				});
			});
	}

	function request(customer, template, opts) {
		opts = opts || {};
		var done = opts.done || function () {};
		var manager = isManager();
		var d = new frappe.ui.Dialog({
			title: __('Request agreement'),
			fields: [
				{ fieldtype: 'Link', fieldname: 'template', label: __('Agreement'), options: 'Agreement Template', reqd: 1,
					default: template || '', get_query: function () { return { filters: { active: 1 } }; } },
				{ fieldtype: 'HTML', fieldname: 'help', options: '<p class="text-muted small">'
					+ __('Show QR code: the customer signs on the store tablet or their phone. Email link: they sign from home.')
					+ (manager ? ' ' + __('Record paper copy: attach a copy the customer signed on paper.') : '') + '</p>' },
			],
			primary_action_label: __('Show QR code'),
			primary_action: function (v) {
				send(customer, v.template, 'qr').then(function (r) {
					d.hide();
					showQr(r, opts.customer_name || customer).onhide = done;
				});
			},
			secondary_action_label: __('Email link'),
			secondary_action: function () {
				var v = d.get_values();
				if (!v) return;
				send(customer, v.template, 'email').then(function (r) {
					d.hide();
					frappe.show_alert({ message: __('Signing link emailed to {0}', [esc(r.email)]), indicator: 'green' });
					done();
				});
			},
		});
		if (manager) {
			d.add_custom_action(__('Record paper copy'), function () {
				var v = d.get_values();
				if (!v) return;
				send(customer, v.template, 'none').then(function (r) {
					d.hide();
					// A new tab: the register's cart (or the customer form) stays as it is.
					window.open('/app/customer-agreement/' + encodeURIComponent(r.name) + '?ffl_record_paper=1', '_blank');
					done();
				});
			});
		}
		d.show();
		return d;
	}

	function recordPaper(frm) {
		var d = new frappe.ui.Dialog({
			title: __('Record paper copy'),
			fields: [
				{ fieldtype: 'HTML', options: '<p class="text-muted small">'
					+ __('Attach a scan or photo of the paper the customer signed. It is kept private.') + '</p>' },
				{ fieldtype: 'Attach', fieldname: 'file_url', label: __('Signed paper'), reqd: 1 },
				{ fieldtype: 'Data', fieldname: 'signer_name', label: __('Name as signed'), reqd: 1,
					default: frm.doc.customer_name },
				{ fieldtype: 'Date', fieldname: 'paper_signed_on', label: __('Date on the paper'), reqd: 1,
					default: frappe.datetime.get_today() },
				{ fieldtype: 'Check', fieldname: 'participant_is_minor', label: __('Participant is under 18') },
				{ fieldtype: 'Select', fieldname: 'signer_relationship', label: __('Signed by'),
					options: ['Self', 'Parent', 'Guardian'], default: 'Self' },
			],
			primary_action_label: __('Record'),
			primary_action: function (v) {
				frappe.call({ method: M + 'record_paper_agreement', freeze: true, args: {
					name: frm.doc.name, file_url: v.file_url, signer_name: v.signer_name,
					paper_signed_on: v.paper_signed_on, participant_is_minor: v.participant_is_minor ? 1 : 0,
					signer_relationship: v.signer_relationship,
				} }).then(function () {
					d.hide();
					frappe.show_alert({ message: __('Paper copy recorded'), indicator: 'green' });
					frm.reload_doc();
				});
			},
		});
		// Attach uploads onto this agreement, so the server can tell the scan is its own.
		d.fields_dict.file_url.df.options = { doctype: frm.doctype, docname: frm.doc.name };
		d.show();
		return d;
	}

	// Counter panel chips (customer_flags.stripHtml): click to ask for the agreement.
	$(document).on('click', '[data-ffl-agreement-template]', function (e) {
		e.preventDefault();
		request($(this).attr('data-ffl-agreement-customer'), $(this).attr('data-ffl-agreement-template'));
	});

	window.ffl.agreements = { request: request, recordPaper: recordPaper, showQr: showQr };
})();
