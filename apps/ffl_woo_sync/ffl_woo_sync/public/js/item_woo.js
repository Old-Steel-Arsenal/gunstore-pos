/**
 * Item form — WooCommerce action buttons.
 *
 * Adds custom buttons to the Item form (registered via hooks.py `doctype_js`):
 *
 *   "Send to WooCommerce"  (green)  — always calls push_item_now.
 *       Server-side routing in client_api.push_item_now handles the
 *       per-serial / item-level distinction so all callers (button, MCP)
 *       behave correctly without JS detection.
 *
 *   "Delist from WooCommerce" (red) — set WC product to draft + qty 0.
 *       Shown while the item has a WC product that is not already delisted; a
 *       delisted item keeps its product id, so woo_delisted is what decides.
 *       When it IS delisted the form says so with an orange indicator, and
 *       "Send to WooCommerce" relists it.
 *
 * Both buttons call whitelisted Python methods in
 * `ffl_woo_sync.woocommerce.client_api`, with freeze + alert on success and
 * a form reload so the WooCommerce section fields refresh.
 */
frappe.ui.form.on('Item', {
    refresh(frm) {
        if (frm.is_new()) return;

        // Say the state outright. Encoding it in whether a button exists is
        // what produced the bug this replaced: the button stayed, and a delisted
        // gun looked identical to a listed one.
        if (cint(frm.doc.woo_delisted)) {
            frm.dashboard.add_indicator(__('Delisted from WooCommerce'), 'orange');
        }

        // ── "Send to WooCommerce" (green) ──────────────────────────────────
        // push_item_now routes internally: per-serial firearm → all Active
        // Serial Nos; non-firearm / uniform-price firearm → item-level product.
        const $push = frm.add_custom_button(__('Send to WooCommerce'), function () {
            frappe.call({
                method: 'ffl_woo_sync.woocommerce.client_api.push_item_now',
                args: { item_code: frm.doc.name },
                freeze: true,
                freeze_message: __('Pushing to WooCommerce…'),
                callback(r) {
                    if (!r.message) return;
                    const msg = r.message;
                    // Per-serial response has "pushed" key; item-level has "woo_product_id".
                    if (Object.prototype.hasOwnProperty.call(msg, 'pushed')) {
                        // Guard refusals are their own bucket. Folding them into
                        // "pushed" is what let a green "Pushed 2/2" appear over two
                        // guns that were still draft on the shop.
                        const held = (msg.skipped || []).length;
                        const errs = (msg.errors || []).length;
                        if (held) {
                            frappe.msgprint({
                                title: __('Some guns were not sent'),
                                message:
                                    __('Sent {0} of {1}. {2} held back:', [msg.pushed.length, msg.total, held]) +
                                    '<ul>' +
                                    (msg.skipped || [])
                                        .map((s) => `<li><b>${frappe.utils.escape_html(s.serial_no)}</b> — ${frappe.utils.escape_html(s.message || s.reason)}</li>`)
                                        .join('') +
                                    '</ul>',
                                indicator: 'orange',
                            });
                        } else {
                            frappe.show_alert({
                                message: msg.ok
                                    ? __('Pushed {0}/{1} serial(s) to WooCommerce.', [msg.pushed.length, msg.total])
                                    : __('Pushed {0}/{1} serial(s) — {2} error(s). Check Serial No records.', [
                                        msg.pushed.length, msg.total, errs,
                                      ]),
                                indicator: msg.ok ? 'green' : 'orange',
                            });
                        }
                    } else if (msg.skipped) {
                        // A guard refused (no price / hand-delisted). The green
                        // "Pushed." alert below would otherwise announce a push
                        // that never happened, with an empty product id.
                        frappe.msgprint({
                            title: __('Not sent to WooCommerce'),
                            message: msg.message,
                            indicator: 'orange',
                        });
                    } else {
                        frappe.show_alert({
                            message: __('Pushed. WC Product ID: {0}', [msg.woo_product_id]),
                            indicator: 'green',
                        });
                    }
                    frm.reload_doc();
                },
            });
        });
        $push.removeClass('btn-default').addClass('btn-success');

        // ── "Delist from WooCommerce" (red) — only when product exists ─────
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
                            method: 'ffl_woo_sync.woocommerce.client_api.delist_item_now',
                            args: { item_code: frm.doc.name },
                            freeze: true,
                            freeze_message: __('Delisting from WooCommerce…'),
                            callback(r) {
                                if (r.message && r.message.ok) {
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
    },
});
