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
- `diff2html/`: diff2html 3.4.56 (MIT, `diff2html/LICENSE.md`), the diff
  renderer of a ticket's Changes page (`diff_view.py`). `diff2html.min.js`
  and `diff2html.min.css` are `bundles/js/diff2html.min.js` and
  `bundles/css/diff2html.min.css` from npm `diff2html-3.4.56.tgz`, sha256
  `0e0b417da107038f17d9d48436adaed3bf037c05501a231f7b3acec06124f65b`,
  unchanged. The page inlines both (its CSP allows no other sources); the
  bundle needs no eval, so it runs under the CSP's nonce.
