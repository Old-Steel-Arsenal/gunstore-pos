"""Prove that a diff touches comments and docstrings only.

python3 scripts/ci/check_comment_only.py <base> <head> [--allow-added PATH ...]

<head> may be the word WORKTREE: compare <base> with the files on disk.

For every .py and .js file that differs between the two git revisions:

* .py: the AST must be identical once docstrings (the leading string of a module,
  class or function) are removed. Comments never reach the AST.
* .js: the token stream must be identical once comments are removed. Strings,
  template literals and regex literals count as single tokens.

Added, deleted or renamed .py/.js files fail the check (a comment sweep has no
reason to create or remove code), except paths named with --allow-added.
Other file types are listed but not judged. Exit status 1 on any failure.
"""
import ast
import subprocess
import sys

# A '/' after one of these starts a regex literal, otherwise it is a division.
REGEX_AFTER = {
	"", "(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";", "+", "-", "*", "%", "<", ">", "~", "^",
	"return", "typeof", "case", "in", "of", "delete", "void", "throw", "new", "else", "do",
}


def strip_docstrings(tree):
	for node in ast.walk(tree):
		if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
			body = node.body
			if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
					and isinstance(body[0].value.value, str):
				node.body = body[1:] or [ast.Pass()]
	return tree


def py_fingerprint(src):
	return ast.dump(strip_docstrings(ast.parse(src)), include_attributes=False)


def js_tokens(src):
	"""Non-comment tokens of a JS source. A heuristic tokenizer: enough to tell comments from code."""
	toks, i, n, last = [], 0, len(src), ""
	while i < n:
		c = src[i]
		if c.isspace():
			i += 1
		elif src.startswith("//", i):
			j = src.find("\n", i)
			i = n if j < 0 else j
		elif src.startswith("/*", i):
			j = src.find("*/", i + 2)
			i = n if j < 0 else j + 2
		elif c in "\"'":
			j = i + 1
			while j < n and src[j] != c:
				j += 2 if src[j] == "\\" else 1
			toks.append(src[i:j + 1])
			i, last = j + 1, "str"
		elif c == "`":
			j = i + 1
			while j < n and src[j] != "`":
				if src[j] == "\\":
					j += 2
				elif src.startswith("${", j):
					depth, j = 1, j + 2
					while j < n and depth:
						depth += (src[j] == "{") - (src[j] == "}")
						j += 1
				else:
					j += 1
			toks.append(src[i:j + 1])
			i, last = j + 1, "str"
		elif c == "/" and last in REGEX_AFTER:
			j, in_class = i + 1, False
			while j < n and src[j] != "\n":
				if src[j] == "\\":
					j += 2
					continue
				if src[j] == "[":
					in_class = True
				elif src[j] == "]":
					in_class = False
				elif src[j] == "/" and not in_class:
					break
				j += 1
			j += 1
			while j < n and src[j].isalpha():
				j += 1
			toks.append(src[i:j])
			i, last = j, "regex"
		elif c.isalnum() or c in "_$":
			j = i
			while j < n and (src[j].isalnum() or src[j] in "_$"):
				j += 1
			toks.append(src[i:j])
			i, last = j, src[i:j]
		else:
			toks.append(c)
			i, last = i + 1, c
	return toks


def git(*args):
	return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def show(rev, path):
	if rev == "WORKTREE":
		with open(path, encoding="utf-8") as f:
			return f.read()
	return git("show", f"{rev}:{path}")


def changed(base, head):
	"""[(status, path)] with status A/M/D/R (rename reported as the new path)."""
	rows = []
	spec = [base] if head == "WORKTREE" else [f"{base}...{head}"]
	for line in git("diff", "--name-status", "-M", *spec).splitlines():
		parts = line.split("\t")
		rows.append((parts[0][0], parts[-1]))
	return rows


def compare(path, before, after):
	"""None when the two sources differ in comments only, else a reason."""
	try:
		if path.endswith(".py"):
			same = py_fingerprint(before) == py_fingerprint(after)
		else:
			same = js_tokens(before) == js_tokens(after)
	except SyntaxError as e:
		return f"does not parse: {e}"
	return None if same else "executable content differs"


def main(argv):
	allow = set()
	args = []
	it = iter(argv)
	for a in it:
		if a == "--allow-added":
			allow.add(next(it))
		else:
			args.append(a)
	if len(args) != 2:
		print(__doc__)
		return 2
	base, head = args
	bad, checked, other = [], 0, []
	for status, path in changed(base, head):
		if not path.endswith((".py", ".js")):
			other.append(path)
			continue
		if status != "M":
			if path not in allow:
				bad.append((path, f"file {'added' if status == 'A' else 'deleted/renamed'}"))
			continue
		checked += 1
		why = compare(path, show(base, path), show(head, path))
		if why:
			bad.append((path, why))
	for path, why in bad:
		print(f"FAIL {path}: {why}")
	print(f"checked {checked} .py/.js files, {len(bad)} failing, {len(other)} other files changed (not judged)")
	return 1 if bad else 0


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
