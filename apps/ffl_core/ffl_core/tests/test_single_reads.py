"""The ``ffl_core.utils`` Singles readers, driven against the real ``tabSingles``.

Every settings switch in the three apps resolves through this one family:
``single_flag`` (the FastBound and Payroc gates, ``paper_4473_mode``,
``firearm.is_own_license``), ``single_flags`` (the GunBroker listing payload),
``single_raw`` (four migrate patches and the test sandbox itself). They are
hand-written SQL against a FRAMEWORK table — the one shape the offline harness
structurally cannot check, because a stubbed ``frappe.db.sql`` never validates a
column name (``docs/testing.md`` rule 7).

That gap has been paid for once. An ``ORDER BY name`` shipped on both readers at
``osa/gunbroker-listing`` @ f9f49da6 — 230 offline tests green, three code
reviews clean — and on a real site every settings read raised
``OperationalError (1054, "Unknown column 'name' in 'ORDER BY'")``: 715 red
tests, a ``bench migrate`` that failed even with ``--skip-failing`` (the
``after_migrate`` hook reads the same helper), and a FastBound gate that threw on
every ``FFL Acquisition`` submit — i.e. on every gun received.

So this module is the tripwire for that class of defect, and its subject is the
FRAMEWORK TABLE — the part no other test owns:

* ``tabSingles`` carries three columns and nothing orderable
  (:class:`SinglesTableFacts`), which is why no reader may order it;
* its writers leave at most one row per field, which is why "which duplicate row
  wins" is a question the readers are allowed not to answer;
* the batched reader and the per-field reader agree, over every raw state a
  field can be in — one query is an optimisation, a second set of semantics is a
  bug.

**Not here:** ``single_flag``'s tri-state (missing row / empty string / explicit
0-or-1 against a docfield default) belongs to
``test_compliance_mode.SingleFlagReads``, which already drives all three states
through ``paper_4473_mode``. This file keeps one representative of that
semantics — inside ``test_single_flags_is_single_flag_for_every_key``, where it
is load-bearing for the equivalence claim rather than a second copy of the
tri-state suite.

A Frappe upgrade that changes either framework fact should turn this file red
and make somebody re-read :mod:`ffl_core.utils`.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.settings_sandbox import SettingsSandbox
from ffl_core.utils import single_flag, single_flags, single_raw, single_raw_map


FFL_SETTINGS = "FFL Settings"

# Not a real DocType, deliberately. These readers take the doctype as a plain
# string and never consult `frappe.get_meta`, so a made-up name exercises exactly
# the same code path — and lets one test stage all four raw states at once
# without writing to a live switch. `settings_sandbox` explains at length why
# leaking a real one is poison: the failure lands in whatever module runs next.
PROBE = "ffl_core Singles Read Probe"


class SinglesTableFacts(SettingsSandbox, FrappeTestCase):
	"""The framework guarantees the readers are allowed to rely on."""

	def test_tabsingles_has_no_column_to_order_by(self):
		"""Three columns, no key, nothing to sort on — the fact an earlier version broke.

		``tabSingles`` is ``(doctype, field, value)`` with a single non-unique
		index on ``(doctype, field)``: no ``name``, no ``creation``, no
		``modified``, no primary key. So "make the two readers pick the same row
		by ordering the query" is not a thing that can be written, and neither of
		Frappe's own readers tries (``get_single_value`` and ``get_singles_dict``
		both run unordered). Any ``ORDER BY`` added to a ``tabSingles`` query is a
		``1054`` waiting for the next migrate.

		If a Frappe upgrade adds a column here, this goes red on purpose: go
		re-read the fold rule on ``single_raw_map`` before relying on it.

		Asked of the SERVER, not of ``frappe.db.get_table_columns`` — that one
		memoises on a ``table_columns::tabSingles`` cache key, and a pin that can be
		satisfied by a stale cache is not a pin. The whole value of this assertion
		is on the day the real schema changes, which is exactly the day a cached
		answer would be wrong.
		"""
		columns = {row[0] for row in frappe.db.sql("SHOW COLUMNS FROM `tabSingles`")}
		self.assertEqual(
			columns,
			{"doctype", "field", "value"},
			"tabSingles schema changed — re-read the fold rule in ffl_core.utils")

	def test_the_framework_keeps_at_most_one_row_per_field(self):
		"""Why "which duplicate wins" is not a question the readers must answer.

		Both framework writers are DELETE-then-INSERT —
		``frappe.db.set_single_value`` deletes the fields it is about to write, and
		``Document.update_single`` deletes the whole doctype's rows before
		re-inserting. Neither can append a second row for a field, so the invariant
		the readers assume is enforced upstream rather than asserted downstream.

		Pinned here because it is the ONLY thing standing between the readers and
		an undecidable question: with no orderable column (see above), a duplicate
		row could not be resolved deterministically even if we wanted to.

		``set_single`` is the sandbox's wrapper around ``set_single_value`` — the
		writer under test — so the two writes both exercise it and both get put
		back on teardown.

		The field is the Check ``paper_4473_mode``, not a Data field carrying a
		value something else acts on: an earlier cut wrote junk into ``atf_csv_url``,
		which is the URL the ATF loader downloads from. A flag flipped and put back
		inside the sandbox is inert; a URL is not, and "the row count is right" is
		not worth handing another module a bad endpoint if a commit ever lands
		mid-test. ``test_compliance_mode.SingleFlagReads`` already drives this same
		field through the sandbox three times over.
		"""
		self.set_single(FFL_SETTINGS, "paper_4473_mode", 0)
		self.set_single(FFL_SETTINGS, "paper_4473_mode", 1)

		count = frappe.db.sql(
			"SELECT COUNT(*) FROM `tabSingles` WHERE `doctype` = %s AND `field` = %s",
			(FFL_SETTINGS, "paper_4473_mode"))[0][0]
		self.assertEqual(count, 1,
			"a Single writer started appending rows — ffl_core.utils folds on the"
			" assumption that it cannot")


class SingleReadersOnARealSingle(SettingsSandbox, FrappeTestCase):
	"""The readers over rows the FRAMEWORK wrote, on a Single that ships."""

	def test_single_raw_returns_the_stored_value_and_none_for_a_row_never_written(self):
		"""The distinction the whole family exists for.

		``None`` (no row) and ``""`` (a row holding nothing) are different states,
		and ``frappe.db.get_single_value`` cannot tell them apart for a Check. Four
		migrate patches decide whether to seed a default by asking exactly this
		question, so getting it wrong re-runs a seed over an operator's choice.
		"""
		self.set_single(FFL_SETTINGS, "own_ffl_license", "1-23-456-78-9A-01234")
		self.assertEqual(single_raw(FFL_SETTINGS, "own_ffl_license"), "1-23-456-78-9A-01234")

		self.clear_single(FFL_SETTINGS, "own_ffl_license")
		self.assertIsNone(single_raw(FFL_SETTINGS, "own_ffl_license"))

	def test_single_raw_map_holds_the_field_that_was_saved_and_not_the_one_cleared(self):
		"""The batched reader, over rows written by ``set_single_value``.

		A key MISSING from this dict is the signal — it means nobody has stated a
		value for that field — so a reader that quietly materialised every field
		would destroy the same distinction ``single_raw`` protects.
		"""
		self.set_single(FFL_SETTINGS, "own_ffl_license", "1-23-456-78-9A-01234")
		self.clear_single(FFL_SETTINGS, "paper_4473_mode")

		stored = single_raw_map(FFL_SETTINGS)
		self.assertEqual(stored.get("own_ffl_license"), "1-23-456-78-9A-01234")
		self.assertNotIn("paper_4473_mode", stored,
			"a cleared field must be ABSENT, not present-and-empty")


class SingleReadersOverEveryRawState(FrappeTestCase):
	"""All four raw states at once, staged directly in ``tabSingles``.

	Written with SQL rather than through a real Single because no shipped Single
	has enough Check fields to stage "saved 1 / saved 0 / saved empty / never
	saved" side by side — and because the batched readers' contract is about the
	ROW SET, not about any particular doctype.
	"""

	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.sql,
			"DELETE FROM `tabSingles` WHERE `doctype` = %s", (PROBE,))
		for field, value in (("stored_on", "1"), ("stored_off", "0"), ("stored_blank", "")):
			frappe.db.sql(
				"INSERT INTO `tabSingles` (`doctype`, `field`, `value`) VALUES (%s, %s, %s)",
				(PROBE, field, value))

	def test_single_raw_map_returns_only_the_saved_fields(self):
		self.assertEqual(
			single_raw_map(PROBE),
			{"stored_on": "1", "stored_off": "0", "stored_blank": ""})

	def test_single_flags_is_single_flag_for_every_key(self):
		"""The promise in ``single_flags``' own docstring, held to.

		The batched form runs a different query from the per-field form, so the two
		can drift apart — over the fold direction, over blank handling, over
		whether a missing key means "off" or "default". One query is an
		optimisation; a second set of semantics is a bug.
		"""
		defaults = {"stored_on": 0, "stored_off": 1, "stored_blank": 1, "never_saved": 1}
		self.assertEqual(
			single_flags(PROBE, defaults),
			{field: single_flag(PROBE, field, default=default)
				for field, default in defaults.items()})
		self.assertEqual(
			single_flags(PROBE, defaults),
			{"stored_on": True, "stored_off": False, "stored_blank": True,
				"never_saved": True})
