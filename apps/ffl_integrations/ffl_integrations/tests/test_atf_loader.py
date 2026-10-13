"""ATF CSV loader tests — uses an inline 3-row fixture, no network."""
from __future__ import annotations

import csv
import io
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.atf import search as atf_search
from ffl_integrations.atf.loader import load_ffl_csv
from ffl_integrations.atf.search import search


# Mirror of the ATF CSV header (LIC_REGN, LIC_DIST, LIC_CNTY, LIC_TYPE, LIC_XPRDTE,
# LIC_SEQN, LICENSE_NAME, BUSINESS_NAME, PREMISE_STREET, PREMISE_CITY, PREMISE_STATE,
# PREMISE_ZIP_CODE, MAIL_STREET, MAIL_CITY, MAIL_STATE, MAIL_ZIP_CODE, VOICE_PHONE).
# LIC_XPRDTE carries the 2-char expiration code that is segment 5 of the licence
# number ("8L"), NOT a YYYYMM date — see the 0726 national file.
HEADER = (
	"LIC_REGN,LIC_DIST,LIC_CNTY,LIC_TYPE,LIC_XPRDTE,LIC_SEQN,LICENSE_NAME,BUSINESS_NAME,"
	"PREMISE_STREET,PREMISE_CITY,PREMISE_STATE,PREMISE_ZIP_CODE,MAIL_STREET,MAIL_CITY,"
	"MAIL_STATE,MAIL_ZIP_CODE,VOICE_PHONE"
)

ROWS = """\
1,99,888,01,8L,12345,SMITH JOHN,JOHN'S GUNS,123 MAIN ST,ANYTOWN,TX,75001,123 MAIN ST,ANYTOWN,TX,75001,5551234567
1,99,888,07,7A,67890,ACME GUNSMITHING LLC,ACME,500 OAK AVE,DALLAS,TX,75002,500 OAK AVE,DALLAS,TX,75002,5559876543
6,04,123,01,6C,11111,WESTERN OUTDOORS,WESTERN OUTDOORS LLC,1 RIVER RD,BOISE,ID,83702,1 RIVER RD,BOISE,ID,83702,2085551111
"""

SAMPLE_CSV = f"{HEADER}\n{ROWS}"

# The 0726 national file shipped the licensee-name header as a bare space.
BLANK_NAME_HEADER_CSV = f"{HEADER.replace(',LICENSE_NAME,', ', ,')}\n{ROWS}"

# Legacy files carried a 6-digit YYYYMM in LIC_XPRDTE and no embedded code.
LEGACY_CSV = f"""{HEADER}
1,99,888,01,202812,12345,SMITH JOHN,JOHN'S GUNS,123 MAIN ST,ANYTOWN,TX,75001,123 MAIN ST,ANYTOWN,TX,75001,5551234567
"""

LOADED_FFLS = (
	"1-99-888-01-8L-12345",
	"1-99-888-07-7A-67890",
	"6-04-123-01-6C-11111",
	# The placeholder spellings: one still produced by the legacy-file test,
	# the rest left behind on any site that ran the pre-fix suite.
	"1-99-888-01-XX-12345",
	"1-99-888-07-XX-67890",
	"6-04-123-01-XX-11111",
)


def _write_tmp(content: str, encoding: str = "utf-8") -> str:
	import tempfile, os
	fd, path = tempfile.mkstemp(suffix=".csv")
	with os.fdopen(fd, "w", encoding=encoding) as f:
		f.write(content)
	return path


