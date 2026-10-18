"""Shared primitives for the pcb-agent tool bundle.

Every helper in here was previously duplicated across two or more scripts.
Import from the submodules, not from this package root, so that a script that
needs only stdlib maths never drags in ``pcbnew`` or ``kicad-cli``.
"""
