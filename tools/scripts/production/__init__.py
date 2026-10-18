"""scripts/production package marker.

External-binary tier: java + freerouting.jar, and KiBot. Every binary is
resolved lazily inside the function that needs it, the same way scripts/board
confines pcbnew, so a missing tool degrades to a reported NO_COVERAGE or an
explicit error instead of breaking test collection.
"""
