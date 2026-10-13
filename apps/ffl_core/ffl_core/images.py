"""Gallery ordering — the one rule for "which photo comes first".

Every sales channel that publishes a gun's photos has to answer the same
question in the same way: the row flagged ``is_primary`` is the featured image
and must lead, everything else keeps the order the operator arranged it in.
WooCommerce promotes ``images[0]`` to the product's featured image; GunBroker
promotes the first ``picture`` multipart part to ``thumbnailURL`` (verified
against the sandbox). Two channels, one rule — so it lives here rather than
being re-derived per channel, where the two would eventually disagree about
which photo the shopper sees first.

Deliberately PURE and URL-agnostic: it returns the stored paths in order and
nothing else. Resolving a path to something a remote system can consume is
per-channel work with per-channel failure modes — WooCommerce needs an
absolute, publicly reachable URL (and an RSR high-res upgrade probe);
GunBroker needs the raw bytes uploaded as a multipart part. Folding either
into this function would drag an HTTP probe into the other channel's payload
build. Callers layer their own resolution over the result:

    # ffl_woo_sync
    [_to_highres(_resolve_image(p)) for p in ordered_gallery_paths(rows, img)]

    # ffl_integrations.gunbroker
    [_file_bytes(p) for p in ordered_gallery_paths(rows, img)]
"""
from __future__ import annotations


def _row_get(row, key: str):
	"""Read ``key`` off an image-gallery row that may be a Document, a
	``frappe._dict`` or a plain ``dict``.

	The child rows arrive as Documents from ``doc.image_gallery`` and as plain
	dicts from ``frappe.db.get_all`` / test fixtures. ``getattr`` alone silently
	answers None for a plain dict — which reads as "no image" and drops the photo
	rather than failing, so it must be handled explicitly, not assumed away.
	"""
	if isinstance(row, dict):
		return row.get(key)
	return getattr(row, key, None)


def ordered_gallery_paths(gallery_rows, fallback_image: str | None = None) -> list[str]:
	"""Stored image paths for one gallery, primary first.

	Args:
	    gallery_rows: the ``image_gallery`` child rows (Documents, ``_dict`` or
	        plain dicts), or None.
	    fallback_image: the single-image field to use when the gallery yields
	        nothing (``Serial No.image`` / ``Item.image``).

	Returns:
	    The paths in publish order — every row flagged ``is_primary`` first (in
	    their own relative order), then the rest in their original order. Blank
	    paths are dropped. When the gallery contributes nothing and
	    ``fallback_image`` is set, the result is just that one path; when neither
	    yields anything the result is empty (callers decide whether an image-less
	    listing is allowed — WooCommerce permits one, GunBroker's API permits one
	    too, and it is our own product rule that refuses it).
	"""
	primary_paths: list[str] = []
	other_paths: list[str] = []
	for row in (gallery_rows or []):
		img = _row_get(row, "image")
		if img and str(img).strip():
			if bool(_row_get(row, "is_primary")):
				primary_paths.append(str(img))
			else:
				other_paths.append(str(img))
	paths = primary_paths + other_paths
	if not paths and (fallback_image or "").strip():
		paths.append(str(fallback_image))
	return paths
