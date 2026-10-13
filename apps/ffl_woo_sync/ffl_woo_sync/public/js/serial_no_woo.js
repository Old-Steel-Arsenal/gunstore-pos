/**
 * Serial No form — WooCommerce per-serial action buttons.
 *
 * Registered via hooks.py `doctype_js`. Gives staff per-gun control that the
 * Item form's "Send to WooCommerce" button can't: that button pushes *all*
 * Active serials at once, with no way to list/delist a single physical firearm.
 *
 *   "Send to WooCommerce"  (green) — push_serial_now: create/update this one
 *       serial's WC product (SKU `item_code::serial`, its own price/condition).
 *       Shown only for Active serials (a non-Active serial is not sellable).
 *
 *   "Delist from WooCommerce" (red) — delist_serial_now: set this serial's WC
 *       product to draft + qty 0. Shown while the serial has a WC product that
 *       is not already delisted; a delisted one keeps its product id, so the
 *       woo_delisted flag is what decides. When it IS delisted the form carries
 *       an orange indicator saying so, and "Send to WooCommerce" relists it.
 *
 * All buttons appear ONLY for per-serial firearms (is_firearm=1 and
 * uniform_sell_price=0). Uniform-price firearms and non-firearms are listed as
 * a single item-level product from the Item form, so a per-serial product would
 * contradict that model — the buttons are intentionally hidden for them.
 *
 * Backend methods live in `ffl_woo_sync.woocommerce.client_api`.
 */
frappe.ui.form.on('Serial No', {
    refresh(frm) {
        if (frm.is_new() || !frm.doc.item_code) return;

        // Gate on the parent Item: only per-serial firearms get per-gun listing.
        frappe.db
            .get_value('Item', frm.doc.item_code, ['is_firearm', 'uniform_sell_price'])
            .then(function (r) {
                const item = (r && r.message) || {};
                // cint() — NOT Boolean(): frappe.db.get_value can return Check
                // fields as the strings "0"/"1", and Boolean("0") === true, which
                // would misclassify the item and surface the buttons on the wrong
                // guns. cint("0") === 0 is falsy, so the gate stays correct.
                const is_per_serial =
                    cint(item.is_firearm) && !cint(item.uniform_sell_price);
                if (!is_per_serial) return;

                // Say the state outright. Encoding it in whether a button exists is
                // what produced the bug this replaced: the button stayed, and a delisted
                // gun looked identical to a listed one.
                if (cint(frm.doc.woo_delisted)) {
                    frm.dashboard.add_indicator(__('Delisted from WooCommerce'), 'orange');
                }

                // ── "Send to WooCommerce" (green) — Active serials only ──────────
                if (frm.doc.status === 'Active') {
                    const $push = frm.add_custom_button(__('Send to WooCommerce'), function () {
                        frappe.call({
                            method: 'ffl_woo_sync.woocommerce.client_api.push_serial_now',
                            args: { serial_no: frm.doc.name },
                            freeze: true,
                            freeze_message: __('Pushing to WooCommerce…'),
                            callback(resp) {
                                const r = resp.message || {};
                                if (r.ok) {
                                    frappe.show_alert({
                                        message: __('Pushed. WC Product ID: {0}', [
                                            r.woo_product_id,
                                        ]),
                                        indicator: 'green',
                                    });
                                    frm.reload_doc();
                                    return;
                                }
                                // A guard refused (no price / hand-delisted). Without
                                // this branch the button did nothing and said nothing —
                                // the operator would reasonably assume it worked.
                                if (r.skipped) {
                                    frappe.msgprint({
                                        title: __('Not sent to WooCommerce'),
                                        message: r.message,
                                        indicator: 'orange',
                                    });
                                    frm.reload_doc();
                                }
                            },
                        });
                    });
                    $push.removeClass('btn-default').addClass('btn-success');
                }

                // ── "Delist from WooCommerce" (red) — only when product exists ───
                // A delisted record KEEPS its product_id — the WooCommerce
                // product still exists, drafted, and that id is how it is found
                // again to relist. `woo_delisted` is the field that says it is
                // currently off the shop, so the gate has to read both. Gating
                // on the id alone left the button up after a delist, and
                // pressing it drafted an already-drafted product.
                if (frm.doc.woo_product_id && !cint(frm.doc.woo_delisted)) {
                    const $delist = frm.add_custom_button(__('Delist from WooCommerce'), function () {
                        frappe.confirm(
                            __('Set this product to Draft on WooCommerce (hides it from the shop)?'),
                            function () {
                                frappe.call({
                                    method: 'ffl_woo_sync.woocommerce.client_api.delist_serial_now',
                                    args: { serial_no: frm.doc.name },
                                    freeze: true,
                                    freeze_message: __('Delisting from WooCommerce…'),
                                    callback(resp) {
                                        if (resp.message && resp.message.ok) {
                                            frappe.show_alert({
                                                message: __('Delisted from WooCommerce.'),
                                                indicator: 'orange',
                                            });
                                            frm.reload_doc();
                                        }
                                    },
                                });
                            }
                        );
                    });
                    $delist.removeClass('btn-default').addClass('btn-danger');
                }
            });
    },
});
