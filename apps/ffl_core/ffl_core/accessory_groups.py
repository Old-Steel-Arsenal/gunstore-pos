"""Title rules for filing a product — the FALLBACK only (catalog master M3b).

The catalog master (the Data Service) decides every catalog product's category;
``ffl_integrations.catalog_master.classify`` reads it. These rules file a product
only when the master has no answer (the service unreachable, or a product it has
not classified yet): the service's class says firearm / ammunition / part /
accessory, and the title narrows it to a leaf group — accessories (Magazine,
Knife, Slings & Mounts, Sights, Optic Mounts …), gun parts (Uppers, Barrels,
Triggers …) and ammunition (Pistol / Rifle / Shotgun / Rimfire Ammo). The same
rules, ported, are the Data Service's own title rules (catalog/classify.py)."""
from __future__ import annotations

import re

import frappe

_HANDGUARD = re.compile(r"handguard|hndgrd|hand ?guard|m-?lok|\brail\b|forend|fore-end|free ?float", re.I)
_MAGAZINE = re.compile(r"\bpmag\b|magazine|\b\d+\s*(rd|rnd|round)s?\b", re.I)
_KNIFE = re.compile(r"\bknife\b|\bknives\b|folder|fixed blade", re.I)
_STOCK_GRIP = re.compile(r"\bstock\b|\bbraces?\b|\bstk\b|cheek|recoil pad|buttpad|butt pad|\bgrips?\b|\bgrp\b|pistol grip", re.I)
_SLING = re.compile(r"\bslings?\b|swivel|\bqdm\b|sling mount|sling adapter|sling plate", re.I)
# An iron sight names itself (MBUS, front/rear, flip-up, night sights); a bare
# "Sight" is how electronic optics are titled too ("Enclosed Rifle Sight – Red MRS").
_IRON_SIGHT = re.compile(r"\bmbus\b|iron sight|back ?up sight|\bbus\b|fro?n?t (flip )?(sight|sght)|"
	r"rear (flip )?(sight|sght)|sight (front|rear)|flip ?up sight|night sights?|sight set", re.I)
_NOT_IRON = re.compile(r"laser|red ?dot|reflex|holographic|scope|\bdot\b|magnifier|thermal|night vision|"
	r"mount|\bmoa\b|\bmrs\b|\bcrs\b|\bspr\b|enclosed|solar|plate", re.I)

# Optic mounts and adapters: under Optic, a title whose FIRST product word is a
# mount word ("Adapter Plate – 509 Series Sight to Glock MOS", "Spacer for 510C
# Reflex Sight") — a sight that merely comes with a mount ("Micro Red Dot … QD
# Mount") names the optic first. Elsewhere only unmistakable optic-mount words.
_MOUNT_WORD = re.compile(r"\bmounts?\b|\bmnt\b|adapter|adaptor|\bplate\b|spacer|riser|\brings?\b|\bbases?\b|cantilever", re.I)
_OPTIC_WORD = re.compile(r"\bdot\b|\bmoa\b|reticle|magnifier|\bscope\b|reflex|holographic|\bprism\b|"
	r"\bsights?\b|laser|thermal|monocular|binocular|rangefinder", re.I)
_OPTIC_MOUNT = re.compile(r"optic (mount|plate|riser)|scope (mount|rings?|base)|red ?dot (mount|riser)|"
	r"\brings\b|\briser\b|adapter plate|\bspacer\b", re.I)
# A cheek riser is stock furniture and a light's rings hold a light (2026-09-25).
_NOT_OPTIC_MOUNT = re.compile(r"cheek|\blight (mount|mnt|rings?)\b|weapon light", re.I)


# Gun-part leaves. A title naming what it is wins over the department's default;
# an explicit handguard / barrel word wins over a caliber or length.
_UPPER = re.compile(r"\buppers?\b|\buppr\b|\bupr\b|\burg\b|conversion kit|\bconv kit|"
	r"caliber (x-?change|conversion)", re.I)
_HANDGUARD_WORD = re.compile(r"handguard|hndgrd|hand ?guard", re.I)
_BARREL = re.compile(r"\bbarrels?\b|\bbbl\b|chrome lined|\bchf\b|\bhbar\b|pencil profile|"
	r"gov(ernment)? profile|\b1[:/](?:[5-9]|1\d|2[0-4])(?:\.\d+)?\b", re.I)  # twist 1:5–1:24, never "lower 1/3"
