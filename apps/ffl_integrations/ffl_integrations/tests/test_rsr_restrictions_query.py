"""RSR ship-restriction query layer.

This is the file that decides what an operator is TOLD about a destination, and
the one place the "display, never a gate" rule is enforced. Three properties are
load-bearing and each has its own class:

* **union across levels** — a city ban is not cancelled by a permissive state;
* **unknown codes fail CLOSED** — all eight live codes are documented by the
  CURRENT layout revision, so the undocumented set is presently empty;
  the branch stays because RSR has already revised that document once, and a
  ninth code must never arrive as "no restriction";
* **county is a third answer** — Woo gives us city/state/zip and no county, so a
  county row can be neither confirmed nor discarded.

Two traps have their own classes because both produce plausible-looking wrong
output: ``S`` means different things in column 4 and column 5, and the permit
family (H/O/S/R) must DISPLAY without BLOCKING — they say the recipient needs a
permit, not that RSR refuses to drop-ship.

Everything here is pure: rows in, verdict out, no DB. The DB layer that feeds it
is one ``frappe.get_all`` and is tested against a real site.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ffl_integrations.rsr import restrictions
from ffl_integrations.tests import catalog_stub


def _row(detail, *, territory="S", municipality="ALL", state="IL"):
	"""One raw restriction line as the doctype stores it."""
	return {
		"municipality": municipality,
		"state": state,
		"territory_type": territory,
		"restriction_detail": detail,
	}


class UnionAcrossLevels(unittest.TestCase):
	"""The three levels are OR-ed. A permissive level never clears a
	restrictive one — that is the whole reason we keep all three."""

	def test_city_hit_blocks_even_when_the_state_has_no_row(self):
		"""The headline case. CHICAGO HEIGHTS bans drop-ship; Illinois as a whole
		does not. Answering "IL is fine" would drop-ship into a city that refuses
		it — the exact failure the union exists to prevent."""
		rows = [_row("F", territory="C", municipality="CHICAGO HEIGHTS")]
		out = restrictions.evaluate(rows, state="IL", city="Chicago Heights")
		self.assertTrue(out["blocked_dropship"])
		self.assertEqual(out["level"], "city")
		self.assertEqual(out["codes"], ["F"])

	def test_state_hit_blocks_a_city_with_no_row_of_its_own(self):
		rows = [_row("F", territory="S", municipality="ALL", state="AL")]
		out = restrictions.evaluate(rows, state="AL", city="Mobile")
		self.assertTrue(out["blocked_dropship"])
		self.assertEqual(out["level"], "state")

	def test_level_reports_the_most_specific_hit_but_codes_are_the_union(self):
		"""Both levels hit: the operator needs the specific one for the headline
		and the union for the detail list, so neither may swallow the other."""
		rows = [
			_row("F", territory="S", municipality="ALL"),
			_row("L", territory="C", municipality="CHICAGO HEIGHTS"),
		]
		out = restrictions.evaluate(rows, state="IL", city="CHICAGO HEIGHTS")
		self.assertEqual(out["level"], "city")
		self.assertEqual(out["levels"], ["city", "state"])
		self.assertEqual(out["codes"], ["F", "L"])

	def test_a_city_row_for_a_different_city_does_not_hit(self):
		rows = [_row("F", territory="C", municipality="DEERFIELD")]
		out = restrictions.evaluate(rows, state="IL", city="PEORIA")
		self.assertFalse(out["blocked_dropship"])
		self.assertIsNone(out["level"])

	def test_no_rows_at_all_is_a_clean_pass(self):
		out = restrictions.evaluate([], state="TX", city="AUSTIN")
		self.assertFalse(out["blocked_dropship"])
		self.assertEqual(out["codes"], [])
		self.assertFalse(out["county_uncertain"])
		self.assertTrue(out["checked"])


class DestinationNormalisation(unittest.TestCase):
	"""The feed is upper-case and Woo is whatever the buyer typed."""

	def test_city_match_ignores_case_and_surrounding_space(self):
		rows = [_row("F", territory="C", municipality="MELROSE PARK")]
		out = restrictions.evaluate(rows, state="IL", city="  melrose park ")
		self.assertTrue(out["blocked_dropship"])

	def test_city_match_collapses_repeated_inner_whitespace(self):
		rows = [_row("F", territory="C", municipality="CHICAGO HEIGHTS")]
		out = restrictions.evaluate(rows, state="IL", city="Chicago  Heights")
		self.assertTrue(out["blocked_dropship"])

	def test_a_spelled_out_state_still_matches_a_two_letter_row(self):
		"""ffl_core already learned this the hard way (`normalize_us_state`): an
		operator or an ATF record may spell the state out, and a miss here is a
		silent fail-open."""
		rows = [_row("F", territory="S", municipality="ALL", state="IL")]
		out = restrictions.evaluate(rows, state="Illinois", city="PEORIA")
		self.assertTrue(out["blocked_dropship"])

	def test_rows_for_another_state_are_ignored(self):
		"""Defensive: the DB layer filters by state, but a caller passing a whole
		SKU's rows must not have New York's bans applied to a Texas address."""
		rows = [_row("Y", territory="S", municipality="ALL", state="NY")]
		out = restrictions.evaluate(rows, state="TX", city="AUSTIN")
		self.assertFalse(out["blocked_dropship"])


class UnknownDestination(unittest.TestCase):
	"""Mirrors options.py's existing tri-state discipline: an unresolved
	destination is ``None``-ish, never a green light."""

	def test_a_blank_state_is_unchecked_not_unrestricted(self):
		rows = [_row("F", territory="S", municipality="ALL")]
		out = restrictions.evaluate(rows, state="", city="CHICAGO")
		self.assertFalse(out["checked"])
		self.assertIsNone(out["blocked_dropship"])

	def test_a_blank_city_leaves_city_rows_unconfirmed(self):
		"""A city-level row plus no city is the same shape of hole as county: we
		must say we could not check it, not that it passed."""
		rows = [_row("F", territory="C", municipality="DEERFIELD")]
		out = restrictions.evaluate(rows, state="IL", city="")
		self.assertTrue(out["city_uncertain"])
		self.assertFalse(out["blocked_dropship"])
		self.assertEqual(len(out["uncertain"]), 1)


class TheLetterSMeansTwoDifferentThings(unittest.TestCase):
	"""``S`` is a valid value in BOTH code columns with unrelated meanings —
	column 4 ``S`` = State scope, column 5 ``S`` = assault-weapon permit.
	This is the single easiest place in the feed to write a bug that looks correct,
	so it gets its own class rather than a comment."""

	def test_column_4_S_is_a_scope_and_column_5_S_is_a_permit(self):
		out = restrictions.evaluate(
			[_row("S", territory="S", municipality="ALL")], state="IL", city="PEORIA")
		match = out["matches"][0]
		self.assertEqual(match["level"], "state")   # column 4
		self.assertEqual(match["code"], "S")        # column 5
		self.assertTrue(match["documented"])
		self.assertIn("assault-weapon permit", match["text"].lower())
		# ...and the scope did not leak into the consequence: a state-scoped row is
		# not thereby a block.
		self.assertFalse(match["blocks_dropship"])

	def test_the_scope_constant_and_the_permit_constant_are_not_the_same_object(self):
		"""Sharing one constant would make every `S` restriction read as a scope,
		or every state-scoped row read as an AWL requirement — and both would
		still produce plausible-looking output."""
		self.assertEqual(restrictions.TERRITORY_STATE, "S")
		self.assertEqual(restrictions.DETAIL_ASSAULT_WEAPON_PERMIT, "S")
		self.assertIsNot(
			restrictions.DOCUMENTED_DETAIL_CODES, restrictions._TERRITORY_LEVEL)
		self.assertNotIn(
			restrictions.TERRITORY_CITY, restrictions.DOCUMENTED_DETAIL_CODES)


class PermitCodesDoNotBlockDropShip(unittest.TestCase):
	"""``H``/``O``/``S``/``R`` say the RECIPIENT needs a permit. They do NOT
	say RSR refuses to drop-ship, and two independent readings of the current
	layout document agree on that:

	* ``A`` and ``L`` spell the consequence out — "...and are blocked to
	  fulfillment or drop shipments". These four carry no such clause;
	* the companion-``F`` note names A/L/Y and pointedly not these four.

	Treating them as blockers would withhold the drop-ship recommendation on
	14,104 rows of perfectly shippable goods. Treating them as nothing would hide
	a permit the shop actually has to hold. They display, and they do not block.
	"""

	_PERMIT_CODES = ("H", "O", "S", "R")

	def test_no_permit_code_blocks_drop_ship(self):
		"""THE signature assertion of the permit-code correction."""
		for code in self._PERMIT_CODES:
			with self.subTest(code=code):
				out = restrictions.evaluate(
					[_row(code, territory="S", municipality="ALL")],
					state="IL", city="PEORIA")
				self.assertFalse(out["blocked_dropship"],
					f"{code!r} is a permit requirement, not a drop-ship block")

	def test_a_permit_code_is_still_shown_to_the_operator(self):
		"""Not blocking is not the same as not mattering — the shop still has to
		hold the permit."""
		for code in self._PERMIT_CODES:
			with self.subTest(code=code):
				out = restrictions.evaluate(
					[_row(code, territory="S", municipality="ALL")],
					state="IL", city="PEORIA")
				self.assertEqual(len(out["matches"]), 1)
				self.assertEqual(out["codes"], [code])
				self.assertFalse(out["matches"][0]["blocks_dropship"])
				self.assertTrue(out["headline"])

	def test_each_permit_code_names_the_permit_it_needs(self):
		""""Restricted" tells an operator nothing they can act on; "requires SOTS
		licensing" tells them the next step."""
		for code, needle in (("H", "high-capacity permit"), ("O", "sots licensing"),
				("S", "assault-weapon permit"), ("R", "short-barrel permit")):
			with self.subTest(code=code):
				out = restrictions.evaluate(
					[_row(code, territory="S", municipality="ALL")],
					state="IL", city="PEORIA")
				self.assertIn(needle, out["matches"][0]["text"].lower())

	def test_a_permit_code_does_not_claim_rsr_refuses_to_ship(self):
		"""The wording difference is the whole point of the correction: the first
		four codes say RSR will not send it, these four say paperwork is needed."""
		for code in self._PERMIT_CODES:
			with self.subTest(code=code):
				out = restrictions.evaluate(
					[_row(code, territory="S", municipality="ALL")],
					state="IL", city="PEORIA")
				self.assertNotIn("will not drop-ship",
					out["matches"][0]["text"].lower())

	def test_a_permit_code_alongside_an_F_is_blocked_by_the_F(self):
		"""The union still governs. A permit code does not soften a real block."""
		out = restrictions.evaluate([
			_row("S", territory="S", municipality="ALL"),
			_row("F", territory="S", municipality="ALL"),
		], state="IL", city="PEORIA")
		self.assertTrue(out["blocked_dropship"])
		self.assertEqual(out["codes"], ["F", "S"])

	def test_all_eight_live_codes_are_documented(self):
		"""The FTP copy of restrictions-fields.txt is an OLD revision with
		four codes. The current one from Dealer's Toolbox has all eight."""
		for code in ("Y", "F", "A", "L", "H", "O", "S", "R"):
			with self.subTest(code=code):
				out = restrictions.evaluate(
					[_row(code, territory="S", municipality="ALL")],
					state="IL", city="PEORIA")
				self.assertEqual(out["unknown_codes"], [])
				self.assertEqual(out["codes"], [code])


