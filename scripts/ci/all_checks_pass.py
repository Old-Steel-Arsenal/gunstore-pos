"""Verdict for the all-checks-pass job in release-image.yml.

Reads NEEDS (the workflow's toJSON(needs)), TESTS_REQUIRED and DRAFT ("true"/"false").
A draft pull request always fails: nothing ran, and a pass here would sit on
the commit as a green required check once the PR is marked ready.
A gate that failed or was cancelled fails the verdict. A skipped gate passes
only where skipping is legitimate: the test jobs on runs that do not need them
(plain develop PRs, develop pushes), and release-version (release PRs into main only).
api-docs-freshness and build always run.
"""
import json
import os
import sys

ALWAYS_RUN = {"api-docs-freshness", "build"}


def problems(needs, tests_required, draft=False):
	if draft:
		return ["draft pull request: the checks run once it is marked ready for review"]
	bad = []
	for job, info in sorted(needs.items()):
		result = info.get("result")
		if result in ("failure", "cancelled"):
			bad.append(f"{job}: {result}")
		elif result == "skipped" and (job in ALWAYS_RUN or tests_required):
			bad.append(f"{job}: skipped, but this run must execute it")
		elif result not in ("success", "skipped"):
			bad.append(f"{job}: unexpected result {result!r}")
	return bad


def main():
	needs = json.loads(os.environ["NEEDS"])
	bad = problems(needs, os.environ.get("TESTS_REQUIRED") == "true", os.environ.get("DRAFT") == "true")
	print("gates:", {job: info.get("result") for job, info in sorted(needs.items())})
	for line in bad:
		print(f"::error::{line}")
	return 1 if bad else 0


if __name__ == "__main__":
	sys.exit(main())
