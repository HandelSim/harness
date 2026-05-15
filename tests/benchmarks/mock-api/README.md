# Mock upstream API

A tiny Flask app that speaks the OpenAI chat-completions shape so the
harness proxy can be exercised end-to-end without a real upstream key.

This is for **wiring tests only** — it has no real LLM behavior. Use it
when you want to prove the benchmark plumbing works (proxy → mock,
harness → proxy → mock, Harbor → harness → proxy → mock) without burning
budget or needing API access.

## Run it standalone

```bash
docker build -t harness-mock-api:latest tests/benchmarks/mock-api
docker run --rm -p 8080:80 \
    -e MOCK_LOG_REQUESTS=1 \
    harness-mock-api:latest

curl -sS -X POST http://127.0.0.1:8080/v1/chat/completions \
    -H 'content-type: application/json' \
    -d '{"model":"x","messages":[{"role":"user","content":"hi"}]}' \
    | python3 -m json.tool
```

## Wire into the harness compose stack

From the repo root:

```bash
docker compose --env-file ./.env \
    -f docker-compose.yml \
    -f tests/benchmarks/mock-api/docker-compose.mock.yml \
    up -d

# .env must contain:
#   PROXY_API_URL=http://mock-api:80/v1/chat/completions
#   PROXY_API_KEY=mock-key
#   PROXY_API_MODEL=mock-model
```

The firewall script in `firewall/init-firewall.sh` treats bare hostnames
(no dots) as intra-cluster and skips the allowlist guard. So
`mock-api` Just Works — no allowlist edit needed.

## Scripted responses

For tests that need a specific reply on a specific input, point the
mock at a JSON script:

```bash
cat > tests/benchmarks/mock-api/script.json <<'JSON'
[
  {"match": "create.*hello\\.txt",
   "content": "```json\n{\"name\":\"bash\",\"arguments\":{\"command\":\"echo hello > /tmp/hello.txt\"}}\n```"},
  {"match": ".*",
   "content": "Done."}
]
JSON

# In .env:
MOCK_SCRIPT_FILE=/app/script.json
```

First regex-match against the last user message wins; default
`MOCK_DEFAULT_CONTENT` is returned on no match.
