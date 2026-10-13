"""Pay-on-sale auto-settlement — GL/ledger acceptance tests.

A "pay-on-sale" supplier (e.g. TEST VENDOR) ships guns the store receives as
OWNED inventory, cost parked in **Stock Received But Not Billed** (SRBNB, a
liability), and only settled to a named Account Payable when the gun sells. This
suite turns the accountant's blocking conditions and ledger checklist into
executable assertions.

Layers, cheapest first (mirrors test_consignment_gl.py):
  - SetupFields:               setup.pay_on_sale.ensure() creates the stamp fields.
  - SettleDirect:              compliance.pay_on_sale.settle_pay_on_sale_on_submit
                               over lightweight SimpleNamespace docs — every skip /
                               BLOCK branch, cheaply, no real invoice.
  - SettleInvoice:             end-to-end through a REAL submitted Sales Invoice
                               on_submit hook (proves the wiring), incl. cancel-reverse.
  - ReceiveStamp:              a Purchase Receipt from a pay_on_sale supplier stamps
                               the serial at intake.
  - Backfill:                  the manual (unregistered) backfill labels in-stock guns
                               with ZERO GL.

GL invariant under test: the settlement JE is EXACTLY
  Dr SRBNB / Cr Creditors[party=Supplier] = srbnb_cost      (no COGS leg, P&L net zero).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

COMPANY_NAME = "FFL Test"


class SetupFields(FrappeTestCase):
    """setup.pay_on_sale.ensure() must create the Supplier flag, the four Serial
    No stamp fields, and the Journal Entry source-tag field."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from ffl_core.setup import dev_site
        dev_site.run()

    def test_ensure_creates_supplier_flag(self):
        from ffl_core.setup import pay_on_sale
        pay_on_sale.ensure()
        self.assertTrue(frappe.db.exists("Custom Field", "Supplier-pay_on_sale"))
        field = frappe.get_doc("Custom Field", "Supplier-pay_on_sale")
        self.assertEqual(field.fieldtype, "Check")

    def test_ensure_creates_serial_stamp_fields(self):
        from ffl_core.setup import pay_on_sale
        pay_on_sale.ensure()
        for fn, ftype in (
            ("pay_on_sale", "Check"),
            ("pay_on_sale_supplier", "Link"),
            ("srbnb_cost", "Currency"),
            ("pay_on_sale_settled", "Check"),
        ):
            name = f"Serial No-{fn}"
            self.assertTrue(frappe.db.exists("Custom Field", name), f"missing {name}")
            self.assertEqual(frappe.get_doc("Custom Field", name).fieldtype, ftype)
        # the supplier link must point at Supplier — it's the settlement party
        self.assertEqual(
            frappe.get_doc("Custom Field", "Serial No-pay_on_sale_supplier").options,
            "Supplier")

    def test_ensure_creates_journal_entry_source_field(self):
        from ffl_core.setup import pay_on_sale
        pay_on_sale.ensure()
        name = "Journal Entry-ffl_pay_on_sale_source"
        self.assertTrue(frappe.db.exists("Custom Field", name))
        field = frappe.get_doc("Custom Field", name)
        self.assertEqual(field.fieldtype, "Data")
        self.assertEqual(int(field.read_only), 1)

    def test_account_helpers_resolve_company_accounts(self):
        from ffl_core.setup import pay_on_sale
        company = frappe.db.get_value("Company", {"company_name": COMPANY_NAME}, "name")
        # both resolve from the Company master (portable — never a hard-coded "- TSA")
        self.assertEqual(
            pay_on_sale.srbnb_account(company),
            frappe.db.get_value("Company", company, "stock_received_but_not_billed"))
        self.assertEqual(
            pay_on_sale.payable_account(company),
            frappe.db.get_value("Company", company, "default_payable_account"))


