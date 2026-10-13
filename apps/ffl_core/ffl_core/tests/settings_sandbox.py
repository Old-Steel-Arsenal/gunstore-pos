"""Test-only: change a Single's field and put it back, whatever the transaction does.

``FrappeTestCase`` rolls back ONCE at tearDownClass, and ``frappe.db.rollback``
only rewinds to the last commit — so any commit that lands mid-run (another
module's ``dev_site.run()``, a setup ensure, a code path that legitimately
commits) turns a test's Single write into a PERMANENT change to the site.

For an ordinary fixture row that is merely untidy. For a settings flag it is
poison: a leaked ``paper_4473_mode=1`` silently puts every later firearm test in
the run on the paper-4473 path, and a leaked ``FastBound Settings.enabled=0``
switches the integration off underneath every FastBound test that follows. The
failures then land in whatever module happens to run next, which is nowhere near
the test that caused them.

Observed for real: a full ``make test`` left ``FFL Settings.paper_4473_mode``
written to tabSingles even though every writer was inside the test transaction.
It happened to settle on the harmless value; that was luck, not design.

So restore explicitly rather than relying on the rollback, and restore to the RAW
prior state — ``None`` (no row at all) is a different thing from ``"0"``, and the
switch readers deliberately tell them apart (``ffl_core.utils.single_flag``).

**Which of the two helpers do I want?**

``ffl_integrations.tests.site_fixtures.snapshot_single_values`` came first and
solves the neighbouring problem — a fixture that overwrites CONNECTION settings
(endpoint / client id / secret) and disconnects the shared dev site from the real
service (the 2026-08-01 incident: ``make test`` left FFL Settings pointing at
``api.example.test``). Use it when Password fields are involved: it round-trips
through ``doc.save()`` + commit, which is the only way ``__Auth`` values survive.

Use THIS module for switches and other plain Check/Data fields. It writes
``tabSingles`` directly, so it can restore a field to "no row at all" — which
``doc.save()`` structurally cannot express, and which is the exact state every
default-ON switch depends on. It also does not commit, so it leaves no trace of
its own on a shared site.

Neither supersedes the other; if you find yourself needing both behaviours at
once, that is the signal to merge them rather than add a third.
"""
from __future__ import annotations

from contextlib import contextmanager

import frappe

from ffl_core.utils import single_raw


read_raw_single = single_raw   # the production reader; same "None means no row" contract


def write_raw_single(doctype: str, field: str, value) -> None:
	"""Set the value, or remove the row entirely when `value` is None."""
	if value is None:
		frappe.db.sql(
			"DELETE FROM `tabSingles` WHERE `doctype` = %s AND `field` = %s",
			(doctype, field))
		return
	frappe.db.set_single_value(doctype, field, value)


@contextmanager
def single_value(doctype: str, field: str, value):
	"""Set a Single's field for the duration of the block, then put it back."""
	before = read_raw_single(doctype, field)
	write_raw_single(doctype, field, value)
	try:
		yield
	finally:
		write_raw_single(doctype, field, before)


class _SandboxState:
	"""Per-test-class record of what each touched Single field looked like first."""

	__slots__ = ("originals",)

	def __init__(self):
		self.originals: dict[tuple[str, str], str | None] = {}


class SettingsSandbox:
	"""Mixin for FrappeTestCase: ``self.set_single(...)`` auto-restores on teardown.

	Use instead of a bare ``frappe.db.set_single_value`` anywhere a test changes a
	settings flag — including in ``setUp``, where the cleanup still runs per test.

	Two layers, because one is not enough:

	* per test, an ``addCleanup`` that puts the previous value back inside the
	  transaction — cheap, and handles a class whose tests flip a flag back and
	  forth;
	* per class, a committed backstop for the case where a mid-test commit made the
	  transaction-level restore evaporate (see ``_restore_and_commit``).
	"""

	@classmethod
	def setUpClass(cls) -> None:
		# Registered BEFORE super(), and that ordering is the whole point. Frappe's
		# IntegrationTestCase.setUpClass does `addClassCleanup(_rollback_db)`, and
		# class cleanups run LIFO — so anything registered AFTER it runs BEFORE the
		# rollback, where the value it reads is the not-yet-rolled-back one. Going
		# first here puts this last, after the rollback has had its say.
		cls.addClassCleanup(cls._restore_sandboxed_singles)
		super().setUpClass()

	@classmethod
	def _restore_sandboxed_singles(cls) -> None:
		"""Committed backstop, run after the class rollback.

		The per-test ``addCleanup`` restore lives INSIDE the test transaction, so it
		shares that transaction's fate: one commit landing mid-test while a pinned
		value is in force makes the pinned value permanent, and the rollback then
		discards the restore along with everything else uncommitted. Observed for
		real — a run left ``FastBound Settings.enabled`` at 1 on a site the operator
		had deliberately set to 0, which is how QA caught it.

		This is why the older ``site_fixtures.snapshot_single_values`` commits its
		restore; leaving that step out was the bug. The equality check keeps the cost
		honest: in the normal case the rollback already did the work and this commits
		nothing, so it does not move the next class's rollback floor for free.
		"""
		state = cls.__dict__.get("_sandbox_state")
		if state is None:
			return
		dirty = False
		for (doctype, field), original in state.originals.items():
			if read_raw_single(doctype, field) == original:
				continue
			write_raw_single(doctype, field, original)
			dirty = True
		if dirty:
			frappe.db.commit()

	def set_single(self, doctype: str, field: str, value) -> None:
		cls = type(self)
		state = cls.__dict__.get("_sandbox_state")
		if state is None:
			state = _SandboxState()
			cls._sandbox_state = state
		key = (doctype, field)
		if key not in state.originals:
			state.originals[key] = read_raw_single(doctype, field)
		self.addCleanup(write_raw_single, doctype, field,
			read_raw_single(doctype, field))
		write_raw_single(doctype, field, value)

	def clear_single(self, doctype: str, field: str) -> None:
		"""Put the field back to 'never saved on this site' for this test."""
		self.set_single(doctype, field, None)
