"""T2c — compliance isolation guard (retail display normalizers ⟂ FastBound).

The retail DISPLAY normalizers in mapping.py (layer B) must NEVER reach the
FastBound A&D push / reverse-import path: the bound book records caliber and
model exactly as marked on the gun.  This locks that separation at the source
level so a future edit cannot wire a display table into the compliance path
unnoticed.  (Regression guard: it passes by construction today and fails the
moment the isolation is broken.)

Covers ``_canonical_caliber`` and
``_canonical_model`` (Garand dedup) — same rule, same guard.
"""
from __future__ import annotations

import inspect

from frappe.tests.utils import FrappeTestCase

import ffl_integrations.fastbound.client as fb_client
import ffl_integrations.fastbound.importer as fb_importer

# The layer-B display symbols that must stay out of the FastBound path.
_FORBIDDEN = (
    "_canonical_caliber", "_CALIBER_CANONICAL",
    "_canonical_model", "_MODEL_CANONICAL",
)


class CaliberFastBoundIsolation(FrappeTestCase):
    def test_display_normalizer_absent_from_fb_client(self):
        src = inspect.getsource(fb_client)
        for name in _FORBIDDEN:
            self.assertNotIn(
                name, src,
                f"{name} must not appear in fastbound/client.py — "
                "FB caliber/model is recorded as-marked, never normalized.",
            )

    def test_display_normalizer_absent_from_fb_importer(self):
        src = inspect.getsource(fb_importer)
        for name in _FORBIDDEN:
            self.assertNotIn(
                name, src,
                f"{name} must not appear in fastbound/importer.py — "
                "FB caliber/model is recorded as-marked, never normalized.",
            )

    def test_fb_modules_do_not_import_woo_mapping(self):
        for mod in (fb_client, fb_importer):
            src = inspect.getsource(mod)
            self.assertNotIn(
                "woocommerce.mapping", src,
                f"{mod.__name__} must not import the Woo display mapping module.",
            )

    def test_fb_push_caliber_stays_as_marked(self):
        # Positive lock: the push path still derives caliber from Serial No →
        # Item verbatim (as-marked fallback), with no normalization wrapper.
        src = inspect.getsource(fb_client)
        self.assertIn('item.get("caliber")', src)

    def test_fb_owns_its_own_model_table(self):
        # The FastBound layer canonicalises models too, but with its OWN table
        # (aggregation keys, not display names).  Assert the two stay separate:
        # the FB table is reachable from the FB side and the Woo display name
        # "M1 Garand" never leaks into it.
        from ffl_integrations.fastbound.normalize import canon_model

        self.assertEqual(canon_model("U.S. Rifle M1"), "M1")
        self.assertNotEqual(canon_model("U.S. Rifle M1"), "M1 Garand")
        self.assertEqual(canon_model("U.S. Carbine M1"), "M1CARBINE")


class ModelTableAgreement(FrappeTestCase):
    """The same model equivalences are spelled out in TWO tables on purpose:

    * ``ffl_woo_sync…mapping._MODEL_CANONICAL`` — retail DISPLAY names;
    * ``ffl_integrations.fastbound.normalize._MODEL`` — bound-book AGGREGATION
      keys.

    They may not be merged (different output spaces, and ffl_woo_sync must not
    import ffl_integrations), so the house rule for a duplicated authority
    applies: **edit both or none**, with a test pinning the agreement.  Same
    pattern as ``TestDropshipStatusAgreement`` for the dropship status set.

    What "agreement" means here is NOT equal tables — the shop deliberately
    folds only the families it has merged terms for, so FastBound legitimately
    knows more.  The two invariants actually asserted are:

    * whatever the shop folds together, FastBound must also read as one gun;
    * whichever families the shop has taken on, it must fold them WHOLE.

    A family the shop has not touched at all is skipped: that is backlog (the
    M1 Carbine and 1911 families today), not a disagreement.  An earlier
    version of this class claimed to catch "a third spelling added to only one
    side" outright; it did not, because a single-entry group satisfies a
    "one FastBound key" check vacuously.  The whole-family assertion is what
    closes that.
    """

    def _tables(self):
        from ffl_integrations.fastbound.normalize import canon_model
        from ffl_woo_sync.woocommerce.mapping import (
            _MODEL_CANONICAL, _canonical_model, _norm_model_key,
        )
        return canon_model, _MODEL_CANONICAL, _canonical_model, _norm_model_key

    def test_spellings_the_shop_folds_together_are_one_gun_to_fastbound(self):
        # Woo → FastBound.  Every spelling folded onto one display name must
        # land on ONE FastBound key.  Teach Woo a spelling FastBound splits and
        # this fails.
        canon_model, table, _, _ = self._tables()
        groups: dict[str, set[str]] = {}
        for key, display in table.items():
            groups.setdefault(display, set()).add(canon_model(key))
        for display, fb_keys in groups.items():
            self.assertEqual(
                len(fb_keys), 1,
                f"the shop folds these onto {display!r} but FastBound splits "
                f"them into {sorted(fb_keys)} — edit both or none.",
            )

    def test_a_family_the_shop_folds_at_all_must_be_folded_whole(self):
        # FastBound → Woo, and the assertion that actually has teeth: if the
        # shop folds ANY spelling of a family FastBound recognises, it must fold
        # EVERY spelling of it.  Partial adoption is the failure that ships a
        # duplicate filter entry, which is the bug this whole change exists to
        # fix.  Families the shop has not taken on at all are skipped — that is
        # backlog, not disagreement.
        canon_model, table, display, norm_key = self._tables()
        from ffl_integrations.fastbound.normalize import _MODEL as FB_MODEL

        families: dict[str, set[str]] = {}
        for spelling, fb_key in FB_MODEL.items():
            families.setdefault(fb_key, set()).add(spelling)

        for fb_key, spellings in families.items():
            spellings = spellings | {fb_key}
            if not any(norm_key(s) in table for s in spellings):
                continue  # shop has not merged this family yet — fine
            displays = {display(s) for s in spellings}
            self.assertEqual(
                len(displays), 1,
                f"FastBound reads {sorted(spellings)} as one gun ({fb_key}), "
                f"but the shop shows them as {sorted(displays)} — folding half "
                "a family leaves a duplicate filter entry.",
            )

    def test_both_layers_keep_the_carbine_apart_from_the_garand(self):
        # The load-bearing invariant of the whole M1 fold, asserted on both
        # sides so neither layer can quietly merge two different firearms.
        canon_model, _, display, _ = self._tables()
        for carbine in ("U.S. Carbine M1", "U.S.CARBINE M1", "U.S. CARBINE M1"):
            self.assertNotEqual(canon_model(carbine), canon_model("U.S. Rifle M1"))
            self.assertNotEqual(display(carbine), "M1 Garand")
