// ATF FFL Record form — one-click party creation.
//
// The natural B2B onboarding path: find (or live-lookup) the dealer in the
// directory, then create the Customer/Supplier straight from the record with
// is_ffl_dealer + the formal atf_ffl_record link set; the validate sync
// fills the license fields. Idempotent server-side — an existing party
// carrying this FFL number is opened instead of duplicated.

frappe.ui.form.on('ATF FFL Record', {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__('Create Dealer Customer'),
			() => createParty(frm, 'Customer'), __('Create'));
		frm.add_custom_button(__('Create Supplier'),
			() => createParty(frm, 'Supplier'), __('Create'));
	},
});

function createParty(frm, partyType) {
	frappe.call({
		method: 'ffl_core.api.atf_link.create_party_from_atf',
		args: { record: frm.doc.name, party_type: partyType },
		freeze: true,
		freeze_message: __('Creating {0}…', [__(partyType)]),
		callback(r) {
			if (!(r.message && r.message.name)) return;
			frappe.show_alert({
				message: r.message.created
					? __('{0} {1} created.', [__(partyType), r.message.name])
					: __('{0} {1} already carries this FFL — opening it.',
						[__(partyType), r.message.name]),
				indicator: r.message.created ? 'green' : 'orange',
			}, 6);
			frappe.set_route('Form', partyType, r.message.name);
		},
	});
}
