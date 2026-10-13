"""Sports South firearm fulfillment email: the licence copy that must follow every
firearm order, what it says, who it goes to, and every case where it is NOT sent
(and what the shop is told instead). No site rows: frappe's reads, the mailer and
the admin alert are stubbed at the module's own seams."""
from __future__ import annotations

import unittest
from unittest import mock

import frappe

from ffl_integrations.sports_south import ffl_email as fe


def _order(**over):
	do = frappe._dict(
		name="DO-0001", order_type="Dropship Firearm", web_ref="", notes_posted="",
		transfer_ffl_record="FFL-REC-1", transfer_ffl_license="5-99-888-01-1A-00001",
		transfer_ffl_premise_street="1 Main St", transfer_ffl_premise_city="Springfield",
		transfer_ffl_premise_state="TX", transfer_ffl_premise_zip="70001")
	do.update(over)
	do.add_comment = mock.MagicMock()
	return do


def _settings(**over):
	base = dict(customer_number="100001", fulfillment_email="fulfillment@example.test",
		order_email_cc="")
	base.update(over)
	return frappe._dict(base)


_RECORD = frappe._dict(licensee_name="JONES ARMS LLC", licensee_dba="Jones Arms", phone="9725550100")


class _Send(unittest.TestCase):
	"""Run `send_ffl_email` with its IO stubbed; `self.mail`, `self.alert` and
	`self.marked` record what it did."""

	def send(self, do=None, settings=None, files=None, record=_RECORD):
		do = do or _order()
		self.do = do
		with mock.patch.object(fe.frappe, "get_doc", return_value=do), \
				mock.patch.object(fe.frappe, "get_cached_doc", return_value=settings or _settings()), \
				mock.patch.object(fe, "_ffl_attachments", return_value=[{"file_url": "/files/ffl.pdf"}]
					if files is None else files), \
				mock.patch.object(fe.frappe.db, "get_value", return_value=record), \
				mock.patch.object(fe.frappe.db, "set_value") as marked, \
				mock.patch.object(fe.frappe, "sendmail") as mail, \
				mock.patch.object(fe, "notify_admins") as alert:
			out = fe.send_ffl_email(do.name)
		self.mail, self.alert, self.marked = mail, alert, marked
		return out


class TestMessage(unittest.TestCase):
	def _message(self, do, record):
		with mock.patch.object(fe.frappe.db, "get_value", return_value=record):
			return fe.build_message(do, _settings())

	def test_subject_is_our_account_number_and_the_po(self):
		m = self._message(_order(), _RECORD)
		self.assertEqual(m["subject"], "100001 DO-0001")

	def test_body_names_the_transfer_dealer_by_trade_name_with_phone_and_licence(self):
		m = self._message(_order(), _RECORD)
		self.assertEqual((m["dealer_name"], m["phone"]), ("Jones Arms", "9725550100"))
		for line in ("Transfer FFL: 5-99-888-01-1A-00001", "Transfer dealer: Jones Arms",
				"Contact phone: 9725550100", "Premises: 1 Main St, Springfield, TX 70001"):
			self.assertIn(line, m["message"])

	def test_the_licensee_name_stands_in_when_there_is_no_trade_name(self):
		m = self._message(_order(), frappe._dict(_RECORD, licensee_dba=""))
		self.assertEqual(m["dealer_name"], "JONES ARMS LLC")

	def test_the_web_order_reference_rides_on_the_po_line(self):
		m = self._message(_order(web_ref="SS-778"), _RECORD)
		self.assertIn("PO: DO-0001 (SS web order SS-778)", m["message"])
		self.assertIn("PO: DO-0001\n", self._message(_order(), _RECORD)["message"])

	def test_an_order_with_no_ffl_record_still_builds_with_blank_contact_details(self):
		with mock.patch.object(fe.frappe.db, "get_value") as get_value:
			m = fe.build_message(_order(transfer_ffl_record=None), _settings())
		get_value.assert_not_called()
		self.assertEqual((m["dealer_name"], m["phone"]), ("", ""))