# Gas system parts name the barrel they fit ("GAS BLOCK(.750 BBL)"); no own leaf yet.
_GAS = re.compile(r"gas (block|tube|key)", re.I)
_BCG = re.compile(r"bolt carrier|\bbcg\b|charging handle|\bchg\.? ?h(an)?dle?|\bbolt (assembly|assy|carrier)", re.I)
_BRACE = re.compile(r"\bbraces?\b", re.I)
_LPK_KIT = re.compile(r"lower (parts|part|build) kit", re.I)
_TRIGGER = re.compile(r"\btrigger|\btrg\b|\bfcg\b|fire control|trigrtech", re.I)
_LOWER_PARTS = re.compile(r"\blpk\b|lower parts|buffer|takedown pin|pivot pin|"
	r"safety selector|\bselector\b|mag(azine)? (catch|release)|bolt catch|receiver extension", re.I)
_MUZZLE = re.compile(r"\bmuzzle (brake|device|devices)\b|\bbrake\b|compensator|\bcomp\b|"
	r"flash ?(hider|suppressor|can)|thread protector|thread(ed)? mount|direct thread|quick ?mount|"
	r"\bhub\b|suppressor mount|\basr mount|blast (shield|can)|linear comp|\bpiston\b|\bmb\b|"
	r"thr(ead)? prot", re.I)
# A complete upper names its caliber (length alone is also a handguard's).
_CALIBER = re.compile(r"5\.56|\b223\b|\b300 ?(blk|blackout|bo)\b|\b308\b|7\.62|6\.5|\b6mm\b|\b9mm\b|"
	r"22 ?lr|\b\d{2,3} ?(arc|grendel|legend|socom|beowulf)\b|\b556\b|\b762", re.I)
# Not a gun part at all, whatever part word it carries (a barrel cover, a trigger
# lock, a vise block, black-powder gear).
_NOT_A_PART = re.compile(r"\bcover\b|\bwrap\b|\btags?\b|\btools?\b|wrench|\bshim|cleaning|pouch|"
	r"\bcase\b|muzzleloader|muzzle ?load|black ?powder|trigger lock|\block\b|\bvise\b|vise block|"
	r"crown(ing)?|\bjag\b|flask|\bpatch(es)?\b|bore ?(guide|snake|brush)", re.I)
# A suppressor itself (NFA, not a part) — a flash suppressor and a suppressor's
# mount / adapter / piston are muzzle devices; "SUP HUB MNT" is a can with a HUB mount.
_SUPPRESSOR = re.compile(r"(?<!flash )(?<!flash)\b(suppressors?|silencers?)\b"
	r"(?![\s-]*(mount|mnt|adapter|adaptor|piston|booster|ready|compatible|height|cover|wrap))|\bsup\b.*\bhub\b|cal sup\b", re.I)
# An optic's own words: a Holosun "507 Comp" is a sight, not a compensator.
_OPTIC_TITLE = re.compile(r"\bsights?\b|red ?dot|reflex|holographic|\bmoa\b|co-?witness", re.I)


def parts_group(title: str | None, default: str | None = None) -> str | None:
	"""The Parts leaf a gun part's title names — Uppers, Handguards & Rails,
	Barrels, Bolt Carrier Groups & Charging Handles, Lower Parts & Kits, Triggers,
	Muzzle Devices, Stocks & Grips — else ``default``; None for a title that is no
	gun part (a barrel cover, a trigger lock, a muzzleloader's jag). In an uppers
	department (``default`` "Uppers") a title naming a caliber is the complete
	upper, whatever rail it also lists."""
	t = title or ""
	if _NOT_A_PART.search(t) or _SUPPRESSOR.search(t):
		return None
	if _UPPER.search(t):
		return "Uppers"
	if default == "Uppers" and _CALIBER.search(t) and not (_BARREL.search(t) or _BCG.search(t)):
		# A complete upper lists its handguard too ("… 16.10\" … 15\" Handguard").
		return "Uppers"
	if _HANDGUARD_WORD.search(t):
		return "Handguards & Rails"
	if _GAS.search(t):
		return default
	if _BARREL.search(t):
		return "Barrels"
	if _BCG.search(t):
		return "Bolt Carrier Groups & Charging Handles"
	if _BRACE.search(t):
		# An arm brace names the receiver extension it fits; it is stock furniture.
		return "Stocks & Grips"
	if _LPK_KIT.search(t):
		return "Lower Parts & Kits"
	if _TRIGGER.search(t):
		return "Triggers"
	if _LOWER_PARTS.search(t):
		return "Lower Parts & Kits"
	if _MUZZLE.search(t) and not _OPTIC_TITLE.search(t):
		return "Muzzle Devices"
	if _HANDGUARD.search(t):
		return "Handguards & Rails"
	if _STOCK_GRIP.search(t):
		return "Stocks & Grips"
	return default


