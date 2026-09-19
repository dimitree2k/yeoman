"""Private contacts adapter of the knowledge module.

Not a public import path: consumers use :mod:`yeoman_gateway.knowledge.api`.  The store
joins the knowledge store's single connection and never commits on its own.
"""

from yeoman_gateway.knowledge._contacts.service import ContactsService

__all__ = ["ContactsService"]