class UnknownCodesFailClosed(unittest.TestCase):
	"""Unknown codes still fail closed even though the unknown set is now EMPTY.

	Every code RSR currently ships is documented. The branch stays because the
	rule was never about a list of letters observed on one day — it is about codes
	we cannot READ, and RSR has already revised this layout document once."""

	def test_the_documented_set_is_currently_exhaustive(self):
		self.assertEqual(restrictions.UNDOCUMENTED_DETAIL_CODES_SEEN, frozenset())

	def test_a_code_rsr_has_never_shipped_fails_closed(self):
		"""If RSR adds a ninth code tomorrow, it blocks until someone reads the
		new document — it does not quietly become "no restriction"."""
		out = restrictions.evaluate(
			[_row("Z", territory="S", municipality="ALL")], state="IL", city="PEORIA")
		self.assertTrue(out["blocked_dropship"])
		self.assertEqual(out["unknown_codes"], ["Z"])
		self.assertEqual(out["codes"], [])

	def test_an_unknown_code_says_so_rather_than_guessing(self):
		out = restrictions.evaluate(
			[_row("Z", territory="S", municipality="ALL")], state="IL", city="PEORIA")
		self.assertFalse(out["matches"][0]["documented"])
		self.assertIn("Z", out["matches"][0]["text"])

	def test_an_undocumented_territory_type_also_fails_closed(self):
		"""Column 4's measured values are only S/C/T — an observation, not an
		invariant. An unrecognised SCOPE is not a pass either."""
		out = restrictions.evaluate(
			[_row("F", territory="Z", municipality="ALL")], state="IL", city="PEORIA")
		self.assertTrue(out["blocked_dropship"])

	def test_a_blank_detail_code_fails_closed(self):
		out = restrictions.evaluate(
			[_row("", territory="S", municipality="ALL")], state="IL", city="PEORIA")
		self.assertTrue(out["blocked_dropship"])

	def test_code_matching_is_case_insensitive_but_display_is_verbatim(self):
		out = restrictions.evaluate(
			[_row("f", territory="S", municipality="ALL")], state="IL", city="PEORIA")
		self.assertEqual(out["codes"], ["F"])
		self.assertEqual(out["unknown_codes"], [])