def refine_parts_group(default: str | None, title: str | None) -> str | None:
	"""A parts department's row filed by its title (``parts_group``), else under
	``default``; None when the title is no gun part, or when the leaf is missing on
	this site (before migrate) — the caller keeps its own group then. Pure but for
	the Item Group existence check."""
	target = parts_group(title, default)
	if target and not frappe.db.exists("Item Group", target):
		return None
	return target


def _mount_first(t: str) -> bool:
	mount, optic = _MOUNT_WORD.search(t), _OPTIC_WORD.search(t)
	return bool(mount) and (not optic or mount.start() < optic.start())


def refine_accessory_group(group: str | None, title: str | None) -> str | None:
	"""A broad accessory group narrowed by the product title: a magazine →
	Magazine; a knife → Knife; a handguard, rail or
	M-LOK part → Handguards & Rails; a stock or grip → Stocks & Grips; a sling or
	swivel → Slings & Mounts; an iron / night sight filed under optics → Sights;
	an optic mount, ring, riser or adapter plate → Optic Mounts.
	Only broad groups move (Misc Accessory, Accessory, Stocks & Grips for its
	handguards, Optic for iron sights); anything else, or a group the site does not
	have, stays as it is. Pure but for the Item Group existence check."""
	t = title or ""
	target = None
	if group == "Optic":
		if _IRON_SIGHT.search(t) and not _NOT_IRON.search(t):
			target = "Sights"
		elif _mount_first(t):
			target = "Optic Mounts"
	elif group in ("Misc Accessory", "Accessory", "Stocks & Grips"):
		broad = group != "Stocks & Grips"
		if broad and _MAGAZINE.search(t) and not _HANDGUARD.search(t):
			target = "Magazine"
		elif broad and _KNIFE.search(t):
			target = "Knife"
		elif broad and _OPTIC_MOUNT.search(t) and not _SLING.search(t) and not _NOT_OPTIC_MOUNT.search(t):
			target = "Optic Mounts"
		elif broad and _SLING.search(t):
			# Before M-LOK: an M-LOK sling swivel is a sling part, not a rail.
			target = "Slings & Mounts"
		elif _HANDGUARD.search(t):
			target = "Handguards & Rails"
		elif broad and _STOCK_GRIP.search(t):
			target = "Stocks & Grips"
		elif broad and _IRON_SIGHT.search(t) and not _NOT_IRON.search(t):
			target = "Sights"
	if target and frappe.db.exists("Item Group", target):
		return target
	return group


