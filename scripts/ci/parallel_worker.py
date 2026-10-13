"""One parallel-test worker: run a list of test modules on one site.

Started by parallel_tests.py from <bench>/sites with the bench virtualenv's python:

    ../env/bin/python parallel_worker.py --site ci2.localhost --modules a.tests.test_x,a.tests.test_y --result r.json

It calls frappe.commands.testing.main, the body of `bench run-tests`, with a LIST
of modules (discover_module_tests accepts one), so environment setup, test
categories and before-tests preparation match a normal run exactly. Thin
wrappers record what the orchestrator needs: which module each test was LOADED
from, every result object, so a failure can be pinned to that module, and
per-module test time, which balances the next run. The exit code is always 0;
the orchestrator judges the JSON.
"""
import argparse
import json
import re
import sys
import time
import unittest
from collections import defaultdict

LOADED_FROM = "_ci_loaded_from"


def iter_tests(suite):
	for item in suite:
		if isinstance(item, unittest.TestSuite):
			yield from iter_tests(item)
		else:
			yield item


def tag_loads(class_sources):
	"""Stamp every test with the module unittest loaded it from.

	A TestCase class imported into another test module is collected and run
	there too (test_ci_scripts.NoRerunTestClasses keeps ours from doing it), yet its
	__module__ still names the module that defines it. The failing module is
	the one being loaded, so that is what a test carries. class_sources maps
	"pkg.module.Class" to the modules that loaded it, for errors raised outside
	any single test (setUpClass). Returns a function that undoes the patch."""
	load = unittest.TestLoader.loadTestsFromModule

	def tagging_load(self, module, *args, **kwargs):
		suite = load(self, module, *args, **kwargs)
		for test in iter_tests(suite):
			setattr(test, LOADED_FROM, module.__name__)
			class_sources[unittest.util.strclass(type(test))].add(module.__name__)
		return suite

	unittest.TestLoader.loadTestsFromModule = tagging_load
	return lambda: setattr(unittest.TestLoader, "loadTestsFromModule", load)


def modules_of(test, modules, class_sources):
	"""The module(s) a failure belongs to; {"?"} when it cannot be told."""
	loaded = getattr(test, LOADED_FROM, None)
	if loaded:
		return {loaded}
	text = f"{test} {getattr(test, 'description', '')}"
	# setUpClass / tearDownClass errors read "setUpClass (pkg.module.Class)"
	named = re.search(r"\(([\w.]+)\)", text)
	if named and class_sources.get(named.group(1)):
		return set(class_sources[named.group(1)])
	found = [m for m in sorted(modules, key=len, reverse=True) if m in text]  # setUpModule (pkg.module)
	return {found[0]} if found else {"?"}


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("--site", required=True)
	parser.add_argument("--modules", required=True, help="comma-separated dotted module names")
	parser.add_argument("--result", required=True)
	args = parser.parse_args()
	modules = [m for m in args.modules.split(",") if m]

	class_sources = defaultdict(set)
	tag_loads(class_sources)

	import frappe
	from frappe.commands import testing
	from frappe.testing import result as result_module
	from frappe.testing import runner as runner_module

	results = []
	run = runner_module.TestRunner.run

	def recording_run(self, suite):
		outcome = run(self, suite)
		results.append(outcome)
		return outcome

	runner_module.TestRunner.run = recording_run

	seconds = defaultdict(float)
	clock = {"mark": None}

	def timed(cls):
		start_test, stop_test = cls.startTest, cls.stopTest

		# Every second between two test events goes to the module of the test at
		# hand, so class and module fixtures (setUpClass runs before the first
		# startTest of its class) count toward the module that needs them.
		def charge(test):
			now = time.monotonic()
			if clock["mark"] is not None:
				seconds[min(modules_of(test, modules, class_sources))] += now - clock["mark"]
			clock["mark"] = now

		def timed_start(self, test):
			charge(test)
			return start_test(self, test)

		def timed_stop(self, test):
			charge(test)
			return stop_test(self, test)

		cls.startTest, cls.stopTest = timed_start, timed_stop

	timed(result_module.TestResult)  # the runner's resultclass

	# Warm every DocType meta right after the runner's own setup (which clears
	# the cache). In one long serial run the early integration tests warm them;
	# a worker holding only a few modules, on a cloned site whose cache is cold,
	# would otherwise send meta loads into tests that patch frappe.db.get_value
	# with doubles that only understand the calls under test.
	from frappe.testing import environment

	initialize = environment._initialize_test_environment

	def initialize_and_warm(site, config):
		initialize(site, config)
		for doctype in frappe.get_all("DocType", pluck="name"):
			frappe.get_meta(doctype)

	environment._initialize_test_environment = initialize_and_warm

	began = time.monotonic()
	crash = None
	try:
		frappe.init(site=args.site, sites_path=".")  # what the run-tests click command does before main()
		testing.main(site=args.site, module=modules)
	except SystemExit:
		pass  # main() exits 1 when a test failed; the recorded results say which
	except Exception as err:  # discovery or import failure before any suite ran
		crash = repr(err)
		print(f"worker crashed: {crash}", file=sys.stderr)

	failed = set()
	counts = {"failures": defaultdict(int), "errors": defaultdict(int)}  # module -> count
	for outcome in results:
		for kind in counts:
			for test, _ in getattr(outcome, kind):
				for module in modules_of(test, modules, class_sources):
					counts[kind][module] += 1
					failed.add(module)
	with open(args.result, "w") as fh:
		json.dump({
			"site": args.site,
			"modules": modules,
			"tests_run": sum(outcome.testsRun for outcome in results),
			"failed_modules": sorted(failed),
			"failure_counts": dict(counts["failures"]),
			"error_counts": dict(counts["errors"]),
			"crash": crash,
			"wall_seconds": round(time.monotonic() - began, 1),
			"module_seconds": {m: round(s, 2) for m, s in seconds.items()},
		}, fh, indent=1)


if __name__ == "__main__":
	main()
