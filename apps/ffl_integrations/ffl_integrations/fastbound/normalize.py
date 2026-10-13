"""Caliber / manufacturer / model canonicalisation for the bound-book importer.

Dealer bound books spell the same thing many ways — "45 AUTO" vs "45ACP",
"M1911 A1" vs "M1911A1", "Harrington and Richardson" vs "H&R". Model-mode
aggregation keys on model+caliber, so without canonicalisation one model splits
into several Items. Each value is `squash`ed to a punctuation/space-insensitive
key, looked up in a small alias table, and falls back to the squashed form.

`squash` is the SAFE layer — it only removes spacing/punctuation, so "M1911 A1"
== "M1911A1" and ".45 ACP" == "45ACP" automatically, with no risk of merging two
different guns. The alias tables are the JUDGEMENT layer: list ONLY true
equivalences (a wrong alias silently merges two different firearms). They are
deliberately small and meant to be EXTENDED as you meet new spellings — add a
line to the right bucket.
"""
from __future__ import annotations

import re


def squash(s: str | None) -> str:
	"""Uppercase + keep only A–Z0–9 so spacing/punctuation never splits a key:
	"M1911 A1" and "M1911A1" → "M1911A1"; ".45 ACP" and "45ACP" → "45ACP".
	"""
	return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


# ── caliber: canonical → the squashed spellings that mean it (SAFE equivalences) ──
# Only well-established equivalences. NOTE: never alias a bare ".30" → "30-06":
# .30 (Garand) and .30 Carbine are different cartridges — let caliber keep them
# apart. The same goes for any ambiguous bare number.
# Canonical keys are squash-clean (A–Z0–9) so the Item code stays alphanumeric.
_CALIBER_GROUPS: dict[str, list[str]] = {
	"45ACP":   ["45ACP", "45AUTO", "45ACPAUTO"],
	"9MM":     ["9MM", "9MMPARA", "9MMPARABELLUM", "9MMLUGER", "9LUGER", "9X19", "9X19MM", "9MMX19"],
	"762X54R": ["762X54R", "762X53R", "762X54", "762X53MMR", "762X54MMR"],
	"762X39":  ["762X39", "762X39MM"],
	# .45 Colt (a.k.a. .45 Long Colt) — same cartridge, several spellings.
	"45COLT":  ["45COLT", "45LONGCOLT", "45COLTLONGCOLT", "45COLT45LONGCOLT"],
	# 5.56 NATO renderings. NOT merged with .223 Remington (distinct chamber).
	"556":     ["556", "556NATO", "556MM", "556X45", "556X45MM", "556NATOMM"],
	# .357 Magnum. Bare "357" is .357 Mag (.357 SIG squashes to "357SIG", unaffected).
	"357MAG":  ["357MAG", "357", "357MAGNUM"],
	# 8mm Mauser (7.92×57 / 8×57 IS JS) — verbose distributor strings → one key.
	# Conservative: only 7.92/8×57 forms; never 7.62 or 7.65 (different cartridges).
	"8MM":     ["8MM", "8MMMAUSER", "8MMMAUSER792X57MM8X57ISJS", "792X57", "792MM", "8X57", "8X57IS", "8X57ISJS"],
}
_CALIBER = {sq: canon for canon, sqs in _CALIBER_GROUPS.items() for sq in sqs}

# ── manufacturer: squashed spelling → canonical display name (stamped on Serial No) ──
# The item key drops manufacturer; this only canonicalises the per-unit maker so
# the bound book shows one consistent name per maker.
_MANUFACTURER: dict[str, str] = {
	"HRARMSCO": "H&R",
	"HARRINGTONANDRICHARDSONARMSCO": "H&R",
	"HARRINGTONRICHARDSON": "H&R",
	"COLTSPTFAMFGCO": "Colt",
	"FABRIQUENATIONALEHERSTAL": "FN Herstal",
	"FABRIQUENATIONALE": "FN Herstal",
	"FABRIQUENATIONALEDARMESDEGUERREHERSTAL": "FN Herstal",
	"SIGSAUERINC": "Sig Sauer",
	"SIGSAUER": "Sig Sauer",
}

# ── model: squashed spelling → canonical model (CONSERVATIVE; extend as needed) ──
# Keep DIFFERENT guns apart — e.g. the M1 Garand ("M1", "U.S. Rifle M1") must NOT
# fold into the M1 Carbine ("U.S. Carbine M1").
#
# ⚠ SECOND COPY: the retail side keeps its own model table
# (``ffl_woo_sync…mapping._MODEL_CANONICAL``) because it needs display *names*
# rather than aggregation keys, and that app may not import this one.  **Edit
# both or none** — a spelling taught to only one side makes the two layers
# disagree about what is the same gun.  Agreement is pinned by
# ``ModelTableAgreement`` in ffl_woo_sync/tests/test_caliber_fb_isolation.py.
_MODEL: dict[str, str] = {
	"USRIFLEM1": "M1",            # M1 Garand also shows up as "U.S. Rifle M1"
	"USCARBINEM1": "M1CARBINE",   # … kept distinct from the M1 Garand
	"USCARBINE": "M1CARBINE",
	"CARBINEM1": "M1CARBINE",
	# 1911 family — fold the "M" prefix and the "U.S. Army"/"Government" service
	# descriptor (NOT every 1911A1 is U.S. Army), but KEEP 1911 / 1911A1 / 1911A2
	# distinct (different models). Bare "1911"/"1911A1" already canonicalise to
	# themselves; these map the prefixed / descriptor-laden spellings onto them.
	"M1911": "1911",
	"M1911GOVERNMENT": "1911",
	"M1911A1": "1911A1",
	"M1911A1USARMY": "1911A1",
	"1911A1USARMY": "1911A1",
	"M1911A2": "1911A2",
	# Spelling/descriptor folds from the real bound-book preview. SAME-model spelling
	# variants only — genuinely different guns are kept apart per the dealer's call:
	# SKS≠Type56, King Cobra≠King Cobra Talo, K98k≠98 all stay distinct (no alias).
	"MODEL1903": "1903",           # Springfield "Model 1903" (Colt 1903 stays apart by caliber)
	"NO4MK1LEEENFIELD": "NO4MK1",  # Lee-Enfield No.4 Mk1 spelled out
	"MKVI": "MARKVI",              # Webley Mark VI (.45ACP vs .455 still split by caliber)
	"HIGHPOWER": "HIPOWER",        # FN/Browning Hi-Power …
	"P35": "HIPOWER",              # … and its FN designation P35
	"MOD98": "98",                 # Mauser "Model 98" == "98" (K98k kept separate)
}


def canon_caliber(raw: str | None) -> str:
	"""Canonical caliber key for aggregation."""
	sq = squash(raw)
	return _CALIBER.get(sq, sq)


def canon_model(raw: str | None) -> str:
	"""Canonical model key for aggregation."""
	sq = squash(raw)
	return _MODEL.get(sq, sq)


def canon_manufacturer(raw: str | None) -> str:
	"""Canonical *display* maker for the Serial No (the Item key drops manufacturer)."""
	return _MANUFACTURER.get(squash(raw), (raw or "").strip())
