"""Add the "GunBroker (Marketplace)" shortcut to the Settings workspace,
next to the WooCommerce one — GunBroker is the third sales channel and its
Single is the only way to switch the channel on.

Half of the two-landing-site contract every new workspace shortcut needs (see
workspace_shortcuts.py for the full mechanics):

  fresh install   -> workspace/settings/settings.json carries the shortcut.
                     This patch does NOT run: install_app passes
                     set_as_patched=True, so patches.txt lines are logged
                     without executing.
  existing site   -> this patch runs on `bench migrate`. The JSON does NOT
                     reach it: modules/import_file.py skips a non-DocType whose
                     DB `modified` is at or past the file's.

Do NOT bump the JSON's `modified` to force a re-sync instead: import_doc would
delete_old_doc() and re-insert the workspace, discarding shop customizations.

Custom-app Settings singles never show up on Frappe's curated settings page
either, so without this the channel is reachable only by typing the URL.
One-time, idempotent.
"""
from ffl_core.workspace_shortcuts import ensure_shortcut

_LABEL = "GunBroker (Marketplace)"
_DOCTYPE = "GunBroker Settings"
_ANCHOR = "WooCommerce (Online Store)"  # the other online sales channel


def execute():
	# Land it right after the WooCommerce shortcut so the three sales
	# channels read as one group.
	ensure_shortcut(_LABEL, _DOCTYPE, anchor=_ANCHOR, block_id="sc_gunbroker_settings",
		color="Blue")
