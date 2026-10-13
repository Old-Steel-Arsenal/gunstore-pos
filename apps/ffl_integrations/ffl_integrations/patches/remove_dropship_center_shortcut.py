"""The Drop-ship Center moved to the osa_dropship extension (1.5): take its card
off the Settings workspace, whose saved copy still links the page the core no
longer ships (a Page link that no longer resolves fails every save of the
workspace). The extension puts the card back when it is installed and on.
One-time, idempotent."""
from ffl_core.workspace_shortcuts import remove_shortcut


def execute():
	remove_shortcut("Drop-ship Center")