class BlockingCodesStopDropShip(unittest.TestCase):
	"""``Y``/``F``/``A``/``L`` — the four whose own definition states the
	consequence. Each is asserted SEPARATELY rather than via "does the code set
	contain F", and that distinction is the point of this class.

	RSR promises a companion ``F`` row alongside A/L/Y, so "blocked iff F is
	present" gives the same answer on well-formed data and is tempting because it
	is one line. It is not the same rule: it makes our verdict depend on RSR's
	data being internally consistent, when each code's own text already answers
	the question. On the one day RSR ships a ``Y`` without its ``F``, the short
	rule drop-ships into an area RSR blocks entirely.
	"""

	_BLOCKERS = ("Y", "F", "A", "L")

	def test_each_blocking_code_blocks_on_its_own(self):
		for code in self._BLOCKERS:
			with self.subTest(code=code):
				out = restrictions.evaluate(
					[_row(code, territory="S", municipality="ALL")],
					state="IL", city="PEORIA")
				self.assertTrue(out["blocked_dropship"])

	def test_a_lone_Y_without_its_companion_F_still_blocks(self):
		"""``Y`` is "all shipments into the defined area are blocked" — drop-ship
		is a shipment. It does not need an ``F`` to say so."""
		out = restrictions.evaluate(
			[_row("Y", territory="S", municipality="ALL")], state="IL", city="PEORIA")
		self.assertTrue(out["blocked_dropship"])
		self.assertEqual(out["codes"], ["Y"])

	def test_a_lone_A_or_L_without_its_companion_F_still_blocks(self):
		"""Both definitions END with "and are blocked to fulfillment or drop
		shipments". The companion row is RSR restating it, not establishing it."""
		for code in ("A", "L"):
			with self.subTest(code=code):
				out = restrictions.evaluate(
					[_row(code, territory="S", municipality="ALL")],
					state="IL", city="PEORIA")
				self.assertTrue(out["blocked_dropship"])

	def test_each_blocking_code_says_rsr_will_not_send_it(self):
		"""The words are served here, not written in the WP theme —
		and these four must read as a refusal, unlike the permit family."""
		for code in self._BLOCKERS:
			with self.subTest(code=code):
				out = restrictions.evaluate(
					[_row(code, territory="S", municipality="ALL")],
					state="IL", city="PEORIA")
				text = out["matches"][0]["text"]
				self.assertTrue(text and text[0].isupper(), text)
				self.assertTrue(out["matches"][0]["documented"])
				self.assertTrue(out["matches"][0]["blocks_dropship"])


