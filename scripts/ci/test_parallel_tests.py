"""Unit tests for the planning half of scripts/ci/parallel_tests.py.

python3 -m unittest discover -s scripts/ci -p 'test_*.py'
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import parallel_tests as pt  # noqa: E402
import parallel_worker as pw  # noqa: E402


class Discover(unittest.TestCase):
	def test_walks_like_frappe_discovery(self):
		with tempfile.TemporaryDirectory() as apps:
			pkg = Path(apps) / "demo_app" / "demo_app"
			files = {
				"tests/test_alpha.py": "def test_a(): pass\ndef test_b(): pass\n",
				"doctype/thing/test_thing.py": "class T:\n    def test_x(self): pass\n",
				"tests/helpers.py": "def test_not_a_module(): pass\n",
				"tests/test_runner.py": "def test_skipped(): pass\n",
				"public/test_asset.py": "def test_skipped(): pass\n",
				"doctype/doctype/boilerplate/test_template.py": "def test_skipped(): pass\n",
			}
			for rel, body in files.items():
				(pkg / rel).parent.mkdir(parents=True, exist_ok=True)
				(pkg / rel).write_text(body)
			self.assertEqual(pt.discover(apps, ["demo_app"]), {
				"demo_app.doctype.thing.test_thing": 1,
				"demo_app.tests.test_alpha": 2,
			})


class Select(unittest.TestCase):
	FOUND = {"a.tests.test_x": 3, "a.tests.test_y": 1, "b.tests.test_z": 2}

	def test_empty_keeps_everything(self):
		self.assertEqual(pt.select(self.FOUND, ""), self.FOUND)

	def test_named_modules_only_in_either_separator(self):
		self.assertEqual(pt.select(self.FOUND, "a.tests.test_y, b.tests.test_z"), {"a.tests.test_y": 1, "b.tests.test_z": 2})

	def test_a_name_that_does_not_exist_stops_the_run(self):
		with self.assertRaises(SystemExit) as caught:
			pt.select(self.FOUND, "a.tests.test_x a.tests.test_gone")
		self.assertIn("a.tests.test_gone", str(caught.exception))


class Planning(unittest.TestCase):
	def test_measured_seconds_win_and_new_modules_get_the_average_per_test(self):
		modules = {"a": 10, "b": 5, "new": 4}
		w = pt.weights(modules, {"a": 20.0, "b": 10.0, "gone": 99.0})
		self.assertEqual(w, {"a": 20.0, "b": 10.0, "new": 8.0})

	def test_without_history_every_test_weighs_the_same(self):
		self.assertEqual(pt.weights({"a": 3, "empty": 0}, {}), {"a": 0.6000000000000001, "empty": 0.2})

	def test_partition_balances_heaviest_first_and_keeps_each_group_sorted(self):
		groups, loads = pt.partition({"m1": 30, "m2": 20, "m3": 20, "m4": 10, "m5": 10}, 2)
		self.assertEqual(sorted(loads), [40, 50])  # the best split of these weights
		self.assertEqual(sorted(m for g in groups for m in g), ["m1", "m2", "m3", "m4", "m5"])
		self.assertTrue(all(g == sorted(g) for g in groups))

	def test_more_workers_than_modules_drops_empty_groups(self):
		groups, _ = pt.partition({"only": 5}, 4)
		self.assertEqual(groups, [["only"]])


class Totals(unittest.TestCase):
	def worker(self, run=10, failures=None, errors=None, crash=None):
		return {"tests_run": run, "failure_counts": failures or {}, "error_counts": errors or {}, "crash": crash}

	def test_sums_every_worker(self):
		first = [self.worker(run=10, failures={"a": 2}), self.worker(run=5, errors={"b": 1, "c": 3})]
		self.assertEqual(pt.totals(first, []), (15, 2, 4))

	def test_a_flaky_module_is_not_counted_as_failing(self):
		first = [self.worker(failures={"a": 2}, errors={"a": 1, "b": 1})]
		self.assertEqual(pt.totals(first, ["a"]), (10, 0, 1))

	def test_a_crashed_worker_is_one_error(self):
		self.assertEqual(pt.totals([self.worker(run=0, crash="ImportError")], []), (0, 0, 1))

	def test_a_worker_result_from_before_the_counts_existed_reads_as_clean(self):
		self.assertEqual(pt.totals([{"tests_run": 7}], []), (7, 0, 0))


class Verdict(unittest.TestCase):
	def worker(self, failed=(), run=10, crash=None, site="s"):
		return {"site": site, "tests_run": run, "failed_modules": list(failed), "crash": crash}

	def test_green(self):
		self.assertEqual(pt.verdict([self.worker(), self.worker()], {}), (True, [], [], []))

	def test_a_module_that_passes_alone_is_flaky_not_fatal(self):
		ok, flaky, broken, _ = pt.verdict([self.worker(failed=["m"])], {"m": self.worker()})
		self.assertEqual((ok, flaky, broken), (True, ["m"], []))

	def test_a_module_that_fails_again_is_fatal(self):
		ok, flaky, broken, _ = pt.verdict([self.worker(failed=["m"])], {"m": self.worker(failed=["m"])})
		self.assertEqual((ok, flaky, broken), (False, [], ["m"]))

	def test_a_rerun_that_ran_nothing_does_not_count_as_a_pass(self):
		ok, _, broken, _ = pt.verdict([self.worker(failed=["m"])], {"m": self.worker(run=0)})
		self.assertEqual((ok, broken), (False, ["m"]))

	def test_a_crashed_worker_fails_the_run(self):
		ok, _, _, notes = pt.verdict([self.worker(crash="ImportError"), self.worker()], {})
		self.assertFalse(ok)
		self.assertIn("crashed", notes[0])

	def test_zero_tests_is_red(self):
		ok, _, _, notes = pt.verdict([self.worker(run=0), self.worker(run=0)], {})
		self.assertEqual((ok, notes), (False, ["no tests ran at all"]))

	def test_many_failures_are_not_re_run(self):
		failed = [f"m{i}" for i in range(pt.RERUN_LIMIT + 1)]
		ok, flaky, broken, notes = pt.verdict([self.worker(failed=failed)], {})
		self.assertEqual((ok, flaky, broken), (False, [], failed))
		self.assertIn("not flakiness", notes[0])


class Attribution(unittest.TestCase):
	"""A TestCase imported into another test module runs there too; its failures
	belong to the module that loaded it, not the one that defines it."""

	def setUp(self):
		import types

		self.base = types.ModuleType("app.tests.test_base")

		class Base(unittest.TestCase):
			def test_shared(self):
				pass

		Base.__module__, Base.__qualname__ = self.base.__name__, "Base"  # as if defined at module level
		self.base.Base = Base
		self.legs = types.ModuleType("app.tests.test_legs")
		self.legs.Base = Base  # `from app.tests.test_base import Base`

		class Legs(Base):
			def test_own(self):
				pass

		Legs.__module__, Legs.__qualname__ = self.legs.__name__, "Legs"
		self.legs.Legs = Legs
		self.sources = __import__("collections").defaultdict(set)
		self.restore = pw.tag_loads(self.sources)
		self.addCleanup(self.restore)

	def loaded(self, module):
		return list(pw.iter_tests(unittest.TestLoader().loadTestsFromModule(module)))

	def test_an_imported_class_is_charged_to_the_module_that_loaded_it(self):
		modules = ["app.tests.test_legs"]
		tests = self.loaded(self.legs)
		self.assertEqual(len(tests), 3)  # Base.test_shared, Legs.test_own, Legs.test_shared
		for test in tests:
			self.assertEqual(pw.modules_of(test, modules, self.sources), {"app.tests.test_legs"})

	def test_a_class_setup_error_goes_to_every_module_that_loaded_the_class(self):
		self.loaded(self.base)
		self.loaded(self.legs)
		holder = unittest.suite._ErrorHolder("setUpClass (app.tests.test_base.Base)")
		self.assertEqual(pw.modules_of(holder, ["app.tests.test_base", "app.tests.test_legs"], self.sources),
			{"app.tests.test_base", "app.tests.test_legs"})

	def test_module_setup_errors_fall_back_to_the_module_name_and_unknowns_to_a_question_mark(self):
		holder = unittest.suite._ErrorHolder("setUpModule (app.tests.test_legs)")
		self.assertEqual(pw.modules_of(holder, ["app.tests.test_legs"], self.sources), {"app.tests.test_legs"})
		self.assertEqual(pw.modules_of(unittest.suite._ErrorHolder("mystery"), ["app.tests.test_legs"], self.sources), {"?"})


if __name__ == "__main__":
	unittest.main()
