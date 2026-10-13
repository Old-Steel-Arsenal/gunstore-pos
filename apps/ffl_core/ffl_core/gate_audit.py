"""Shared static-audit helper for the endpoint-gate tests.

Every module-level ``@frappe.whitelist`` endpoint in an app must open with a
recognized permission gate as its first executable statement (a docstring
doesn't count), unless it is on the app's reviewed OPEN_BY_DESIGN allowlist;
guest webhooks are exempt (their contract is the HMAC signature). This is the
repo-wide, executable form of the convention test_console_endpoints pins for
the distributor console — it makes the endpoint-gates adjudication table a
test, so an ungated endpoint merged later fails CI, not a security review.

Lives OUTSIDE any tests/ directory (which the scanner itself skips) so all
three per-app test modules import ONE implementation instead of copy-pasting
it. Each app's test supplies
its own gate vocabulary + allowlist and calls find_ungated_endpoints().
"""
from __future__ import annotations

import ast
from pathlib import Path

# The full recognized-gate vocabulary across all three apps. Any single app's
# endpoints only use a subset, but one shared list keeps the three tests from
# drifting; an app never has a *false* offender from an over-broad list, only
# (in principle) a missed one — which the adjudication review already covers.
DEFAULT_GATE_PATTERNS = (
    "frappe.only_for(",
    "frappe.has_permission(",
    "_gate(",
    "_check_perm(",
    "_check_manager",
    "_check_refund_perm(",
    "_dealer_for_session(",
    "_has_ship_role(",
    "frappe.get_roles(",
    # Extension SDK gate (ffl_core.extensions.require_enabled).
    # Only the roles= form is a gate — see _is_gate below.
    "require_enabled(",
)


def _is_gate(text: str, gate_patterns: tuple[str, ...]) -> bool:
    """A first statement counts as a gate when it names a known gate — except
    ``require_enabled(app)`` without ``roles=``, which only checks the store
    switch and lets any logged-in user through."""
    if "require_enabled(" in text and "roles=" not in text:
        return any(p in text for p in gate_patterns if p != "require_enabled(")
    return any(p in text for p in gate_patterns)


def _is_whitelist(deco: ast.expr) -> tuple[bool, dict]:
    target = deco.func if isinstance(deco, ast.Call) else deco
    if not (isinstance(target, ast.Attribute) and target.attr == "whitelist"):
        return False, {}
    kwargs = {}
    if isinstance(deco, ast.Call):
        for kw in deco.keywords:
            try:
                kwargs[kw.arg] = ast.literal_eval(kw.value)
            except (ValueError, SyntaxError):
                pass
    return True, kwargs


def _first_executable(node: ast.FunctionDef):
    body = node.body
    # A leading docstring is an Expr(Constant(str)) — not a statement that runs.
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return next(
        (s for s in body if not isinstance(s, (ast.Import, ast.ImportFrom))),
        None)


def find_ungated_endpoints(app: str, app_root: str | Path, *,
        open_by_design: set[str] = frozenset(),
        gate_patterns: tuple[str, ...] = DEFAULT_GATE_PATTERNS,
        trees: dict[Path, ast.Module] | None = None) -> list[str]:
    """Return "<dotted> (first stmt: …)" for every whitelisted endpoint in
    ``app`` whose first executable statement is not a recognized gate and
    which is not allow_guest or on ``open_by_design``. ``trees``: the modules
    under ``app_root`` already parsed ({path: ast.Module}) — the extension
    lint passes its own instead of having the tree walked and parsed again."""
    offenders: list[str] = []
    for dotted, node, guest in _endpoints(app, app_root, trees=trees):
        if guest or dotted in open_by_design:
            continue
        first = _first_executable(node)
        text = ast.unparse(first) if first is not None else ""
        if not _is_gate(text, gate_patterns):
            offenders.append(f"{dotted} (first stmt: {text[:70]!r})")
    return offenders


def _endpoints(app: str, app_root: str | Path, *,
        trees: dict[Path, ast.Module] | None = None):
    """Yield (dotted, FunctionDef, allow_guest) for every module-level
    ``@frappe.whitelist`` endpoint in ``app``. Shared by the two audits below
    so "what counts as an endpoint" is stated once."""
    root = Path(app_root)
    for path in sorted(root.rglob("*.py") if trees is None else trees):
        rel = path.relative_to(root)
        if "tests" in rel.parts or path.name.startswith("test_"):    # inside the app: a checkout may sit under any folder
            continue
        tree = ast.parse(path.read_text(encoding="utf-8")) if trees is None else trees[path]
        module = f"{app}." + ".".join(rel.with_suffix("").parts)
        module = module.removesuffix(".__init__")
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            gated = False
            guest = False
            for deco in node.decorator_list:
                ok, kwargs = _is_whitelist(deco)
                if ok:
                    gated = True
                    guest = bool(kwargs.get("allow_guest"))
            if gated:
                yield f"{module}.{node.name}", node, guest


def find_shadowed_endpoints(app: str, app_root: str | Path, *,
        trees: dict[Path, ast.Module] | None = None) -> list[str]:
    """Return "<dotted> -> <what it resolves to>" for every endpoint whose
    MODULE ATTRIBUTE is no longer the decorated function.

    Frappe resolves ``/api/method/<dotted>`` by attribute, then checks the
    resolved object against its whitelist registry — so a later module-level
    assignment to an endpoint's own name silently replaces the endpoint, and
    every call 403s with "Function <other name> is not whitelisted". The
    static gate audit cannot see it: the decorator is still right there on the
    def. `ffl_core.api.atf_link.create_party_from_atf` was dead this way for a
    week (2026-09-11 .. 09-18) — a "public name for extensions" alias written
    over the endpoint it was meant to sit beside.

    Importing is the point: this check only has teeth at runtime.
    """
    import frappe

    offenders: list[str] = []
    for dotted, _node, _guest in _endpoints(app, app_root, trees=trees):
        try:
            fn = frappe.get_attr(dotted)
        except Exception as exc:                       # unimportable is its own bug
            offenders.append(f"{dotted} -> cannot resolve ({exc})")
            continue
        if fn not in frappe.whitelisted:
            offenders.append(f"{dotted} -> {getattr(fn, '__name__', fn)!r}, not whitelisted")
    return offenders