class ATFLoader(FrappeTestCase):
	def setUp(self):
		# Wipe any prior test rows so each test starts clean.
		frappe.db.sql("SET SQL_SAFE_UPDATES=0")
		for ffl in LOADED_FFLS:
			frappe.db.delete("ATF FFL Record", {"ffl_number": ffl})
		frappe.db.commit()
		# ...and keep the "no network" in this module's docstring true. search()
		# merges the shared Data Service's national listing over the local cache
		# whenever FFL Settings.live_check_provider is "Data Service" — which it IS
		# on a configured site — so the name searches below counted 19 real dealers
		# from the service on top of their own fixture, and the suite's result
		# depended on a remote call. Local-only is what these two tests are about;
		# the merge has its own coverage in test_data_service_provider.py.
		patcher = patch.object(atf_search, "_remote_rows", return_value=None)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_loads_three_rows(self):
		path = _write_tmp(SAMPLE_CSV)
		stats = load_ffl_csv(path)
		self.assertEqual(stats["created"], 3)
		self.assertEqual(stats["total"], 3)
		self.assertTrue(frappe.db.exists("ATF FFL Record", "1-99-888-01-8L-12345"))

	def test_expiration_code_is_segment_5_of_the_licence_number(self):
		"""The CSV publishes the real code; EZ Check returns numbers carrying it,
		so a placeholder here would fork every dealer into two records."""
		load_ffl_csv(_write_tmp(SAMPLE_CSV))
		self.assertFalse(frappe.db.exists("ATF FFL Record", "1-99-888-01-XX-12345"))
		self.assertTrue(frappe.db.exists("ATF FFL Record", "1-99-888-07-7A-67890"))

	def test_rerun_is_idempotent(self):
		path = _write_tmp(SAMPLE_CSV)
		load_ffl_csv(path)
		stats = load_ffl_csv(path)
		self.assertEqual(stats["created"], 0)
		self.assertEqual(stats["updated"], 3)
		# Counted by the sample's own numbers: other fixtures share the 1-99-888 pattern.
		self.assertEqual(frappe.db.count("ATF FFL Record", {"ffl_number": (
			"in", ["1-99-888-01-8L-12345", "1-99-888-07-7A-67890"])}), 2)

	def test_search_by_exact_ffl_number(self):
		load_ffl_csv(_write_tmp(SAMPLE_CSV))
		hits = search("1-99-888-01-8L-12345")
		self.assertEqual(len(hits), 1)
		self.assertEqual(hits[0]["licensee_name"], "SMITH JOHN")

	def test_search_by_name_like(self):
		load_ffl_csv(_write_tmp(SAMPLE_CSV))
		hits = search("ACME")
		self.assertEqual(len(hits), 1)
		self.assertEqual(hits[0]["ffl_number"], "1-99-888-07-7A-67890")

	def test_utf8_bom_does_not_swallow_the_region_digit(self):
		"""ATF ships the national CSV with a BOM; read as plain utf-8 it lands on
		the LIC_REGN header and every licence number loses its first segment."""
		load_ffl_csv(_write_tmp(SAMPLE_CSV, encoding="utf-8-sig"))
		self.assertTrue(frappe.db.exists("ATF FFL Record", "1-99-888-01-8L-12345"))

	def test_blank_name_header_falls_back_to_column_position(self):
		"""0726 shipped LICENSE_NAME as a bare space. 32k licensees have no
		BUSINESS_NAME, so dropping the column leaves them nameless."""
		load_ffl_csv(_write_tmp(BLANK_NAME_HEADER_CSV))
		self.assertEqual(
			frappe.db.get_value("ATF FFL Record", "1-99-888-01-8L-12345", "licensee_name"),
			"SMITH JOHN",
		)

	def test_legacy_yyyymm_file_still_loads_with_a_placeholder(self):
		load_ffl_csv(_write_tmp(LEGACY_CSV))
		self.assertTrue(frappe.db.exists("ATF FFL Record", "1-99-888-01-XX-12345"))
		self.assertEqual(
			str(frappe.db.get_value("ATF FFL Record", "1-99-888-01-XX-12345", "expiry_date")),
			"2028-12-28",
		)

	def test_csv_blanks_do_not_wipe_live_ez_check_data(self):
		"""The CSV has no real expiry date and often no phone; a monthly reload
		must not erase what an EZ Check lookup established."""
		load_ffl_csv(_write_tmp(SAMPLE_CSV))
		frappe.db.set_value("ATF FFL Record", "1-99-888-01-8L-12345",
			{"expiry_date": "2028-11-01"}, update_modified=False)
		frappe.db.commit()

		blank_phone = SAMPLE_CSV.replace(",5551234567", ",")
		load_ffl_csv(_write_tmp(blank_phone))

		row = frappe.db.get_value("ATF FFL Record", "1-99-888-01-8L-12345",
			["expiry_date", "phone"], as_dict=True)
		self.assertEqual(str(row.expiry_date), "2028-11-01")
		self.assertEqual(row.phone, "5551234567")


if __name__ == "__main__":
	unittest.main()
