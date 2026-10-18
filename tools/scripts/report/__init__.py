"""scripts/report package marker.

Presentation outputs. cross_section.py is the bundle's only OPTIONAL
DEPENDENCY tier (svglib + reportlab + Pillow); those imports live inside the
one function that needs them, so importing this package stays stdlib-only.
"""
