// Waivers and agreements at the counter (rules: ffl_core/agreements.py).
//
//   ffl.agreements.request(customer, template?) — the Request agreement dialog:
//     • Send to tablet (default once a Signing Terminal is paired): the agreement
//       opens on the store's signing tablet by itself (ffl_core/signing_terminal.py).
//     • Show QR code: the store's tablet, or the customer's phone, scans it
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
	function send(customer, template, deliver, replace, terminal) {
		return frappe.call({ method: M + 'request_agreement', freeze: true,
			args: { customer: customer, template: template, deliver: deliver, replace: replace ? 1 : 0, terminal: terminal || null } })
			.then(function (r) {
				var m = r.message || {};
				if (!m.link_open) return m;
				return new Promise(function (resolve) {
					frappe.confirm(__('A signing link for this agreement is still open (until {0}); the customer may be signing it now. Make a new one? The open link will stop working.',
						[esc(frappe.datetime.str_to_user(m.token_expires))]),
					function () { resolve(send(customer, template, deliver, true, terminal)); });
				});
			});
	}

	// Which signing tablet to send to: the one used last on this computer, else the one
	// set up for this register, else the first paired one.
	var LAST_TERMINAL = 'ffl-agreement-terminal';
	function pickTerminal(terminals) {
		var paired = terminals.filter(function (t) { return t.paired; });
		var last = null;
		try { last = localStorage.getItem(LAST_TERMINAL); } catch (e) { /* no storage */ }
		var profile = window.cur_pos && cur_pos.frm && cur_pos.frm.doc && cur_pos.frm.doc.pos_profile;
		var hit = paired.filter(function (t) { return t.name === last; })[0]
			|| paired.filter(function (t) { return profile && t.pos_profile === profile; })[0]
			|| paired[0];
		return hit ? hit.name : '';
	}

	function request(customer, template, opts) {
		opts = opts || {};
		var done = opts.done || function () {};
		var manager = isManager();
		var who = opts.customer_name || customer;
		return frappe.call({ method: 'ffl_core.signing_terminal.list_terminals' })
			.then(function (r) { return (r.message || []).filter(function (t) { return t.paired; }); },
				function () { return []; })
			.then(function (terminals) { return dialog(terminals); });

		function dialog(terminals) {
			var tablets = terminals.length > 0;
			var fields = [
				{ fieldtype: 'Link', fieldname: 'template', label: __('Agreement'), options: 'Agreement Template', reqd: 1,
					default: template || '', get_query: function () { return { filters: { active: 1 } }; } },
			];
			if (tablets) {
				fields.push({ fieldtype: 'Select', fieldname: 'terminal', label: __('Signing tablet'),
					options: terminals.map(function (t) { return t.name; }), default: pickTerminal(terminals) });
			}
			fields.push({ fieldtype: 'HTML', fieldname: 'help', options: '<p class="text-muted small">'
				+ (tablets ? __('Send to tablet: it opens on the store tablet for the customer to sign.') + ' ' : '')
				+ __('Show QR code: the customer scans it with a tablet or their phone. Email link: they sign from home.')
				+ (manager ? ' ' + __('Record paper copy: attach a copy the customer signed on paper.') : '') + '</p>' });
			var showQrCode = function (v) {
				send(customer, v.template, 'qr').then(function (r) {
					d.hide();
					showQr(r, who).onhide = done;
				});
			};
			var d = new frappe.ui.Dialog({
				title: __('Request agreement'),
				fields: fields,
				primary_action_label: tablets ? __('Send to tablet') : __('Show QR code'),
				primary_action: function (v) {
					if (!tablets) return showQrCode(v);
					send(customer, v.template, 'tablet', false, v.terminal).then(function (r) {
						try { localStorage.setItem(LAST_TERMINAL, v.terminal); } catch (e) { /* no storage */ }
						d.hide();
						frappe.show_alert({ message: __('Sent to {0}. {1} can sign there now.', [esc(r.terminal), esc(who)]), indicator: 'green' });
						done();
					});
				},
				secondary_action_label: tablets ? __('Show QR code') : __('Email link'),
				secondary_action: function () {
					var v = d.get_values();
					if (!v) return;
					if (tablets) return showQrCode(v);
					emailLink(v);
				},
			});
			var emailLink = function (v) {
				send(customer, v.template, 'email').then(function (r) {
					d.hide();
					frappe.show_alert({ message: __('Signing link emailed to {0}', [esc(r.email)]), indicator: 'green' });
					done();
				});
			};
			if (tablets) {
				d.add_custom_action(__('Email link'), function () {
					var v = d.get_values();
					if (v) emailLink(v);
				});
			}
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

	// The register: adding an Item that needs an agreement the customer has not signed
	// prompts right away (the submit is refused anyway: agreements.refuse_missing_required).
	// Once per sale and agreement; after ERPNext's own add-item work, never in its way.
	var prompted = {};
	frappe.ui.form.on('POS Invoice Item', {
		item_code: function (frm, cdt, cdn) {
			var row = locals[cdt] && locals[cdt][cdn];
			var customer = frm.doc.customer;
			if (!row || !row.item_code || !customer || frm.doc.is_return || frm.doc.docstatus !== 0) return;
			frappe.call({ method: M + 'missing_for_items', args: { customer: customer, item_codes: [row.item_code] } })
				.then(function (r) {
					(r.message || []).forEach(function (tpl) {
						var key = (frm.doc.name || '') + '|' + customer + '|' + tpl;
						if (prompted[key]) return;
						prompted[key] = true;
						setTimeout(function () {
							frappe.confirm(__('{0} needs {1} signed before this sale can complete. Send it now?',
								['<b>' + esc(row.item_name || row.item_code) + '</b>', '<b>' + esc(tpl) + '</b>']),
							function () { request(customer, tpl, { customer_name: frm.doc.customer_name }); });
						}, 300);
					});
				}, function () { /* a hint; the submit gate holds */ });
		},
	});

	// Counter panel chips (customer_flags.stripHtml): click to ask for the agreement.
	$(document).on('click', '[data-ffl-agreement-template]', function (e) {
		e.preventDefault();
		request($(this).attr('data-ffl-agreement-customer'), $(this).attr('data-ffl-agreement-template'));
	});

	window.ffl.agreements = { request: request, recordPaper: recordPaper, showQr: showQr };
})();
