# Vendored libraries

serve imports these from here (`sys.path`), so `harness serve` needs nothing
installed beyond Python 3.

- `mistune/`: mistune 3.3.4 (BSD-3-Clause, `mistune/LICENSE`), the pure-Python
  markdown renderer the Vault pages use. From PyPI
  `mistune-3.3.4-py3-none-any.whl`, sha256
  `ee015381e955e370962968befe1d729ab60fafb6a715ac6751763fbce38c8d4a`.
  3.3.x is the last line that runs on Python below 3.10. One local change:
  `core.py` falls back to `Self = Any` when `typing_extensions` is missing
  (it is only a type annotation).