class SettleDirect(FrappeTestCase):
    """compliance.pay_on_sale.settle_pay_on_sale_on_submit called directly over
    lightweight SimpleNamespace docs — cheap coverage of every skip / BLOCK
    branch + the exact 2-line settlement JE, without a real submitted invoice
    (mirrors test_consignment_gl.py's PostConsignmentPayableDirect)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from ffl_core.setup import dev_site, pay_on_sale
        dev_site.run()
        pay_on_sale.ensure()
        cls.company = frappe.db.get_value("Company", {"company_name": COMPANY_NAME}, "name")
        frappe.defaults.set_user_default("Company", cls.company)
        # Defensive (CI fresh company may leave these blank) — the settlement JE
        # needs a Creditors party account + cost center.
        if not frappe.db.get_value("Company", cls.company, "default_payable_account"):
            frappe.db.set_value("Company", cls.company, "default_payable_account",
                frappe.db.get_value("Account", {"company": cls.company,
                    "account_type": "Payable", "is_group": 0}, "name"), update_modified=False)
        cls.srbnb = frappe.db.get_value("Company", cls.company, "stock_received_but_not_billed")
        cls.creditors = frappe.db.get_value("Company", cls.company, "default_payable_account")
        if not frappe.db.exists("Item", "POS-DIRECT-GUN"):
            frappe.get_doc({
                "doctype": "Item", "item_code": "POS-DIRECT-GUN", "item_name": "POS-DIRECT-GUN",
                "item_group": "Pistol", "stock_uom": "Nos",
                "is_stock_item": 1, "has_serial_no": 1,
            }).insert(ignore_permissions=True)
        if not frappe.db.exists("Supplier", "_Test POS Supplier"):
            frappe.get_doc({
                "doctype": "Supplier", "supplier_name": "_Test POS Supplier",
                "supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
                    or "All Supplier Groups",
                "supplier_type": "Company",
            }).insert(ignore_permissions=True)
        cls.supplier = "_Test POS Supplier"

    @staticmethod
    def _ensure_serial(serial: str, *, pay_on_sale: int = 0, supplier: str | None = None,
            srbnb_cost: float | None = None, settled: int = 0, purchase_rate: float | None = None,
            item_code: str = "POS-DIRECT-GUN") -> str:
        if not frappe.db.exists("Serial No", serial):
            frappe.get_doc({
                "doctype": "Serial No", "serial_no": serial, "item_code": item_code,
            }).insert(ignore_permissions=True)
        updates = {"pay_on_sale": pay_on_sale, "pay_on_sale_settled": settled}
        if supplier is not None:
            updates["pay_on_sale_supplier"] = supplier
        if srbnb_cost is not None:
            updates["srbnb_cost"] = srbnb_cost
        if purchase_rate is not None:
            updates["purchase_rate"] = purchase_rate
        frappe.db.set_value("Serial No", serial, updates, update_modified=False)
        return serial

    @staticmethod
    def _row(**over) -> "frappe._dict":
        base = {"idx": 1, "item_code": "POS-DIRECT-GUN", "serial_no": ""}
        base.update(over)
        return frappe._dict(base)

    def _doc(self, *rows, **over):
        base = dict(doctype="Sales Invoice", name=self._testMethodName, company=self.company,
            posting_date=frappe.utils.nowdate(), is_return=0, items=list(rows))
        base.update(over)
        return SimpleNamespace(**base)

    def _settle(self, doc):
        from ffl_core.compliance.pay_on_sale import settle_pay_on_sale_on_submit
        settle_pay_on_sale_on_submit(doc)

    def _je_for(self):
        return frappe.db.get_value(
            "Journal Entry", {"ffl_pay_on_sale_source": self._testMethodName, "docstatus": 1})

    # --- skips (zero write) --------------------------------------------------

    def test_return_of_non_pay_on_sale_posts_nothing(self):
        # a return (is_return=1) whose serial isn't pay-on-sale → nothing to reverse
        serial = self._ensure_serial(self._testMethodName, pay_on_sale=0, srbnb_cost=500.0)
        doc = self._doc(self._row(serial_no=serial), is_return=1)
        self._settle(doc)
        self.assertFalse(self._je_for())

    def test_return_of_unsettled_serial_posts_nothing(self):
        # pay-on-sale but never settled (settled=0) → nothing to reverse (idempotent)
        serial = self._ensure_serial(self._testMethodName, pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=500.0, settled=0)
        doc = self._doc(self._row(serial_no=serial), is_return=1)
        self._settle(doc)
        self.assertFalse(self._je_for())

    def test_return_of_settled_serial_reverses_and_clears_flag(self):
        # A settled pay-on-sale gun returned via a credit note (is_return=1, WITHOUT
        # cancelling the original sale) must reverse its settlement:
        # Dr Creditors[party] / Cr SRBNB = srbnb_cost, no COGS, settled→0. (MAJOR-1)
        serial = self._ensure_serial(self._testMethodName, pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=700.00, settled=1)
        doc = self._doc(self._row(serial_no=serial), is_return=1)
        self._settle(doc)

        je = frappe.get_doc("Journal Entry", self._je_for())
        self.assertEqual(len(je.accounts), 2)
        dr = next(a for a in je.accounts if a.debit_in_account_currency)
        cr = next(a for a in je.accounts if a.credit_in_account_currency)
        # reversal direction is the mirror of settlement: Dr Creditors[party] / Cr SRBNB
        self.assertEqual(dr.account, self.creditors)
        self.assertAlmostEqual(dr.debit_in_account_currency, 700.00)
        self.assertEqual(dr.party_type, "Supplier")
        self.assertEqual(dr.party, self.supplier)
        self.assertEqual(cr.account, self.srbnb)
        self.assertAlmostEqual(cr.credit_in_account_currency, 700.00)
        self.assertFalse(cr.party)
        cogs = frappe.get_cached_value("Company", self.company, "default_expense_account")
        self.assertFalse([a for a in je.accounts if a.account == cogs])
        # flag cleared → the returned gun can re-sell + re-settle
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 0)

    def test_partial_return_reverses_only_returned_serial(self):
        # Two settled guns on one original sale; a return of only A reverses A alone
        # and leaves sibling B settled (no over-reversal).
        a = self._ensure_serial(self._testMethodName + "-A", pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=500.0, settled=1)
        b = self._ensure_serial(self._testMethodName + "-B", pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=500.0, settled=1)
        doc = self._doc(self._row(serial_no=a), is_return=1)  # only A on the credit note
        self._settle(doc)
        self.assertEqual(int(frappe.db.get_value("Serial No", a, "pay_on_sale_settled")), 0)
        self.assertEqual(int(frappe.db.get_value("Serial No", b, "pay_on_sale_settled")), 1)
        je = frappe.get_doc("Journal Entry", self._je_for())
        self.assertEqual(len(je.accounts), 2)  # only A's two lines

    def test_return_missing_supplier_blocks(self):
        # fail-closed on reverse too — can't reverse an AP to an unknown party
        serial = self._ensure_serial(self._testMethodName, pay_on_sale=1,
            supplier="", srbnb_cost=500.0, settled=1)
        doc = self._doc(self._row(serial_no=serial), is_return=1)
        with self.assertRaises(frappe.ValidationError):
            self._settle(doc)
        self.assertFalse(self._je_for())

    def test_return_then_resell_settles_again(self):
        # state cycles through a RETURN too: settle → return-reverse → re-sell settle
        serial = self._ensure_serial(self._testMethodName, pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=500.0, settled=1)
        self._settle(self._doc(self._row(serial_no=serial), is_return=1))
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 0)
        self._settle(self._doc(self._row(serial_no=serial), name=self._testMethodName + "-RESALE"))
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 1)
        self.assertTrue(frappe.db.get_value(
            "Journal Entry", {"ffl_pay_on_sale_source": self._testMethodName + "-RESALE", "docstatus": 1}))

    def test_consolidated_invoice_skipped(self):
        serial = self._ensure_serial(self._testMethodName, pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=500.0)
        doc = self._doc(self._row(serial_no=serial), is_consolidated=1)
        self._settle(doc)
        self.assertFalse(self._je_for())
        # the consolidated skip must NOT stamp settled either
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 0)

    def test_non_pay_on_sale_serial_posts_nothing(self):
        serial = self._ensure_serial(self._testMethodName, pay_on_sale=0, srbnb_cost=500.0)
        doc = self._doc(self._row(serial_no=serial))
        self._settle(doc)
        self.assertFalse(self._je_for())

    def test_already_settled_serial_is_idempotent(self):
        serial = self._ensure_serial(self._testMethodName, pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=500.0, settled=1)
        doc = self._doc(self._row(serial_no=serial))
        self._settle(doc)
        self.assertFalse(self._je_for())

    def test_zero_srbnb_cost_and_zero_purchase_rate_skipped(self):
        # Genuinely no parked cost (e.g. a $0 sample) — owes the supplier nothing.
        serial = self._ensure_serial(self._testMethodName, pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=0.0, purchase_rate=0.0)
        doc = self._doc(self._row(serial_no=serial))
        self._settle(doc)
        self.assertFalse(self._je_for())

    def test_row_without_serial_skipped(self):
        doc = self._doc(self._row(item_code="POS-DIRECT-ACC", serial_no=""))
        self._settle(doc)  # must not raise
        self.assertFalse(self._je_for())

    # --- multi-serial lines: every gun on a qty>1 line settles ---------------

    def test_multi_serial_line_settles_every_serial(self):
        # Web-order fulfilment (dispose_web_order) builds ONE line with qty=2+ and
        # both serials in it ("01378\n01821"). Each gun owes its own supplier its
        # own parked cost, so each gets its own Dr SRBNB / Cr Creditors pair —
        # taking only the line's first serial silently under-settled the rest.
        a = self._ensure_serial(self._testMethodName + "-A", pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=300.00, purchase_rate=300.00)
        b = self._ensure_serial(self._testMethodName + "-B", pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=425.50, purchase_rate=425.50)
        doc = self._doc(self._row(serial_no=f"{a}\n{b}", qty=2))
        self._settle(doc)

        je = frappe.get_doc("Journal Entry", self._je_for())
        self.assertEqual(len(je.accounts), 4)  # one Dr/Cr pair per gun
        dr = [x for x in je.accounts if x.debit_in_account_currency]
        cr = [x for x in je.accounts if x.credit_in_account_currency]
        self.assertTrue(all(x.account == self.srbnb and not x.party for x in dr))
        self.assertEqual(
            sorted(flt(x.debit_in_account_currency) for x in dr), [300.00, 425.50])
        self.assertTrue(all(
            x.account == self.creditors and x.party == self.supplier for x in cr))
        self.assertEqual(
            sorted(flt(x.credit_in_account_currency) for x in cr), [300.00, 425.50])
        self.assertAlmostEqual(je.total_debit, 725.50)
        for serial in (a, b):
            self.assertEqual(
                int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 1,
                f"{serial} left unsettled")

    def test_multi_serial_line_return_reverses_every_serial(self):
        # the mirror: a credit note for a qty=2 line reverses BOTH guns' payables.
        a = self._ensure_serial(self._testMethodName + "-A", pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=300.00, settled=1)
        b = self._ensure_serial(self._testMethodName + "-B", pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=425.50, settled=1)
        doc = self._doc(self._row(serial_no=f"{a}\n{b}", qty=2), is_return=1)
        self._settle(doc)

        je = frappe.get_doc("Journal Entry", self._je_for())
        self.assertEqual(len(je.accounts), 4)
        dr = [x for x in je.accounts if x.debit_in_account_currency]
        cr = [x for x in je.accounts if x.credit_in_account_currency]
        self.assertTrue(all(
            x.account == self.creditors and x.party == self.supplier for x in dr))
        self.assertEqual(
            sorted(flt(x.debit_in_account_currency) for x in dr), [300.00, 425.50])
        self.assertTrue(all(x.account == self.srbnb and not x.party for x in cr))
        self.assertEqual(
            sorted(flt(x.credit_in_account_currency) for x in cr), [300.00, 425.50])
        for serial in (a, b):
            self.assertEqual(
                int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 0,
                f"{serial} left settled")

    def test_multi_serial_line_settles_only_qualifying_serials(self):
        # Eligibility stays PER SERIAL inside the line, independent of position: a
        # non-pay-on-sale sibling and an already-settled one are skipped without
        # holding up the qualifying gun — even when they sit AHEAD of it in the row
        # (the old first-serial-only read stopped at the plain gun and settled
        # nothing at all).
        a = self._ensure_serial(self._testMethodName + "-A", pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=300.00, purchase_rate=300.00)
        plain = self._ensure_serial(self._testMethodName + "-PLAIN", pay_on_sale=0,
            srbnb_cost=999.00)
        done = self._ensure_serial(self._testMethodName + "-DONE", pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=500.00, settled=1)
        doc = self._doc(self._row(serial_no=f"{plain}\n{done}\n{a}", qty=3))
        self._settle(doc)

        je = frappe.get_doc("Journal Entry", self._je_for())
        self.assertEqual(len(je.accounts), 2)  # only A's pair
        self.assertAlmostEqual(je.total_debit, 300.00)
        self.assertEqual(int(frappe.db.get_value("Serial No", a, "pay_on_sale_settled")), 1)
        self.assertEqual(int(frappe.db.get_value("Serial No", plain, "pay_on_sale_settled")), 0)
        self.assertEqual(int(frappe.db.get_value("Serial No", done, "pay_on_sale_settled")), 1)

    def test_each_je_row_is_stamped_with_the_gun_it_settles(self):
        # A 5-gun line produces 10 JE rows; without a per-row mark neither a human
        # auditor nor the cancel path can tell which pair belongs to which gun.
        # The serial rides the STANDARD Journal Entry Account.user_remark field —
        # no custom field, no fixture, upgrade-safe.
        a = self._ensure_serial(self._testMethodName + "-A", pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=300.00, purchase_rate=300.00)
        b = self._ensure_serial(self._testMethodName + "-B", pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=425.50, purchase_rate=425.50)
        self._settle(self._doc(self._row(serial_no=f"{a}\n{b}", qty=2)))

        je = frappe.get_doc("Journal Entry", self._je_for())
        self.assertEqual(
            sorted((x.user_remark or "") for x in je.accounts), sorted([a, a, b, b]))
        # each gun's own Dr/Cr pair carries that gun's serial and its own amount
        for serial, cost in ((a, 300.00), (b, 425.50)):
            pair = [x for x in je.accounts if x.user_remark == serial]
            self.assertEqual(len(pair), 2)
            self.assertEqual(
                sorted(flt(x.debit_in_account_currency) + flt(x.credit_in_account_currency)
                    for x in pair), [cost, cost])

    def test_missing_supplier_on_a_later_serial_blocks_the_whole_line(self):
        # The fail-closed BLOCKs are newly reachable from a line's 2nd..Nth gun.
        # One unstampable gun fails the WHOLE document — we never book the good
        # gun's half and leave the bad one for later (zero writes, same as when
        # the bad gun sat first).
        good = self._ensure_serial(self._testMethodName + "-GOOD", pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=300.00, purchase_rate=300.00)
        bad = self._ensure_serial(self._testMethodName + "-BAD", pay_on_sale=1,
            supplier="", srbnb_cost=500.00, purchase_rate=500.00)
        doc = self._doc(self._row(serial_no=f"{good}\n{bad}", qty=2))

        with self.assertRaises(frappe.ValidationError):
            self._settle(doc)

        self.assertFalse(self._je_for())  # the good gun's pair must NOT land either
        self.assertEqual(int(frappe.db.get_value("Serial No", good, "pay_on_sale_settled")), 0)

    def test_a_serial_repeated_on_the_doc_is_settled_once(self):
        # Same gun twice in one line's text AND again on a second line — a gun can
        # only be owed for once, so it gets exactly one Dr/Cr pair. The settled
        # flag can't guard this: every read happens before any flag write.
        serial = self._ensure_serial(self._testMethodName, pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=300.00, purchase_rate=300.00)
        doc = self._doc(
            self._row(serial_no=f"{serial}\n{serial}", qty=2),
            self._row(idx=2, serial_no=serial, qty=1))
        self._settle(doc)

        je = frappe.get_doc("Journal Entry", self._je_for())
        self.assertEqual(len(je.accounts), 2)
        self.assertAlmostEqual(je.total_debit, 300.00)
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 1)

    # --- BLOCK (fail closed, zero write) -------------------------------------

    def test_missing_supplier_blocks(self):
        # pay_on_sale + real srbnb_cost but no locked supplier → BLOCK, never post
        # an AP to a null/guessed party.
        serial = self._ensure_serial(self._testMethodName, pay_on_sale=1,
            supplier="", srbnb_cost=500.0)
        doc = self._doc(self._row(serial_no=serial))
        with self.assertRaises(frappe.ValidationError):
            self._settle(doc)
        self.assertFalse(self._je_for())
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 0)

    def test_nonexistent_supplier_blocks(self):
        serial = self._ensure_serial(self._testMethodName, pay_on_sale=1,
            supplier="_Ghost Supplier ZZZ", srbnb_cost=500.0)
        # a stale link to a deleted Supplier must fail closed, not post a broken AP
        frappe.db.set_value("Serial No", serial, "pay_on_sale_supplier",
            "_Ghost Supplier ZZZ", update_modified=False)
        doc = self._doc(self._row(serial_no=serial))
        with self.assertRaises(frappe.ValidationError):
            self._settle(doc)
        self.assertFalse(self._je_for())

    def test_missing_srbnb_cost_but_real_purchase_rate_blocks(self):
        # No persisted srbnb_cost yet a real purchase cost exists = an
        # incompletely-stamped pay-on-sale gun. We NEVER guess the settlement
        # amount — fail closed and tell the operator to backfill.
        serial = self._ensure_serial(self._testMethodName, pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=0.0, purchase_rate=500.0)
        doc = self._doc(self._row(serial_no=serial))
        with self.assertRaises(frappe.ValidationError):
            self._settle(doc)
        self.assertFalse(self._je_for())

    # --- happy path: the exact 2-line settlement JE -------------------

    def test_settlement_posts_exactly_dr_srbnb_cr_creditors_no_cogs(self):
        serial = self._ensure_serial(self._testMethodName, pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=700.00, purchase_rate=700.00)
        doc = self._doc(self._row(serial_no=serial))
        self._settle(doc)

        je_name = self._je_for()
        self.assertTrue(je_name)
        je = frappe.get_doc("Journal Entry", je_name)

        # EXACTLY 2 lines — Dr SRBNB (no party) / Cr Creditors[party=Supplier]
        self.assertEqual(len(je.accounts), 2)
        dr = [a for a in je.accounts if a.debit_in_account_currency]
        cr = [a for a in je.accounts if a.credit_in_account_currency]
        self.assertEqual(len(dr), 1)
        self.assertEqual(len(cr), 1)
        self.assertEqual(dr[0].account, self.srbnb)
        self.assertAlmostEqual(dr[0].debit_in_account_currency, 700.00)
        self.assertFalse(dr[0].party)  # SRBNB line carries NO party
        self.assertEqual(cr[0].account, self.creditors)
        self.assertAlmostEqual(cr[0].credit_in_account_currency, 700.00)
        self.assertEqual(cr[0].party_type, "Supplier")
        self.assertEqual(cr[0].party, self.supplier)

        # NO COGS / income leg anywhere — pure liability reclass, P&L net zero
        cogs = frappe.get_cached_value("Company", self.company, "default_expense_account")
        self.assertFalse([a for a in je.accounts if a.account == cogs])
        self.assertAlmostEqual(je.total_debit, je.total_credit)

        # per-serial idempotency flag stamped
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 1)

    def test_repeat_settle_does_not_double_post(self):
        serial = self._ensure_serial(self._testMethodName, pay_on_sale=1,
            supplier=self.supplier, srbnb_cost=500.0, purchase_rate=500.0)
        doc = self._doc(self._row(serial_no=serial))
        self._settle(doc)
        self._settle(doc)  # re-fired hook — settled=1 now guards it
        jes = frappe.get_all("Journal Entry",
            filters={"ffl_pay_on_sale_source": self._testMethodName, "docstatus": 1})
        self.assertEqual(len(jes), 1)


class ReverseDirect(FrappeTestCase):
    """compliance.pay_on_sale.reverse_pay_on_sale_on_cancel — on cancel/return,
    cancel the settlement JE (ERPNext auto-posts the reverse Dr Creditors / Cr
    SRBNB) and flip pay_on_sale_settled back to 0 so the gun can re-sell.
    The flag flip is gated on THIS invoice owning a settlement JE, so a
    consolidated SI (no JE tagged with its name) is a clean no-op."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from ffl_core.setup import dev_site, pay_on_sale
        dev_site.run()
        pay_on_sale.ensure()
        cls.company = frappe.db.get_value("Company", {"company_name": COMPANY_NAME}, "name")
        frappe.defaults.set_user_default("Company", cls.company)
        cls.srbnb = frappe.db.get_value("Company", cls.company, "stock_received_but_not_billed")
        cls.creditors = frappe.db.get_value("Company", cls.company, "default_payable_account")
        if not frappe.db.exists("Item", "POS-REV-GUN"):
            frappe.get_doc({
                "doctype": "Item", "item_code": "POS-REV-GUN", "item_name": "POS-REV-GUN",
                "item_group": "Pistol", "stock_uom": "Nos",
                "is_stock_item": 1, "has_serial_no": 1,
            }).insert(ignore_permissions=True)
        if not frappe.db.exists("Supplier", "_Test Rev Supplier"):
            frappe.get_doc({
                "doctype": "Supplier", "supplier_name": "_Test Rev Supplier",
                "supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
                    or "All Supplier Groups",
                "supplier_type": "Company",
            }).insert(ignore_permissions=True)
        cls.supplier = "_Test Rev Supplier"

    def _seed_settled(self, source: str, *serials: str, cost: float = 500.0) -> None:
        # several serials = ONE qty>1 line carrying them all (web-order shape)
        from ffl_core.compliance.pay_on_sale import settle_pay_on_sale_on_submit
        for serial in serials:
            if not frappe.db.exists("Serial No", serial):
                frappe.get_doc({
                    "doctype": "Serial No", "serial_no": serial, "item_code": "POS-REV-GUN",
                }).insert(ignore_permissions=True)
            frappe.db.set_value("Serial No", serial, {
                "pay_on_sale": 1, "pay_on_sale_supplier": self.supplier,
                "srbnb_cost": cost, "purchase_rate": cost, "pay_on_sale_settled": 0,
            }, update_modified=False)
        doc = SimpleNamespace(doctype="Sales Invoice", name=source, company=self.company,
            posting_date=frappe.utils.nowdate(), is_return=0,
            items=[self._line(*serials)])
        settle_pay_on_sale_on_submit(doc)

    @staticmethod
    def _line(*serials: str) -> "frappe._dict":
        return frappe._dict({"idx": 1, "item_code": "POS-REV-GUN",
            "serial_no": "\n".join(serials), "qty": len(serials)})

    def _cancel_doc(self, source: str, *serials: str) -> SimpleNamespace:
        return SimpleNamespace(doctype="Sales Invoice", name=source,
            items=[self._line(*serials)])

    def _reverse(self, doc) -> None:
        from ffl_core.compliance.pay_on_sale import reverse_pay_on_sale_on_cancel
        reverse_pay_on_sale_on_cancel(doc)

    def test_reverse_cancels_je_and_flips_settled(self):
        serial = self._testMethodName
        self._seed_settled(self._testMethodName, serial)
        je_name = frappe.db.get_value(
            "Journal Entry", {"ffl_pay_on_sale_source": self._testMethodName, "docstatus": 1})
        self.assertTrue(je_name)
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 1)

        self._reverse(self._cancel_doc(self._testMethodName, serial))

        self.assertEqual(frappe.db.get_value("Journal Entry", je_name, "docstatus"), 2)
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 0)
        # GL for the source nets to zero after reversal — no residual AP
        net = frappe.db.sql("""
            SELECT COALESCE(SUM(debit) - SUM(credit), 0) FROM `tabGL Entry`
            WHERE voucher_no = %s
        """, je_name)[0][0]
        self.assertAlmostEqual(flt(net), 0.0, places=2)

    def test_no_matching_je_is_a_noop(self):
        # non-pay-on-sale invoice (no settlement JE at all) — must not raise or
        # touch any serial's flag.
        serial = self._testMethodName
        if not frappe.db.exists("Serial No", serial):
            frappe.get_doc({"doctype": "Serial No", "serial_no": serial,
                "item_code": "POS-REV-GUN"}).insert(ignore_permissions=True)
        frappe.db.set_value("Serial No", serial,
            {"pay_on_sale": 1, "pay_on_sale_settled": 1}, update_modified=False)
        self._reverse(self._cancel_doc(self._testMethodName, serial))
        # a different invoice's cancel must NOT flip this serial (no JE owned)
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 1)

    def test_second_reverse_is_a_noop(self):
        serial = self._testMethodName
        self._seed_settled(self._testMethodName, serial)
        doc = self._cancel_doc(self._testMethodName, serial)
        self._reverse(doc)
        self._reverse(doc)  # already reversed — nothing at docstatus 1; must not raise
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 0)

    def test_cancel_restores_flag_for_every_serial_on_a_line(self):
        # Cancelling a sale whose qty=2 line carried two guns must un-settle BOTH,
        # or the second gun stays flagged settled with its JE gone — it would then
        # never re-settle when re-sold.
        a, b = self._testMethodName + "-A", self._testMethodName + "-B"
        self._seed_settled(self._testMethodName, a, b)
        for serial in (a, b):
            self.assertEqual(
                int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 1)

        self._reverse(self._cancel_doc(self._testMethodName, a, b))

        for serial in (a, b):
            self.assertEqual(
                int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 0,
                f"{serial} left settled after cancel")

    def test_cancel_leaves_a_gun_settled_by_another_invoice_alone(self):
        # THE major-1 case. Gun B was settled by an EARLIER invoice, so when it
        # shows up on this invoice's line the submit path deliberately skips it
        # (already settled). Cancelling THIS invoice must not un-settle it: B's
        # payable from the other invoice is still standing in the GL, and a flag
        # reset would let B's payable be booked a SECOND time when it re-sells.
        # Restoration therefore follows what THIS doc's JE actually booked, not
        # which guns happen to sit on its lines.
        a, b = self._testMethodName + "-A", self._testMethodName + "-B"
        other = self._testMethodName + "-OTHER-INVOICE"
        self._seed_settled(other, b)                 # B settled by a different invoice
        self._seed_settled(self._testMethodName, a)  # A settled by this one
        other_je = frappe.db.get_value(
            "Journal Entry", {"ffl_pay_on_sale_source": other, "docstatus": 1})

        # this invoice's line happens to carry BOTH guns
        self._reverse(self._cancel_doc(self._testMethodName, a, b))

        self.assertEqual(int(frappe.db.get_value("Serial No", a, "pay_on_sale_settled")), 0)
        self.assertEqual(int(frappe.db.get_value("Serial No", b, "pay_on_sale_settled")), 1,
            "B was settled by another invoice — its flag must survive this cancel")
        # and the other invoice's payable is untouched
        self.assertEqual(frappe.db.get_value("Journal Entry", other_je, "docstatus"), 1)

    def test_cancel_of_a_je_without_serial_stamps_falls_back_to_the_lines(self):
        # Journal Entries booked before the per-row serial stamp existed (the prod
        # backlog) carry no user_remark. Those cancel exactly as they always did —
        # restore off the document's own lines — rather than restoring nothing.
        serial = self._testMethodName
        self._seed_settled(self._testMethodName, serial)
        je_name = frappe.db.get_value(
            "Journal Entry", {"ffl_pay_on_sale_source": self._testMethodName, "docstatus": 1})
        for row in frappe.get_all("Journal Entry Account",
                filters={"parent": je_name}, pluck="name"):
            frappe.db.set_value("Journal Entry Account", row, "user_remark", None,
                update_modified=False)

        self._reverse(self._cancel_doc(self._testMethodName, serial))

        self.assertEqual(frappe.db.get_value("Journal Entry", je_name, "docstatus"), 2)
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 0)

    def test_reverse_then_resell_settles_again(self):
        # state cycles: settle → reverse → settle again (a cancelled sale re-sold).
        serial = self._testMethodName
        self._seed_settled(self._testMethodName, serial)
        self._reverse(self._cancel_doc(self._testMethodName, serial))
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 0)
        # re-sell under a new invoice name
        self._seed_settled(self._testMethodName + "-2", serial)
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 1)
        self.assertTrue(frappe.db.get_value(
            "Journal Entry", {"ffl_pay_on_sale_source": self._testMethodName + "-2", "docstatus": 1}))


