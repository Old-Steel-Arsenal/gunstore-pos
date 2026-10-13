"""Catalog Category — the POS's mirror of one node of the catalog master's category
tree (the Data Service's ``/v1/catalog/categories``), and the node's links: the Item Group it
files as and its product-category term on each store. Written only by the
category mirror (``ffl_integrations.catalog_master`` and, for the store terms,
``ffl_woo_sync.woocommerce.categories``); read through ``ffl_core.catalog_tree``.
Edit names and places in the catalog workbench, never here."""
from frappe.model.document import Document


class CatalogCategory(Document):
	pass
