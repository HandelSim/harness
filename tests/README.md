# tests/

Tests for the harness project. Quick navigation:

- **Architecture and conventions** — `architecture/tests.md` (layout,
  sections, conventions, mock upstream, CI matrix, how to add a new
  test).
- **Inventory** — `INVENTORY.md`, this directory. Flat list of every
  testable feature/behavior with stable IDs (`F###`, `P###`, `A###`,
  ...). Source of truth that coverage maps against.
- **Coverage** — `COVERAGE.md`, this directory. Map of every inventory
  ID to its test(s), with status and quoted assertion evidence.
- **Benchmarks** — `benchmarks/README.md`.
- **Fixtures** — `fixtures/responses/README.md`.

## Quick start

```
harness test                       # all CI-runnable tests
harness test proxy                 # only proxy tests
harness test --pattern 'mcp*'      # explicit glob
harness test integration --slow    # slow integration test (HARNESS_RUN_SLOW=1)
```

See `architecture/tests.md` for everything else.