class SettleInvoice(FrappeTestCase):
    """End-to-end: a REAL submitted Sales Invoice selling a pay-on-sale gun posts
    the settlement JE through the actual on_submit hook (hooks.py), and cancelling
    it reverses through on_cancel — proving the wiring, not just the bare function.
    Uses an ANTIQUE firearm (is_antique=1) so the sale skips the 4473/NICS/dealer
    gates (they gate on is_regulated_firearm_item, which excludes antiques), while
    the gun stays serialized + pay-on-sale-stampable (both live on the Serial No)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from ffl_core.setup import dev_site, pay_on_sale
        dev_site.run()
        pay_on_sale.ensure()
        cls.company = frappe.db.get_value("Company", {"company_name": COMPANY_NAME}, "name")
        frappe.defaults.set_user_default("Company", cls.company)
        cls.warehouse = frappe.db.get_value(
            "Warehouse", {"company": cls.company, "is_group": 0}, "name")
        cls.srbnb = frappe.db.get_value("Company", cls.company, "stock_received_but_not_billed")
        cls.creditors = frappe.db.get_value("Company", cls.company, "default_payable_account")
        if not frappe.db.exists("Item", "POS-E2E-GUN"):
            frappe.get_doc({
                "doctype": "Item", "item_code": "POS-E2E-GUN", "item_name": "POS-E2E-GUN",
                "item_group": "Pistol", "stock_uom": "Nos",
                "is_stock_item": 1, "has_serial_no": 1,
            }).insert(ignore_permissions=True)
            frappe.db.set_value("Item", "POS-E2E-GUN", "is_antique", 1, update_modified=False)
        if not frappe.db.exists("Customer", "_Test POS Buyer"):
            frappe.get_doc({
                "doctype": "Customer", "customer_name": "_Test POS Buyer",
                "customer_group": "Individual", "territory": "All Territories",
            }).insert(ignore_permissions=True)
        if not frappe.db.exists("Supplier", "_Test E2E Supplier"):
            frappe.get_doc({
                "doctype": "Supplier", "supplier_name": "_Test E2E Supplier",
                "supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
                    or "All Supplier Groups",
                "supplier_type": "Company",
            }).insert(ignore_permissions=True)
        cls.supplier = "_Test E2E Supplier"

    def _receive_and_stamp(self, serial: str, cost: float) -> str:
        from ffl_core.firearm import serialized_receipt_row
        if not frappe.db.exists("Serial No", serial):
            se = frappe.get_doc({
                "doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
                "company": self.company,
                "items": [serialized_receipt_row(
                    item_code="POS-E2E-GUN", qty=1, warehouse=self.warehouse,
                    basic_rate=cost, serial=serial)],
            })
            se.flags.ignore_permissions = True
            se.insert()
            se.submit()
        # Stamp the pay-on-sale marker (what the receive hook / backfill do).
        frappe.db.set_value("Serial No", serial, {
            "pay_on_sale": 1, "pay_on_sale_supplier": self.supplier,
            "srbnb_cost": cost, "purchase_rate": cost, "pay_on_sale_settled": 0,
        }, update_modified=False)
        return serial

    def _submit_invoice(self, *serials: str, net: float) -> "frappe.Document":
        # several serials = ONE qty>1 line carrying them all (web-order shape)
        si = frappe.new_doc("Sales Invoice")
        si.company = self.company
        si.customer = "_Test POS Buyer"
        si.append("items", {
            "item_code": "POS-E2E-GUN", "qty": len(serials), "rate": net,
            "warehouse": self.warehouse, "serial_no": "\n".join(serials),
            "use_serial_batch_fields": 1,
        })
        si.set_missing_values()
        si.flags.ignore_permissions = True
        si.insert()
        si.submit()
        return si

    def test_submit_settles_at_parked_cost_not_sale_price(self):
        # received at 400 (parked in SRBNB), sold at 1000: the settlement clears
        # the PARKED 400, never the sale price.
        serial = self._receive_and_stamp(self._testMethodName, cost=400.0)
        si = self._submit_invoice(serial, net=1000.0)
        self.assertEqual(si.docstatus, 1)

        je_name = frappe.db.get_value(
            "Journal Entry", {"ffl_pay_on_sale_source": si.name, "docstatus": 1})
        self.assertTrue(je_name)
        je = frappe.get_doc("Journal Entry", je_name)

        self.assertEqual(len(je.accounts), 2)
        dr = next(a for a in je.accounts if a.debit_in_account_currency)
        cr = next(a for a in je.accounts if a.credit_in_account_currency)
        self.assertEqual(dr.account, self.srbnb)
        self.assertAlmostEqual(dr.debit_in_account_currency, 400.0)   # parked cost, not 1000
        self.assertFalse(dr.party)
        self.assertEqual(cr.account, self.creditors)
        self.assertAlmostEqual(cr.credit_in_account_currency, 400.0)
        self.assertEqual(cr.party_type, "Supplier")
        self.assertEqual(cr.party, self.supplier)
        self.assertAlmostEqual(je.total_debit, je.total_credit)
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 1)

    def test_non_pay_on_sale_sale_posts_no_je(self):
        # wiring sanity: a plain (non-pay-on-sale) firearm sale through the SAME
        # on_submit hook must not create a settlement JE at all.
        from ffl_core.firearm import serialized_receipt_row
        serial = self._testMethodName
        se = frappe.get_doc({
            "doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
            "company": self.company,
            "items": [serialized_receipt_row(
                item_code="POS-E2E-GUN", qty=1, warehouse=self.warehouse,
                basic_rate=500, serial=serial)],
        })
        se.flags.ignore_permissions = True
        se.insert()
        se.submit()
        si = self._submit_invoice(serial, net=750.0)
        self.assertFalse(
            frappe.db.exists("Journal Entry", {"ffl_pay_on_sale_source": si.name}))

    def test_cancel_reverses_settlement(self):
        serial = self._receive_and_stamp(self._testMethodName, cost=600.0)
        si = self._submit_invoice(serial, net=1200.0)
        je_name = frappe.db.get_value(
            "Journal Entry", {"ffl_pay_on_sale_source": si.name, "docstatus": 1})
        self.assertTrue(je_name)

        si.cancel()

        self.assertEqual(frappe.db.get_value("Journal Entry", je_name, "docstatus"), 2)
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 0)

    def test_multi_serial_line_settles_every_serial_end_to_end(self):
        # The prod shape (a past invoice): a web-order fulfilment line with
        # qty=2 and both serials in the one row. Through the REAL on_submit hook,
        # each gun settles at ITS OWN parked cost — 400 + 655.25, not just 400.
        a = self._receive_and_stamp(self._testMethodName + "-A", cost=400.0)
        b = self._receive_and_stamp(self._testMethodName + "-B", cost=655.25)
        si = self._submit_invoice(a, b, net=1000.0)

        je = frappe.get_doc("Journal Entry", frappe.db.get_value(
            "Journal Entry", {"ffl_pay_on_sale_source": si.name, "docstatus": 1}))
        self.assertEqual(len(je.accounts), 4)  # one Dr/Cr pair per gun
        self.assertAlmostEqual(je.total_debit, 1055.25)
        self.assertAlmostEqual(je.total_debit, je.total_credit)
        self.assertEqual(
            sorted(flt(x.debit_in_account_currency) for x in je.accounts
                if x.account == self.srbnb), [400.0, 655.25])
        self.assertEqual(
            sorted(flt(x.credit_in_account_currency) for x in je.accounts
                if x.account == self.creditors), [400.0, 655.25])
        for serial in (a, b):
            self.assertEqual(
                int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 1,
                f"{serial} left unsettled")

    def _make_return(self, original_si, serial):
        ret = frappe.new_doc("Sales Invoice")
        ret.company = self.company
        ret.customer = original_si.customer
        ret.is_return = 1
        ret.return_against = original_si.name
        ret.append("items", {
            "item_code": "POS-E2E-GUN", "qty": -1, "rate": original_si.items[0].rate,
            "warehouse": self.warehouse, "serial_no": serial, "use_serial_batch_fields": 1,
        })
        ret.set_missing_values()
        ret.flags.ignore_permissions = True
        ret.insert()
        ret.submit()
        return ret

    def test_return_credit_note_reverses_settlement(self):
        # A separate return credit note (NOT a cancel of the original sale) must
        # reverse the settlement — the MAJOR-1 bug. The original settlement JE
        # stays; a NEW reversal JE (tagged with the return's name) offsets it.
        serial = self._receive_and_stamp(self._testMethodName, cost=400.0)
        si = self._submit_invoice(serial, net=1000.0)
        settle_je = frappe.db.get_value(
            "Journal Entry", {"ffl_pay_on_sale_source": si.name, "docstatus": 1})
        self.assertTrue(settle_je)
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 1)

        ret = self._make_return(si, serial)

        # original settlement JE untouched (still submitted); a distinct reversal JE
        # carries the return's name, and the serial is un-settled.
        self.assertEqual(frappe.db.get_value("Journal Entry", settle_je, "docstatus"), 1)
        rev_je = frappe.db.get_value(
            "Journal Entry", {"ffl_pay_on_sale_source": ret.name, "docstatus": 1})
        self.assertTrue(rev_je)
        self.assertNotEqual(rev_je, settle_je)
        rev = frappe.get_doc("Journal Entry", rev_je)
        dr = next(a for a in rev.accounts if a.debit_in_account_currency)
        self.assertEqual(dr.account, self.creditors)     # Dr Creditors (reverse the AP)
        self.assertAlmostEqual(dr.debit_in_account_currency, 400.0)
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale_settled")), 0)


class ReceiveStamp(FrappeTestCase):
    """A Purchase Receipt from a pay_on_sale supplier stamps the pay-on-sale
    marker onto each firearm serial at intake (compliance/purchase.py) — the
    'stamp at park time' half of the pay-on-sale flow. Mirrors test_consignment_capture.py's
    real-PR fixtures."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from ffl_core.setup import dev_site, pay_on_sale
        dev_site.run()
        pay_on_sale.ensure()
        cls.company = frappe.db.get_value("Company", {"company_name": COMPANY_NAME}, "name")
        frappe.defaults.set_user_default("Company", cls.company)
        if not frappe.db.exists("Supplier Group", "Distributor"):
            frappe.get_doc({"doctype": "Supplier Group", "supplier_group_name": "Distributor",
                "parent_supplier_group": "All Supplier Groups", "is_group": 0}
                ).insert(ignore_permissions=True)
        # a pay_on_sale FFL supplier + a plain FFL supplier (unique ffl_numbers —
        # the column is UNIQUE across the test DB)
        cls._supplier("POS-TESTVENDOR-FFL", ffl_number="1-88-888-88-8X-88881", pay_on_sale=1)
        cls._supplier("POS-NORMAL-FFL", ffl_number="1-88-888-88-8X-88882", pay_on_sale=0)
        if not frappe.db.exists("Item", "POS-RCV-GUN"):
            frappe.get_doc({"doctype": "Item", "item_code": "POS-RCV-GUN", "item_name": "POS-RCV-GUN",
                "item_group": "Pistol", "stock_uom": "Nos", "is_stock_item": 1,
                "has_serial_no": 1}).insert(ignore_permissions=True)
        cls.warehouse = frappe.db.get_value(
            "Warehouse", {"company": cls.company, "is_group": 0}, "name")

    @staticmethod
    def _supplier(name, *, ffl_number, pay_on_sale):
        fields = dict(is_ffl_dealer=1, ffl_number=ffl_number, ez_check_status="Verified",
            pay_on_sale=pay_on_sale)
        if frappe.db.exists("Supplier", name):
            for k, v in fields.items():
                frappe.db.set_value("Supplier", name, k, v, update_modified=False)
            return
        frappe.get_doc({"doctype": "Supplier", "supplier_name": name,
            "supplier_group": "Distributor", **fields}).insert(ignore_permissions=True)

    def _make_pr(self, supplier, serial, rate, **extra):
        pr = frappe.get_doc({
            "doctype": "Purchase Receipt", "supplier": supplier, "company": self.company,
            "items": [{"item_code": "POS-RCV-GUN", "qty": 1, "warehouse": self.warehouse,
                "serial_no": serial, "rate": rate}],
            **extra,
        })
        pr.flags.ignore_permissions = True
        pr.insert()
        pr.submit()
        return pr

    def test_pay_on_sale_supplier_stamps_serial_at_receive(self):
        serial = "POS-RCV-STAMP-1"
        self._make_pr("POS-TESTVENDOR-FFL", serial, rate=450.00)
        info = frappe.db.get_value("Serial No", serial,
            ["pay_on_sale", "pay_on_sale_supplier", "srbnb_cost", "pay_on_sale_settled"],
            as_dict=True)
        self.assertEqual(int(info.pay_on_sale), 1)
        self.assertEqual(info.pay_on_sale_supplier, "POS-TESTVENDOR-FFL")
        self.assertAlmostEqual(float(info.srbnb_cost), 450.00, places=2)  # = the parked cost
        self.assertEqual(int(info.pay_on_sale_settled), 0)  # unsettled until sold

    def test_normal_supplier_leaves_serial_unstamped(self):
        serial = "POS-RCV-PLAIN-1"
        self._make_pr("POS-NORMAL-FFL", serial, rate=450.00)
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale") or 0), 0)
        self.assertFalse(frappe.db.get_value("Serial No", serial, "pay_on_sale_supplier"))

    def test_consignment_receipt_does_not_stamp_pay_on_sale(self):
        # A consignment intake never gets a pay-on-sale stamp even if the PR's FFL
        # supplier happens to be pay_on_sale — the two models are mutually
        # exclusive (owned vs. not-owned), enforced by the if/elif at the stamp site.
        if not frappe.db.exists("Customer", "_Test POS Consignor"):
            frappe.get_doc({"doctype": "Customer", "customer_name": "_Test POS Consignor",
                "customer_group": "Individual", "territory": "All Territories"}
                ).insert(ignore_permissions=True)
        from ffl_core.setup import consignment
        consignment.ensure()
        serial = "POS-RCV-CONS-1"
        self._make_pr("POS-TESTVENDOR-FFL", serial, rate=450.00,
            ffl_acquisition_type="Consignment", ffl_acquisition_source="Individual",
            ffl_seller_customer="_Test POS Consignor",
            ffl_seller_id_type="Driver License", ffl_seller_id_number="DL-POS-C1")
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "is_consignment")), 1)
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale") or 0), 0)


