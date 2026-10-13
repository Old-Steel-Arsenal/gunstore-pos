"""Photo manager — the one write path for ``image_gallery`` rows on Item and Serial No.

The Desk form component (``window.gunstore.renderPhotoManager``) and the
"Upload photos by serial" page both talk to this module and nothing else. The
child table stays the storage and the publishing order stays
``ffl_core.images.ordered_gallery_paths`` (primary first, then row order), so
WooCommerce / GunBroker publish exactly what the operator arranged here.

Every write goes through ``doc.save()`` — never ``db.set_value`` — so the
gallery/featured-image reconciliation in ``item_hooks.sync_item_image_gallery``
(registered on BOTH doctypes) keeps "exactly one primary, ``image`` mirrors it"
true whichever endpoint ran. Files are made public on the way in through
``ffl_core.utils.set_file_privacy`` — product photos must be fetchable by the web
store, and that helper is the single home for visibility flips.
"""
from __future__ import annotations

import json
import os

import frappe
from frappe import _

from ffl_core.utils import set_file_privacy

ALLOWED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
MAX_FILES_PER_CALL = 50
THUMBS_PER_SERIAL = 4


def _check_perm(doctype: str, name: str, ptype: str = "write") -> None:
	"""Gate: ``ptype`` on this exact document, and only the two doctypes that
	carry a gallery are accepted at all."""
	if doctype == "Item":
		frappe.has_permission("Item", ptype, doc=name, throw=True)
	elif doctype == "Serial No":
		frappe.has_permission("Serial No", ptype, doc=name, throw=True)
	else:
		frappe.throw(
			_("Photos are managed on Item and Serial No only, not {0}.").format(doctype),
			frappe.ValidationError,
		)


def _as_list(value) -> list:
	if isinstance(value, str):
		value = value.strip()
		if value.startswith("["):
			return json.loads(value)
		return [value] if value else []
	return list(value or [])


def _row(doc, row_name: str):
	for r in doc.get("image_gallery") or []:
		if r.name == row_name:
			return r
	frappe.throw(
		_("Photo row {0} is not on {1} {2}.").format(row_name, doc.doctype, doc.name),
		frappe.DoesNotExistError,
	)


def _renumber(doc) -> None:
	"""Primary first, then the operator's order; ``idx`` and ``sort_order`` agree.

	``ordered_gallery_paths`` publishes by row order and the form strip sorts by
	``sort_order`` — keeping the two identical is what makes a drag on the screen
	the order the shopper sees."""
	rows = sorted(
		list(doc.get("image_gallery") or []),
		key=lambda r: (0 if r.is_primary else 1, r.idx or 0),
	)
	doc.set("image_gallery", [])
	for i, r in enumerate(rows, start=1):
		r.idx = i
		r.sort_order = i
		doc.append("image_gallery", r)


def gallery(doc) -> list[dict]:
	"""The rows in publish order, shaped for the client."""
	rows = sorted(
		doc.get("image_gallery") or [],
		key=lambda r: (0 if r.is_primary else 1, r.idx or 0),
	)
	return [
		{
			"name": r.name,
			"image": r.image,
			"is_primary": 1 if r.is_primary else 0,
			"sort_order": r.sort_order,
			"caption": r.caption or "",
		}
		for r in rows
	]


def _validated_public_url(file_url: str) -> str:
	url = (file_url or "").strip()
	ext = os.path.splitext(url.split("?", 1)[0])[1].lower()
	if ext not in ALLOWED_EXTENSIONS:
		frappe.throw(
			_("{0} is not a photo (allowed: {1}).").format(url, ", ".join(ALLOWED_EXTENSIONS)),
			frappe.ValidationError,
		)
	if not frappe.db.exists("File", {"file_url": url}):
		frappe.throw(_("{0} is not a file on this site.").format(url), frappe.ValidationError)
	return set_file_privacy(url, private=False) or url


def _url_still_referenced(url: str) -> bool:
	return bool(
		frappe.db.count("Item Image Gallery", {"image": url})
		or frappe.db.exists("Item", {"image": url})
		or frappe.db.exists("Serial No", {"image": url})
	)


@frappe.whitelist()
def add_gallery_images(doctype: str, name: str, file_urls) -> list[dict]:
	"""Append already-uploaded photos to a document's gallery in ONE save.

	Args:
	    doctype (str): ``Item`` or ``Serial No``.
	    name (str): the document.
	    file_urls (list[str] | str): ``/files/...`` URLs of Files already on this
	        site (the Frappe uploader creates them); a JSON string is accepted.

	Returns:
	    list[dict]: the gallery in publish order — ``name``, ``image``,
	    ``is_primary``, ``sort_order``, ``caption`` per row.

	Notes:
	    Files are made public. A URL already in the gallery is skipped. The
	    first photo of an empty gallery becomes primary (validate hook); an
	    existing featured photo is never displaced by an upload.
	"""
	_check_perm(doctype, name)
	urls = _as_list(file_urls)
	if not urls:
		frappe.throw(_("No photos given."), frappe.ValidationError)
	if len(urls) > MAX_FILES_PER_CALL:
		frappe.throw(
			_("At most {0} photos per upload.").format(MAX_FILES_PER_CALL), frappe.ValidationError
		)
	doc = frappe.get_doc(doctype, name)
	have = {(r.image or "").strip() for r in doc.get("image_gallery") or []}
	for u in urls:
		public = _validated_public_url(u)
		if public in have:
			continue
		have.add(public)
		doc.append("image_gallery", {"image": public, "is_primary": 0})
	_renumber(doc)
	doc.save()
	return gallery(doc)