# Ammunition by what it is for, from the title (shared with the RSR caliber reader,
# which runs the same patterns over a firearm's name). Permissive on whitespace
# ("9MM", "9 MM", "9 LUGER").
AMMO_RE_SHOTGUN = re.compile(
	# 12GA / 20GA / 28GA / 410GA / 12 GA / "GAUGE" / shotshell / buckshot /
	# birdshot. Bare "410" also covers .410 bore which RSR writes without GA;
	# safe within dept-18 (ammo-only) context where stray model numbers don't
	# appear in descriptions.
	r"\b(?:\d+\s*GA|GAUGE|SHOTSHELL|SHOT\s*SHELL|BUCKSHOT|BIRDSHOT|SLUG|410)\b",
	re.I,
)
AMMO_RE_RIMFIRE = re.compile(
	# .22 LR / .22 Short / .22 WMR / .17 HMR / .17 HM2 / 5mm Rem / "RIMFIRE"
	r"\b(?:RIMFIRE|22\s*LR|22\s*SHORT|22\s*WMR|17\s*HMR|17\s*HM2|5\s*MM\s*REM)\b",
	re.I,
)
# Pistol calibers — small, well-defined set. Permissive whitespace.
AMMO_RE_PISTOL = re.compile(
	r"\b("
	r"9\s*MM(?!\s*REM)|9\s*LUGER|9\s*X\s*19|"
	r"10\s*MM|"
	r"32\s*ACP|32\s*H&R|"
	r"25\s*ACP|25\s*NAA|"
	r"380\s*(?:ACP|AUTO)|380(?!\s*WBY)|"
	r"38\s*(?:SPL|SPECIAL|S&W|SUPER)|"
	r"357\s*(?:MAG|SIG)|"
	r"40\s*(?:S&W|SW)|"
	r"44\s*(?:MAG|SPL|SPECIAL|RUSS)|"
	r"45\s*(?:ACP|AUTO|GAP|LC|COLT|LONG\s*COLT)|"
	r"454\s*CAS|"
	r"460\s*S&W|"
	r"500\s*S&W|"
	r"50\s*AE|"
	r"PISTOL|HANDGUN"
	r")\b",
	re.I,
)
# Rifle calibers — broader catch-all, includes common centerfire rifle rounds.
AMMO_RE_RIFLE = re.compile(
	r"\b("
	r"5\.?56(?:\s*NATO|\s*X\s*45)?|556\s*NATO|"
	r"7\.?62(?:\s*X\s*(?:39|51|54)|\s*NATO)?|"
	r"\.?223(?:\s*REM)?|"
	r"\.?224\s*VAL|224\s*VALK|"
	r"\.?22[- ]?250|"
	r"\.?243\s*WIN|"
	r"\.?257\s*(?:WBY|ROB)|"
	r"\.?260\s*REM|"
	r"\.?264\s*WIN|"
	r"\.?270\s*(?:WIN|WSM|WBY)?|"
	r"\.?280\s*REM|"
	r"\.?284\s*WIN|"
	r"\.?300\s*(?:WIN|WBY|WSM|RUM|BLK|BLACKOUT|SAV|HAM|H&H|NORMA|PRC)?|"
	r"\.?303\s*BR(?:ITISH)?|"
	r"\.?308\s*WIN|"
	r"\.?30[- ]?06|"
	r"\.?30\s*CARBINE|"
	r"\.?300\s*(?:WIN|BLACKOUT|BLK|HAM|RUM)|"
	r"\.?338\s*(?:LAPUA|WIN|FED|EDGE|RUM|ARC)|"
	r"\.?348\s*WIN|"
	r"\.?350\s*LEG(?:END)?|"
	r"\.?358\s*WIN|"
	r"\.?375\s*(?:H&H|RUM|RUGER|WBY|CHEY)|"
	r"\.?416\s*(?:REM|RIGBY|WBY|RUGER|BARRETT)|"
	r"\.?444\s*MAR|"
	r"\.?450\s*(?:BUSH|MARLIN|BM)|"
	r"\.?458\s*(?:SOCOM|LOTT|WIN)|"
	r"\.?6\.?5\s*(?:CREED|GRENDEL|PRC|SWE|JAP|MAN|REM|MAG)|"
	r"\.?6\.?8\s*(?:SPC|REM|WSTN|WESTERN|MM)|"
	r"\.?25\s*(?:CREED|REM)|"
	r"\.?22\s*(?:HORN|ARC|NOSL)|"
	r"\.?17\s*(?:WSM|REM|FB|MACH)|"
	r"\.?204\s*RUGER|"
	r"\.?28\s*NOSL|"
	r"\.?6\.?8\s*REM\s*SPC|"
	r"\.?9\.?3\s*X\s*\d+|"
	r"7\s*MM(?:\s*-?\s*\d+)?(?:\s*WBY|\s*REM|\s*MAUSER|\s*MAG|\s*PRC|\s*VAL|\s*-08|\s*WSM)?|"
	r"6\s*MM(?:\s*REM|\s*ARC|\s*BR)?|"
	r"8\s*MM(?:\s*MAUSER|\s*REM)?|"
	r"\bRIFLE\b"
	r")\b",
	re.I,
)


def ammo_group(text: str | None) -> str | None:
	"""The ammunition leaf a title names — Shotgun / Rimfire / Rifle / Pistol Ammo —
	or None. Order matters: gauges and rimfire first, rifle before pistol."""
	if not text:
		return None
	for rgx, leaf in ((AMMO_RE_SHOTGUN, "Shotgun Ammo"), (AMMO_RE_RIMFIRE, "Rimfire Ammo"),
			(AMMO_RE_RIFLE, "Rifle Ammo"), (AMMO_RE_PISTOL, "Pistol Ammo")):
		if rgx.search(text):
			return leaf
	return None


# A suppressor itself, by its title (the fallback's NFA leaf).
SUPPRESSOR_TITLE = _SUPPRESSOR
