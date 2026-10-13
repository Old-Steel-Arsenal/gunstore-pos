"""The summary-card strip every ffl_core report puts above its table.

Frappe's `report_summary` is a list of plain dicts, so a second report needing
one reaches for copy-and-paste — and the thing that gets copied is not the dict
shape, which is Frappe's, but the CONVENTIONS: that a zero-count flag reads
"✓ Clean" in green rather than "0" in grey, and that a non-zero one is red. Two
copies of a convention are two conventions the first time somebody edits one,
and the reader of a red "0" has no way to know which report is lying.

So there is one copy, here, and the reports import it.
"""
from __future__ import annotations

from frappe import _


def card(value, label, datatype=None, indicator="Grey"):
	"""One summary card. `datatype` is Frappe's formatter hint (Currency /
	Percent / …) and is omitted for a value that is already display text."""
	out = {"value": value, "label": label, "indicator": indicator}
	if datatype:
		out["datatype"] = datatype
	return out


def flag_card(count, label):
	"""A data-flag count: 0 → green "✓ Clean"; anything else → the red count.

	Deliberately not a plain zero. A grid of numbers where the good answer is
	"0" trains the eye to skim past the column, which is the opposite of what a
	flag is for."""
	clean = count == 0
	return {"value": _("✓ Clean") if clean else count, "label": label,
		"indicator": "Green" if clean else "Red"}
