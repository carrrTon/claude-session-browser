# Project Instructions

## Project workflow
- Keep temporary tests, one-off verification scripts, and generated review artifacts under `.tmp/`; do not place them in the repository root.
- Use `.tmp/tests/` as the default location for ad-hoc or regression test files that are not intended to ship with the project.

## Verification
- Run Python regression tests from the project root with `python3 -m unittest discover -s .tmp/tests -p 'test_*.py'` when `.tmp/tests/` contains tests.
- Run `python3 -m py_compile app.py` after editing the Python app.