class TestSend(_Send):
	def test_sends_one_email_with_the_licence_copy_and_marks_the_order(self):
		out = self.send()
		self.assertEqual(out, {"sent": True, "to": "fulfillment@example.test", "subject": "100001 DO-0001"})
		kwargs = self.mail.call_args.kwargs
		self.assertEqual(kwargs["recipients"], ["fulfillment@example.test"])
		self.assertIsNone(kwargs["cc"])
		self.assertEqual(kwargs["subject"], "100001 DO-0001")
		self.assertEqual(kwargs["attachments"], [{"file_url": "/files/ffl.pdf"}])
		self.assertEqual((kwargs["reference_doctype"], kwargs["reference_name"]),
			("Distributor Order", "DO-0001"))
		self.assertNotIn("\n", kwargs["message"])                 # newlines become <br>
		self.assertIn("<br>", kwargs["message"])
		self.marked.assert_called_once_with("Distributor Order", "DO-0001", "notes_posted",
			"ss_ffl_email", update_modified=False)
		self.do.add_comment.assert_called_once()
		self.alert.assert_not_called()

	def test_the_configured_cc_is_copied(self):
		self.send(settings=_settings(order_email_cc="desk@example.test"))
		self.assertEqual(self.mail.call_args.kwargs["cc"], ["desk@example.test"])

	def test_the_mark_keeps_the_order_notes_marks_already_there(self):
		self.send(do=_order(notes_posted="shipped,a_mark"))
		self.assertEqual(self.marked.call_args.args[3], "a_mark,shipped,ss_ffl_email")

	def test_a_second_run_does_not_send_again(self):
		out = self.send(do=_order(notes_posted="x,ss_ffl_email"))
		self.assertEqual(out, {"sent": False, "reason": "already sent"})
		self.mail.assert_not_called()
		self.marked.assert_not_called()

	def test_only_firearm_lane_orders_get_the_email(self):
		out = self.send(do=_order(order_type="Dropship Accessory"))
		self.assertEqual(out, {"sent": False, "reason": "not a firearm order"})
		self.mail.assert_not_called()

	def test_no_fulfillment_address_alerts_instead_of_sending(self):
		out = self.send(settings=_settings(fulfillment_email="  "))
		self.assertEqual(out, {"sent": False, "reason": "no recipient configured"})
		self.mail.assert_not_called()
		self.alert.assert_called_once()
		self.assertEqual(self.alert.call_args.kwargs["dedup_key"], "ss_ffl_mail_cfg_DO-0001")

	def test_no_licence_copy_tells_the_shop_to_send_it_by_hand_and_marks_nothing(self):
		out = self.send(files=[])
		self.assertEqual(out, {"sent": False, "reason": "no ffl attachment"})
		self.mail.assert_not_called()
		self.marked.assert_not_called()               # so the next run tries again
		self.do.add_comment.assert_called_once()
		note = self.do.add_comment.call_args.args[1]
		self.assertIn("fulfillment@example.test", note)
		self.assertIn("100001 DO-0001", note)
		self.alert.assert_called_once()
		self.assertEqual(self.alert.call_args.kwargs["dedup_key"], "ss_ffl_mail_nofile_DO-0001")


class TestAttachments(unittest.TestCase):
	def _find(self, record_files, license_no="5-99-888-01-1A-00001", record="FFL-REC-1",
			doc_url=None, has_field=True):
		meta = mock.MagicMock()
		meta.get_field.return_value = has_field
		with mock.patch.object(fe.frappe, "get_all", return_value=record_files), \
				mock.patch.object(fe.frappe, "get_meta", return_value=meta), \
				mock.patch.object(fe.frappe.db, "get_value", return_value=doc_url) as get_value:
			out = fe._ffl_attachments(record, license_no)
		return out, get_value

	def test_only_documents_and_images_on_the_record_are_attached(self):
		files = [frappe._dict(file_url="/files/a.pdf", file_name="a.PDF"),
			frappe._dict(file_url="/files/b.xlsx", file_name="b.xlsx"),
			frappe._dict(file_url="/files/c.jpeg", file_name="c.jpeg")]
		out, get_value = self._find(files)
		self.assertEqual(out, [{"file_url": "/files/a.pdf"}, {"file_url": "/files/c.jpeg"}])
		get_value.assert_not_called()

	def test_falls_back_to_the_licence_copy_stored_on_the_supplier_or_customer(self):
		out, get_value = self._find([], doc_url="/private/files/ffl.pdf")
		self.assertEqual(out, [{"file_url": "/private/files/ffl.pdf"}])
		self.assertEqual(get_value.call_args.args[:2], ("Supplier", {"ffl_number": "5-99-888-01-1A-00001"}))

	def test_a_party_doctype_without_the_attach_field_is_not_asked(self):
		out, get_value = self._find([], has_field=False)
		self.assertEqual(out, [])
		get_value.assert_not_called()

	def test_without_a_licence_number_there_is_nothing_to_fall_back_to(self):
		out, get_value = self._find([], license_no=None)
		self.assertEqual(out, [])
		get_value.assert_not_called()


class TestEnqueue(unittest.TestCase):
	def test_enqueues_the_sender_by_a_dotted_name_that_resolves(self):
		with mock.patch.object(fe.frappe, "enqueue") as enqueue:
			fe.enqueue_ffl_email("DO-0001")
		path = enqueue.call_args.args[0]
		self.assertEqual(frappe.get_attr(path), fe.send_ffl_email)
		self.assertEqual(enqueue.call_args.kwargs["do_name"], "DO-0001")
		self.assertEqual(enqueue.call_args.kwargs["job_name"], "ss_ffl_email_DO-0001")


if __name__ == "__main__":
	unittest.main()
