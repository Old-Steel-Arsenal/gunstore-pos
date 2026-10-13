"""Unit tests for scripts/fetch_fastbound_spec.sh, with a fake `curl` on PATH (no network).

What is tested is the script's own judgement: a response that is not JSON leaves the old copy and
fails the run; changed content keeps the previous copy as <name>.prev.json; unchanged content
leaves .prev.json alone.

python3 -m unittest discover -s scripts/ci -p 'test_*.py'
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "fetch_fastbound_spec.sh"
NAMES = ("v1-account", "v1-partner", "v1-webhookmodels")

# Writes $FAKE_BODY to the file after -o, like `curl -fsSL ... -o FILE`.
CURL_SHIM = """#!/bin/sh
while [ $# -gt 0 ]; do
	[ "$1" = "-o" ] && { printf '%s' "$FAKE_BODY" > "$2"; exit 0; }
	shift
done
exit 2
"""


class FetchFastboundSpec(unittest.TestCase):
	def setUp(self):
		self.tmp = Path(tempfile.mkdtemp())
		self.addCleanup(shutil.rmtree, self.tmp, True)
		# The script cd's to its own parent's parent, so give it a repo-shaped root.
		(self.tmp / "scripts").mkdir()
		shutil.copy(SCRIPT, self.tmp / "scripts" / SCRIPT.name)
		self.shims = self.tmp / "shims"
		self.shims.mkdir()
		curl = self.shims / "curl"
		curl.write_text(CURL_SHIM)
		curl.chmod(0o755)
		self.dir = self.tmp / "docs" / "fastbound"

	def run_script(self, body):
		env = dict(os.environ, FAKE_BODY=body, PATH=f"{self.shims}:{os.environ['PATH']}")
		return subprocess.run(["bash", str(self.tmp / "scripts" / SCRIPT.name)],
			env=env, capture_output=True, text=True)

	def test_bad_json_leaves_the_old_file_and_fails(self):
		self.assertEqual(self.run_script('{"v": 1}').returncode, 0)
		r = self.run_script("<html>502 Bad Gateway</html>")
		self.assertNotEqual(r.returncode, 0)
		self.assertIn("did not return valid JSON", r.stderr)
		for n in NAMES:
			self.assertEqual((self.dir / f"{n}.json").read_text(), '{"v": 1}')
			self.assertFalse((self.dir / f"{n}.prev.json").exists())

	def test_changed_content_keeps_the_previous_file_as_prev(self):
		self.run_script('{"v": 1}')
		self.assertEqual(self.run_script('{"v": 2}').returncode, 0)
		for n in NAMES:
			self.assertEqual((self.dir / f"{n}.json").read_text(), '{"v": 2}')
			self.assertEqual((self.dir / f"{n}.prev.json").read_text(), '{"v": 1}')

	def test_unchanged_content_does_not_overwrite_prev(self):
		self.run_script('{"v": 1}')
		self.run_script('{"v": 2}')
		r = self.run_script('{"v": 2}')
		self.assertEqual(r.returncode, 0)
		self.assertIn("unchanged", r.stdout)
		for n in NAMES:
			self.assertEqual((self.dir / f"{n}.prev.json").read_text(), '{"v": 1}')

	def test_no_temp_files_are_left_behind(self):
		self.run_script('{"v": 1}')
		self.run_script("not json")
		self.assertEqual(list(self.dir.glob(".fetch.*")), [])


if __name__ == "__main__":
	unittest.main()