class Backfill(FrappeTestCase):
    """The manual (UNregistered) backfill labels existing in-stock guns from
    pay_on_sale suppliers, resolving the supplier per gun off its FFL Acquisition,
    with ZERO GL (SRBNB is already parked). Idempotent; leaves an unresolvable gun
    untagged (fail closed at sale)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from ffl_core.setup import dev_site, pay_on_sale
        dev_site.run()
        pay_on_sale.ensure()
        cls.company = frappe.db.get_value("Company", {"company_name": COMPANY_NAME}, "name")
        frappe.defaults.set_user_default("Company", cls.company)
        cls.warehouse = frappe.db.get_value(
            "Warehouse", {"company": cls.company, "is_group": 0}, "name")
        if not frappe.db.exists("Supplier Group", "Distributor"):
            frappe.get_doc({"doctype": "Supplier Group", "supplier_group_name": "Distributor",
                "parent_supplier_group": "All Supplier Groups", "is_group": 0}
                ).insert(ignore_permissions=True)
        cls._supplier("BF-POS-FFL", ffl_number="1-99-999-99-9X-99991", pay_on_sale=1)
        cls._supplier("BF-NORM-FFL", ffl_number="1-99-999-99-9X-99992", pay_on_sale=0)
        if not frappe.db.exists("Item", "BF-GUN"):
            frappe.get_doc({"doctype": "Item", "item_code": "BF-GUN", "item_name": "BF-GUN",
                "item_group": "Pistol", "stock_uom": "Nos", "is_stock_item": 1,
                "has_serial_no": 1}).insert(ignore_permissions=True)

    @staticmethod
    def _supplier(name, *, ffl_number, pay_on_sale):
        fields = dict(is_ffl_dealer=1, ffl_number=ffl_number, ez_check_status="Verified",
            pay_on_sale=pay_on_sale, supplier_group="Distributor")
        if frappe.db.exists("Supplier", name):
            for k, v in fields.items():
                frappe.db.set_value("Supplier", name, k, v, update_modified=False)
            return
        frappe.get_doc({"doctype": "Supplier", "supplier_name": name, **fields}
            ).insert(ignore_permissions=True)

    def _receive(self, supplier, serial, rate):
        pr = frappe.get_doc({
            "doctype": "Purchase Receipt", "supplier": supplier, "company": self.company,
            "items": [{"item_code": "BF-GUN", "qty": 1, "warehouse": self.warehouse,
                "serial_no": serial, "rate": rate}],
        })
        pr.flags.ignore_permissions = True
        pr.insert()
        pr.submit()
        return pr

    def _clear_stamp(self, serial):
        # simulate a legacy gun received BEFORE feature #3 (no receive-stamp)
        frappe.db.set_value("Serial No", serial, {
            "pay_on_sale": 0, "pay_on_sale_supplier": "", "srbnb_cost": 0,
            "pay_on_sale_settled": 0}, update_modified=False)

    def test_resolve_supplier_via_purchase_receipt(self):
        from ffl_core.patches.backfill_pay_on_sale_serials import resolve_pay_on_sale_supplier
        serial = "BF-RES-1"
        self._receive("BF-POS-FFL", serial, 450.00)
        self.assertEqual(resolve_pay_on_sale_supplier(serial), "BF-POS-FFL")

    def test_resolve_returns_none_for_non_pay_on_sale_supplier(self):
        from ffl_core.patches.backfill_pay_on_sale_serials import resolve_pay_on_sale_supplier
        serial = "BF-RES-2"
        self._receive("BF-NORM-FFL", serial, 450.00)
        self.assertIsNone(resolve_pay_on_sale_supplier(serial))

    def test_backfill_stamps_legacy_gun(self):
        from ffl_core.patches.backfill_pay_on_sale_serials import execute
        serial = "BF-STAMP-1"
        self._receive("BF-POS-FFL", serial, 450.00)
        self._clear_stamp(serial)  # legacy state

        execute()

        info = frappe.db.get_value("Serial No", serial,
            ["pay_on_sale", "pay_on_sale_supplier", "srbnb_cost", "pay_on_sale_settled"],
            as_dict=True)
        self.assertEqual(int(info.pay_on_sale), 1)
        self.assertEqual(info.pay_on_sale_supplier, "BF-POS-FFL")
        self.assertAlmostEqual(float(info.srbnb_cost), 450.00, places=2)
        self.assertEqual(int(info.pay_on_sale_settled), 0)

    def test_backfill_posts_zero_gl(self):
        from ffl_core.patches.backfill_pay_on_sale_serials import execute
        serial = "BF-ZEROGL-1"
        self._receive("BF-POS-FFL", serial, 450.00)
        self._clear_stamp(serial)
        gl_before = frappe.db.count("GL Entry")
        sle_before = frappe.db.count("Stock Ledger Entry")
        execute()
        # label-only: no Journal Entry, no stock movement
        self.assertEqual(frappe.db.count("GL Entry"), gl_before)
        self.assertEqual(frappe.db.count("Stock Ledger Entry"), sle_before)

    def test_backfill_is_idempotent(self):
        from ffl_core.patches.backfill_pay_on_sale_serials import execute
        serial = "BF-IDEM-1"
        self._receive("BF-POS-FFL", serial, 450.00)
        self._clear_stamp(serial)
        execute()
        # already stamped now → a second run must not re-stamp THIS serial
        res2 = execute()
        self.assertNotIn(serial, [
            r.name for r in frappe.get_all("Serial No",
                filters={"name": serial, "pay_on_sale": 0})])
        # and the stamp is unchanged
        self.assertAlmostEqual(
            float(frappe.db.get_value("Serial No", serial, "srbnb_cost")), 450.00, places=2)

    def test_backfill_skips_consignment_serial(self):
        from ffl_core.patches.backfill_pay_on_sale_serials import execute
        serial = "BF-CONS-1"
        self._receive("BF-POS-FFL", serial, 450.00)
        self._clear_stamp(serial)
        frappe.db.set_value("Serial No", serial, "is_consignment", 1, update_modified=False)
        try:
            execute()
            self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale") or 0), 0)
        finally:
            frappe.db.set_value("Serial No", serial, "is_consignment", 0, update_modified=False)

    def test_backfill_skips_zero_purchase_rate(self):
        # no determinable SRBNB-parked amount → leave untagged for manual review
        from ffl_core.patches.backfill_pay_on_sale_serials import execute
        serial = "BF-ZEROCOST-1"
        self._receive("BF-POS-FFL", serial, 450.00)
        self._clear_stamp(serial)
        frappe.db.set_value("Serial No", serial, "purchase_rate", 0, update_modified=False)
        execute()
        self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale") or 0), 0)


if __name__ == "__main__":
    unittest.main()