class CountyIsNeitherHitNorSilence(unittest.TestCase):
	"""Woo carries no county and we have no zip->county set, so a ``T`` row
	can only ever be reported as unresolved."""

	def test_a_county_row_does_not_claim_a_hit(self):
		out = restrictions.evaluate(
			[_row("F", territory="T", municipality="COOK")], state="IL", city="PEORIA")
		self.assertFalse(out["blocked_dropship"])
		self.assertIsNone(out["level"])
		self.assertEqual(out["codes"], [])

	def test_a_county_row_does_not_vanish(self):
		"""The other half, and the one a naive filter gets wrong: dropping T rows
		would read as "Illinois is clear" on 75,207 rows of real restriction."""
		out = restrictions.evaluate(
			[_row("F", territory="T", municipality="COOK")], state="IL", city="PEORIA")
		self.assertTrue(out["county_uncertain"])
		self.assertEqual(len(out["uncertain"]), 1)
		self.assertEqual(out["uncertain"][0]["level"], "county")
		self.assertEqual(out["uncertain"][0]["municipality"], "COOK")

	def test_county_uncertainty_survives_alongside_a_confirmed_state_hit(self):
		"""A confirmed hit must not swallow the unresolved one — they are separate
		facts and the operator is shown both."""
		rows = [
			_row("F", territory="S", municipality="ALL"),
			_row("L", territory="T", municipality="COOK"),
		]
		out = restrictions.evaluate(rows, state="IL", city="PEORIA")
		self.assertTrue(out["blocked_dropship"])
		self.assertTrue(out["county_uncertain"])

	def test_an_undocumented_code_on_a_county_row_stays_uncertain_not_blocking(self):
		"""Two rules meet here. Fail-closed applies to codes we cannot READ;
		county applies to destinations we cannot RESOLVE. An unreadable code on an
		unresolvable row is still unresolvable — blocking would let one county ban
		a whole state, which is explicitly refused."""
		out = restrictions.evaluate(
			[_row("Z", territory="T", municipality="COOK")], state="IL", city="PEORIA")
		self.assertFalse(out["blocked_dropship"])
		self.assertTrue(out["county_uncertain"])
		self.assertEqual(out["uncertain"][0]["code"], "Z")
		self.assertFalse(out["uncertain"][0]["documented"])


