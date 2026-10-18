"""scripts/lab package marker.

PHYSICAL-EVIDENCE TIER. Every module here INGESTS a record that a bench, an
instrument or an accredited laboratory already produced, and decides whether
that record is admissible as evidence for a named rule on a named board unit.

No module in this package talks to an instrument, drives a fixture, or takes a
measurement of its own. That is not a missing feature - a number this repo
generated itself would be a SECOND answer to a question the bench had already
answered, and once both were in the same report nothing downstream could tell
them apart. The instrument's file is the evidence; these modules are the
custody chain around it.

Modules here are also runnable as standalone scripts; each inserts the tools/
directory on sys.path so `from common...` works either way.
"""