@frappe.whitelist()
def reorder_gallery(doctype: str, name: str, row_names) -> list[dict]:
	"""Persist the order the operator dragged the photos into.

	Args:
	    doctype (str): ``Item`` or ``Serial No``.
	    name (str): the document.
	    row_names (list[str] | str): every gallery row name, in the new order;
	        JSON string accepted. Must be exactly the current set of rows.

	Returns:
	    list[dict]: the gallery in publish order (primary always first).
	"""
	_check_perm(doctype, name)
	order = _as_list(row_names)
	doc = frappe.get_doc(doctype, name)
	rows = {r.name: r for r in doc.get("image_gallery") or []}
	if set(order) != set(rows) or len(order) != len(rows):
		frappe.throw(_("The order must list every photo exactly once."), frappe.ValidationError)
	for i, rn in enumerate(order, start=1):
		rows[rn].idx = i
	_renumber(doc)
	doc.save()
	return gallery(doc)


@frappe.whitelist()
def set_primary_image(doctype: str, name: str, row_name: str) -> list[dict]:
	"""Make one gallery photo the featured image.

	Args:
	    doctype (str): ``Item`` or ``Serial No``.
	    name (str): the document.
	    row_name (str): the gallery row to feature.

	Returns:
	    list[dict]: the gallery in publish order; exactly one row is primary and
	    the document's ``image`` mirrors it.
	"""
	_check_perm(doctype, name)
	doc = frappe.get_doc(doctype, name)
	target = _row(doc, row_name)
	for r in doc.get("image_gallery") or []:
		r.is_primary = 1 if r is target else 0
	_renumber(doc)
	doc.save()
	return gallery(doc)


@frappe.whitelist()
def set_image_caption(doctype: str, name: str, row_name: str, caption: str = "") -> list[dict]:
	"""Set a photo's caption (also used as its alt text).

	Args:
	    doctype (str): ``Item`` or ``Serial No``.
	    name (str): the document.
	    row_name (str): the gallery row.
	    caption (str): the new caption; blank clears it.

	Returns:
	    list[dict]: the gallery in publish order.
	"""
	_check_perm(doctype, name)
	doc = frappe.get_doc(doctype, name)
	_row(doc, row_name).caption = (caption or "").strip()[:140]
	doc.save()
	return gallery(doc)


@frappe.whitelist()
def remove_gallery_image(doctype: str, name: str, row_name: str) -> list[dict]:
	"""Remove a photo from the gallery; delete the file only when nothing else uses it.

	Args:
	    doctype (str): ``Item`` or ``Serial No``.
	    name (str): the document.
	    row_name (str): the gallery row to remove.

	Returns:
	    list[dict]: the remaining gallery in publish order.

	Notes:
	    The same photo may sit on an Item AND on one of its serials; the File is
	    deleted only when no gallery row and no ``image`` field anywhere still
	    points at it. Removing the last photo clears the document's ``image``.
	"""
	_check_perm(doctype, name)
	doc = frappe.get_doc(doctype, name)
	target = _row(doc, row_name)
	url = (target.image or "").strip()
	doc.remove(target)
	if (doc.get("image") or "").strip() == url or not doc.get("image_gallery"):
		# The validate hook re-absorbs any `image` missing from the gallery, so a
		# stale mirror would resurrect the row just removed. Clear it; the hook
		# then promotes the first remaining row (or leaves it empty).
		doc.image = None
	_renumber(doc)
	doc.save()
	if url and not _url_still_referenced(url):
		for file_name in frappe.get_all("File", filters={"file_url": url}, pluck="name"):
			frappe.delete_doc("File", file_name, ignore_permissions=True)
	return gallery(doc)


@frappe.whitelist()
def serials_for_item(item_code: str, in_stock_only: int = 1, search: str = "") -> list[dict]:
	"""The serials of one Item with their photo counts, for the upload-by-serial page.

	Args:
	    item_code (str): the Item.
	    in_stock_only (int): 1 = only ``status = Active`` serials (default), 0 = all.
	    search (str): optional substring filter on the serial number.

	Returns:
	    list[dict]: per serial — ``serial_no``, ``status``, ``warehouse``,
	    ``image``, ``photo_count``, ``thumbs`` (first few photo URLs in publish
	    order). Newest serials first, capped at 500.
	"""
	frappe.has_permission("Serial No", "read", throw=True)
	filters = {"item_code": item_code}
	if frappe.utils.cint(in_stock_only):
		filters["status"] = "Active"
	if (search or "").strip():
		filters["name"] = ("like", f"%{search.strip()}%")
	serials = frappe.get_all(
		"Serial No",
		filters=filters,
		fields=["name", "status", "warehouse", "image"],
		order_by="creation desc",
		limit_page_length=500,
	)
	if not serials:
		return []
	rows = frappe.get_all(
		"Item Image Gallery",
		filters={"parenttype": "Serial No", "parent": ("in", [s.name for s in serials])},
		fields=["parent", "image", "is_primary", "idx"],
		order_by="parent, is_primary desc, idx",
	)
	by_parent: dict[str, list[str]] = {}
	for r in rows:
		if (r.image or "").strip():
			by_parent.setdefault(r.parent, []).append(r.image)
	return [
		{
			"serial_no": s.name,
			"status": s.status,
			"warehouse": s.warehouse,
			"image": s.image,
			"photo_count": len(by_parent.get(s.name, [])),
			"thumbs": by_parent.get(s.name, [])[:THUMBS_PER_SERIAL],
		}
		for s in serials
	]