class VerdictShapeIsStable(unittest.TestCase):
	"""The WP metabox renders this dict. Every key is present on every path so the
	renderer never has to guess whether a missing key means false or unknown."""

	_KEYS = {"checked", "blocked_dropship", "codes", "unknown_codes", "level",
		"levels", "county_uncertain", "city_uncertain", "matches", "uncertain",
		"headline", "destination"}

	def test_every_path_returns_the_same_keys(self):
		cases = [
			([], "TX", "AUSTIN"),
			([_row("F")], "IL", "PEORIA"),
			([_row("F", territory="T", municipality="COOK")], "IL", "PEORIA"),
			([_row("O")], "IL", "PEORIA"),
			([_row("F")], "", ""),
		]
		for rows, state, city in cases:
			with self.subTest(state=state, city=city):
				self.assertEqual(set(restrictions.evaluate(
					rows, state=state, city=city)), self._KEYS)


class HeadlineIsServedNotDerived(unittest.TestCase):
	"""Operator-facing words are wanted, and this project's standing
	rule is that they come from the server so POS and WP cannot drift into two
	explanations of the same code. The metabox renders `headline`; it must never
	have to assemble one from `codes` + `level`."""

	def test_a_confirmed_hit_names_the_destination_it_checked(self):
		out = restrictions.evaluate(
			[_row("F", territory="C", municipality="CHICAGO HEIGHTS")],
			state="IL", city="Chicago Heights")
		self.assertIn("Chicago Heights", out["headline"])
		self.assertIn("drop-ship", out["headline"].lower())

	def test_the_destination_that_was_checked_is_reported_back(self):
		"""So an operator seeing a surprising answer can tell WHICH address it was
		judged against — the commonest cause of a surprising answer."""
		out = restrictions.evaluate([], state="IL", city="Peoria")
		self.assertEqual(out["destination"], "Peoria, IL")

	def test_a_clean_destination_has_no_headline_to_render(self):
		out = restrictions.evaluate([], state="TX", city="Austin")
		self.assertEqual(out["headline"], "")

	def test_a_county_only_result_says_it_could_not_be_resolved(self):
		"""The wording an operator must not misread as either a hit or a pass."""
		out = restrictions.evaluate(
			[_row("F", territory="T", municipality="COOK")], state="IL", city="PEORIA")
		self.assertIn("county", out["headline"].lower())
		self.assertNotIn("will not drop-ship", out["headline"].lower())

	def test_an_unchecked_destination_says_so(self):
		out = restrictions.evaluate([_row("F")], state="", city="")
		self.assertIn("could not", out["headline"].lower())


