# mock-upstream response fixtures

Each `*.json` file in this directory is one fixture for `scripts/mock_upstream.py`.
At startup the mock loads them lexicographically and, on every request,
matches the **most recent user-message content** against each fixture's
`match` regex (case-insensitive, multiline). The first hit wins.

Mount this directory into the mock container at `/fixtures` and set
`MOCK_FIXTURES_DIR=/fixtures` to enable the dispatch path. Without that env
var, the mock falls back to the legacy `MOCK_SCENARIO={text,tool}` env (so
tests written before fixtures existed keep working unchanged).

## File shape

```json
{
  "name": "human-readable label, shown in startup log",
  "match": "regex against the latest user message",
  "response": { /* full OpenAI chat-completion body */ }
}
```

A fixture with `"match": ""` (or `match` missing) is the **catch-all**.
Name it so it sorts last (e.g. `99_default.json`); first-match-wins means
anything earlier wins over it.

## Filename convention

`NN_short_slug.json`, where `NN` controls priority. Lower numbers win.
Reserved ranges:

- `01–09` — simple/text scenarios.
- `10–19` — file edit / write scenarios.
- `20–29` — bash / shell scenarios.
- `30–49` — Serena MCP scenarios.
- `50–69` — Graphify MCP scenarios.
- `70–98` — additional MCPs.
- `99` — catch-all default. Don't add fixtures with `99_` prefix unless
  you intend to override the default.

## Shipped fixtures

| File                          | Matches                                       | Response style              |
| ----------------------------- | --------------------------------------------- | --------------------------- |
| `01_simple_text.json`         | `say hello`, `hi`, `hello`                    | plain text                  |
| `02_create_file.json`         | `create file`, `write a file`                 | tool call: Write            |
| `03_list_files.json`          | `list files`, `what's in this dir`            | tool call: Bash `ls -la`    |
| `04_serena_find_symbol.json`  | `find symbol`, `find function`, `serena find` | tool call: Serena MCP       |
| `05_serena_read_symbol.json`  | `read symbol`, `show me the body`             | tool call: Serena MCP       |
| `06_graphify_extract.json`    | `graphify`, `extract from url`                | tool call: Graphify MCP     |
| `99_default.json`             | catch-all                                     | plain text (same as `01_`)  |

## Adding a new fixture

1. Pick a number in the right range (see table above).
2. Drop a `NN_slug.json` here. The mock picks it up at restart.
3. Verify the regex with `python3 -c "import re,json; ..."` if it's
   non-trivial — a typo silently makes it never match and you'd debug for
   hours.
4. The response body must be a valid OpenAI chat completion (`choices` is
   required). To emit a tool call, set the assistant `content` to a
   markdown string containing a fenced ```` ```json ```` block whose body
   is `{"name": "...", "arguments": {...}}` — the proxy parses that into
   a structured `tool_calls` field.
