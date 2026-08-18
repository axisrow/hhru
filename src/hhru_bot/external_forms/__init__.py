"""Read-only/dry-run support for forms hosted outside hh.ru (#276)."""

from .detect import FormField, FormScan, scan_form

__all__ = ["FormField", "FormScan", "scan_form"]