class MalformedRowsDoNotCrash(unittest.TestCase):
	"""RSR's feed is 517,560 lines refreshed every two hours. A bad line must
	degrade this panel, never take it down — and never quietly pass."""

	def test_a_row_missing_every_field_is_treated_as_unreadable_not_absent(self):
		out = restrictions.evaluate([{}], state="IL", city="PEORIA")
		self.assertIsInstance(out["blocked_dropship"], bool)

	def test_none_rows_are_tolerated(self):
		out = restrictions.evaluate([None, _row("F")], state="IL", city="PEORIA")
		self.assertTrue(out["blocked_dropship"])


class RowsComeBackInThePerLineShape(unittest.TestCase):
	"""Storage reshape: rows live as one JSON column per SKU (the loader's
	local write side, untouched), but `restrictions_for` / `check` /
	`blocked_states_for` now READ through the remote catalog (the Data Service / the
	in-memory stub in tests) via `_entries_for` — the local `current_batch`
	pointer this class used to mock has been REMOVED, not just renamed.
	:func:`evaluate` is written against per-line dicts and is deliberately
	untouched — `restrictions_for` owns unpacking a SKU's raw stub entries into
	that per-line shape."""

	def setUp(self):
		self.stub = catalog_stub.install()

	def tearDown(self):
		catalog_stub.uninstall()

	def test_entries_unpack_to_the_field_names_evaluate_expects(self):
		self.stub.seed_restrictions(
			"SKU1", [["CHICAGO HEIGHTS", "IL", "C", "F"], ["ALL", "AL", "S", "L"]])
		rows = restrictions.restrictions_for("SKU1", state="IL")
		self.assertEqual(rows, [{
			"municipality": "CHICAGO HEIGHTS", "state": "IL",
			"territory_type": "C", "restriction_detail": "F"}])

	def test_other_states_entries_are_filtered_out_here_not_only_in_evaluate(self):
		self.stub.seed_restrictions("SKU1", [["ALL", "NY", "S", "Y"]])
		self.assertEqual(restrictions.restrictions_for("SKU1", state="TX"), [])

	def test_no_entries_seeded_for_this_sku_reports_nothing_rather_than_a_clean_pass(self):
		"""The stub-backed equivalent of the old "no batch pointer" case: SKU2
		carries real entries, SKU1 carries none, and SKU1 must not see them —
		proving the read is genuinely per-SKU, not "anything loaded anywhere"."""
		self.stub.seed_restrictions("SKU2", [["CHICAGO HEIGHTS", "IL", "C", "F"]])
		self.assertEqual(restrictions.restrictions_for("SKU1", state="IL"), [])

	def test_a_remote_read_failure_degrades_to_could_not_check_never_to_no_restrictions(self):
		"""The failure this used to inject as a corrupt JSON string in the local
		batch table has moved down a layer now that reads go through the remote
		catalog — the transport (or the stub standing in for it) is what can now
		fail. It must still reach the operator as "could not check"
		(checked=False), never a swallowed error rendering as a clean pass."""
		with mock.patch.object(restrictions, "_entries_for",
				side_effect=RuntimeError("transport down")), \
				mock.patch.object(restrictions.frappe, "log_error"):
			out = restrictions.check("SKU1", state="IL", city="PEORIA")
		self.assertFalse(out["checked"])
		self.assertIsNone(out["blocked_dropship"])


