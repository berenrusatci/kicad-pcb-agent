"""scripts/sim package marker.

External-binary tier: ngspice. The binary is resolved lazily and its absence
is reported as NO_COVERAGE, never as a pass - so importing this package and
running the rest of the suite never requires a simulator.
"""
