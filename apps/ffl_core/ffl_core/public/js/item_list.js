// Item list view — bulk Force Delete action.
//
// The "Quantity" column shown in the list comes from the Item.actual_qty
// custom field (in_list_view=1, kept in sync via the Stock Ledger Entry
// doc_events hook in hooks.py + ffl_core.api.item_admin). No DOM-injection
// hack needed — Frappe renders it as a normal column.

frappe.listview_settings = frappe.listview_settings || {};
frappe.listview_settings['Item'] = frappe.listview_settings['Item'] || {};

// Make sure `image` is fetched as part of each list row's data even when
// the column isn't explicitly enabled via List Settings — the DOM
// decorator below reads from `listview.data[i].image`.
frappe.listview_settings['Item'].add_fields = (frappe.listview_settings['Item'].add_fields || [])
	.concat(['image']);

const _origOnload = frappe.listview_settings['Item'].onload;

frappe.listview_settings['Item'].onload = function (listview) {
	if (typeof _origOnload === 'function') {
		try { _origOnload(listview); } catch (e) { /* keep going */ }
	}

	if (frappe.user.has_role('System Manager')) {
		listview.page.add_actions_menu_item(__('Force Delete'), () => {
			const codes = listview.get_checked_items().map((d) => d.name);
			if (!codes.length) {
				frappe.show_alert({ message: __('Select one or more items first.'), indicator: 'orange' });
				return;
			}
			if (typeof window.gunStoreForceDeleteItems === 'function') {
				window.gunStoreForceDeleteItems(codes);
			} else {
				frappe.msgprint(__('Force Delete dialog not loaded. Refresh the page.'));
			}
		}, true);
	}

	// Initial decorate after first render. Frappe calls listview_settings.refresh
	// (defined below) on every subsequent re-render — that's the supported
	// hook, no MutationObserver needed (and no leak across SPA route changes).
	setTimeout(() => decorateRowImages(listview), 200);
};

// Frappe calls this on every list refresh (filter, pagination, sort).
const _origRefresh = frappe.listview_settings['Item'].refresh;
frappe.listview_settings['Item'].refresh = function (listview) {
	if (typeof _origRefresh === 'function') {
		try { _origRefresh(listview); } catch (e) { /* keep going */ }
	}
	decorateRowImages(listview);
};

function decorateRowImages(listview) {
	if (!listview || !listview.$result) return;
	const $rows = listview.$result.find('.list-row:not([data-img-decorated])');
	if (!$rows.length) return;

	$rows.each(function () {
		const $row = $(this);
		$row.attr('data-img-decorated', '1');

		const code = $row.find('a[data-name], .list-id-link').first().attr('data-name');
		if (!code) return;
		// Look up image from the listview's data cache
		const doc = (listview.data || []).find((d) => d.name === code);
		const url = doc && doc.image;

		const $subject = $row.find('.list-row-col.list-subject').first();
		if (!$subject.length) return;
		// Hide Frappe's built-in avatar so we don't double up
		$subject.find('.list-row-image, .avatar').hide();

		const $thumb = url
			? $(`<img class="gunstore-row-thumb" src="${frappe.utils.escape_html(url)}"
					loading="lazy"
					onerror="this.style.visibility='hidden'"
					style="width:44px;height:44px;object-fit:cover;border-radius:4px;
						border:1px solid var(--border-color); margin-right:10px;
						flex-shrink:0; cursor:pointer">`)
			: $(`<div class="gunstore-row-thumb"
					style="width:44px;height:44px;border-radius:4px;
						border:1px dashed var(--border-color); margin-right:10px;
						flex-shrink:0; background:var(--subtle-fg)"></div>`);

		// Insert as the first child of the subject cell (after the checkbox).
		const $checkbox = $subject.children().filter('.list-row-checkbox, input[type=checkbox]').first();
		if ($checkbox.length) {
			$checkbox.after($thumb);
		} else {
			$subject.prepend($thumb);
		}
	});
}
