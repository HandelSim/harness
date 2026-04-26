# test-project

Tiny fixture for the harness integration test. A small Python "calculator"
project that exists solely so Serena (an MCP server that does semantic code
analysis) and Graphify (a static AST/graph extractor) have something
realistic to look at.

The project is intentionally:
- Small — every file under 80 lines.
- Stdlib-only — no external dependencies; runs anywhere with Python 3.
- Realistic — multiple classes, inheritance, cross-file imports, an
  exception hierarchy. Enough structure for symbol lookup and call-graph
  extraction to produce non-trivial output.

Run the (also tiny) test suite with:

    python -m pytest

Layout:

    src/calculator/
        core.py        Calculator (add/sub/mul/div)
        advanced.py    ScientificCalculator(Calculator) (sqrt/pow/log)
        parser.py      ExpressionParser (tokenize/parse)
        exceptions.py  CalculatorError + subclasses
    tests/
        test_core.py
        test_advanced.py
        test_parser.py