class BlockedStatesForTheQuotePanel(unittest.TestCase):
	"""The Create Order quote's red line, replacing the guessed catalog
	column. Coarse by design — any blocking entry anywhere in a state reports
	that state — and display-only, like everything in this module."""

	_ENTRIES = [
		["ALL", "NY", "S", "O"],           # permit code — does NOT block
		["ALL", "NJ", "S", "F"],           # state-level block
		["COOK", "IL", "T", "Y"],          # county-level block → IL still reported
		["ALL", "CA", "S", "H"],           # permit code — does NOT block
		["DEERFIELD", "IL", "C", "L"],     # city-level block, same state
	]

	def setUp(self):
		self.stub = catalog_stub.install()

	def tearDown(self):
		catalog_stub.uninstall()

	def test_reports_only_states_with_a_blocking_code(self):
		self.stub.seed_restrictions("SKU1", self._ENTRIES)
		self.assertEqual(
			restrictions.blocked_states_for("SKU1"), ["IL", "NJ"])

	def test_a_permit_only_sku_reports_no_states(self):
		"""The permit-code correction carries through: H/O/S/R alone must not paint a
		red line, or 14,104 rows of shippable goods look unshippable at quote."""
		self.stub.seed_restrictions("SKU1", [["ALL", "NY", "S", "O"], ["ALL", "CA", "S", "H"]])
		self.assertEqual(restrictions.blocked_states_for("SKU1"), [])

	def test_an_undocumented_code_fails_closed_into_the_red_line(self):
		self.stub.seed_restrictions("SKU1", [["ALL", "TX", "S", "Z"]])
		self.assertEqual(restrictions.blocked_states_for("SKU1"), ["TX"])

	def test_a_sku_with_no_row_reports_no_states(self):
		self.assertEqual(restrictions.blocked_states_for("SKU1"), [])

	def test_no_entries_seeded_for_this_sku_reports_no_states(self):
		"""The stub-backed equivalent of the old "no batch pointer" case — see the
		matching note on RowsComeBackInThePerLineShape."""
		self.stub.seed_restrictions("SKU2", self._ENTRIES)
		self.assertEqual(restrictions.blocked_states_for("SKU1"), [])

	def test_a_read_failure_yields_an_empty_hint_not_an_exception(self):
		"""This is a hint beside the authoritative panel; a hint that can take
		down a quote would be a bad trade. Logged, then empty."""
		with mock.patch.object(restrictions, "_entries_for",
				side_effect=RuntimeError("transport down")), \
				mock.patch.object(restrictions.frappe, "log_error") as log_error:
			self.assertEqual(restrictions.blocked_states_for("SKU1"), [])
		log_error.assert_called_once()


if __name__ == "__main__":
	unittest.main()
