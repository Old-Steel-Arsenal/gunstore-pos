"""ATF FFL number shape + license-type classification.

Pure module: no frappe import, no I/O. Both `ffl_woo_sync` (webhook backfill,
order validate) and `ffl_integrations` (FastBound contact) need to answer "is
this a Type 03 Curio & Relic license?", and both already depend on `ffl_core`
— so the answer lives here, once.

Two DIFFERENT jobs live in this module and must not be conflated:

* **Input acceptance** — `FFL_NUMBER_RE` / `CR_LICENSE_RE`. Strict: the
  hyphenated ATF form, nothing else. Used to reject what a human typed.
* **Classification** — `normalize()` / `license_type_group()` /
  `is_cr_license()`. Tolerant: accepts hyphenated or already-normalized input,
  because it runs over values that were stored by other systems (FastBound
  keeps `599888019D26128`; our suppliers write `5-99-888-01-9D-26128`).

**Do not "unify" these into one rule.** It looks like duplication and it is
not; each direction of the collapse breaks something real:

* Make CLASSIFICATION strict (hyphens required) and it stops recognising the
  form FastBound actually stores. `fflNumber` lives in the live book as
  `599888019D26128`, and `find_contact_by_ffl_number` compares normalized —
  so a strict classifier fails to match the existing contact and a second one
  is created for the SAME licensee. On an A&D book that reads as two different
  transferees receiving firearms.
* Make INPUT ACCEPTANCE tolerant and a clerk can save a shape WooCommerce can
  never produce (the Woo side rejects unhyphenated in both its JS and PHP
  gates — measured, not inferred), so the two systems stop agreeing about what
  a licence number even is.

The same concept genuinely needs different strictness at different layers.
Measured behaviour, for the next person tempted to align them:

    "123456037C89012"   Woo JS isCrLicense -> false    (its normalizeCrInput
                        Woo PHP preg_match -> 0         strips whitespace and
                        POS CR_LICENSE_RE  -> False     upper-cases, but
                        POS is_cr_license  -> True      deliberately does NOT
                                                        insert hyphens)

`\\Z`, not `$` (load-bearing)
----------------------------
The contract regex is shared verbatim across JS / PHP / Python. `$` does not
mean the same thing in all three: JS rejects a trailing newline, while PHP and
Python both match before a final newline. `"1-23-456-03-7C-89012\\n"` (21 chars)
would therefore be blessed as a valid 03 license here, stored with the newline
intact, and then reach FastBound as the 15-char normalized form — a persisted
value that no longer equals the value we validated, and an `ATF FFL Record`
name with a newline in it. `\\Z` anchors at the true end of string. The PHP
side solves the same problem with the `/D` modifier. See test_ffl_number.py.

This module deliberately does NOT import `fastbound.client._normalize_ffl`, and
that helper does not import this one: FastBound's normalization serves wire
formatting (`externalId` / `fflNumber`), this one serves type classification.
Same semantics today, separate responsibilities, no cross-app dependency.
"""
from __future__ import annotations

import re

# Canonical hyphenated ATF license: region-district-county-TYPE-expiration-sequence.
FFL_NUMBER_RE = re.compile(r"^(\d)-(\d{2})-(\d{3})-(\d{2})-([0-9A-Za-z]{2})-(\d{5})\Z")

# The same shape with the type group pinned to 03 (Curio & Relic). Frozen as
# contract C2 — keep byte-identical to the JS/PHP copies apart from the anchor.
CR_LICENSE_RE = re.compile(r"^\d-\d{2}-\d{3}-03-[0-9A-Za-z]{2}-\d{5}\Z")

# An ATF license is exactly this many alphanumerics once punctuation is dropped.
_NORMALIZED_LEN = 15
# Zero-based slice of the 2-digit license-type group inside the normalized form.
_TYPE_SLICE = slice(6, 8)
CR_TYPE_CODE = "03"

# How a Type 03 is written into `ATF FFL Record.license_type`.
#
# The label form, not the bare code, because the eZ Check importer
# (ffl_integrations.atf.scraper) is the established and higher-volume writer of
# that column and already uses labels. Two spellings in one column is a trap
# that fails silently: today every reader takes the first two characters, but
# the next one to do an equality match — a report GROUP BY, a Desk filter, a
# `== "03"` — would simply return fewer rows, with no error to notice.
#
# Kept identical to scraper._LICENSE_TYPE_LABELS["03"]; that equality is pinned
# by a test rather than by an import, so no cross-app dependency is created for
# the sake of one string.
#
# ⚠ CHANGING THIS STRING FAILS A TEST IN A DIFFERENT APP, ON PURPOSE:
#   apps/ffl_integrations/ffl_integrations/tests/test_fastbound_cr_contact.py
#   -> LicenseTypeLabelConsistencyTest
# That is the invariant doing its job, not a stray unrelated breakage. If you
# genuinely mean to change the spelling, change it in BOTH places (here and
# scraper._LICENSE_TYPE_LABELS) — the column has two writers, and two spellings
# in it fail silently, by returning fewer rows to any equality match rather than
# by erroring. Running only the ffl_woo_sync suite will NOT catch you.
CR_LICENSE_TYPE_LABEL = "03 - Collector of Curios and Relics"


def normalize(value: str | None) -> str:
	"""Drop non-alphanumerics and upper-case, so hyphens/spacing don't defeat
	comparisons: ``1-23-456-03-7C-89012`` -> ``123456037C89012``."""
	if not value:
		return ""
	return "".join(ch for ch in str(value) if ch.isalnum()).upper()


def license_type_group(value: str | None) -> str:
	"""The 2-character license-type group (segment 4), or "" when the input is
	not license-shaped.

	Length must be exactly 15: a longer string is not a license plus noise we
	may ignore, it is a value we do not understand — and slicing a confident
	"03" out of it would route a firearm down the collector path on the
	strength of a guess.
	"""
	normalized = normalize(value)
	if len(normalized) != _NORMALIZED_LEN:
		return ""
	return normalized[_TYPE_SLICE]


def is_cr_license(value: str | None) -> bool:
	"""True when this is a Type 03 (Curio & Relic) collector licence.

	The single source of truth for `Woo Online Order.is_cr_transfer`, the
	FastBound contact branch, and the disposition note. Accepts hyphenated or
	normalized input; rejects anything that isn't license-shaped.
	"""
	return license_type_group(value) == CR_TYPE_CODE
