// Serial No list: entry point to the upload-by-serial page. ERPNext's own
// listview settings for Serial No (indicators) load before this file, so extend
// rather than replace.
(function () {
	const prev = frappe.listview_settings['Serial No'] || {};
	const prevOnload = prev.onload;
	frappe.listview_settings['Serial No'] = Object.assign(prev, {
		onload(listview) {
			if (prevOnload) prevOnload(listview);
			listview.page.add_menu_item(__('Upload photos by serial'), () => frappe.set_route('serial-photos'));
		},
	});
})();
