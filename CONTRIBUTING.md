# Contributing to yt-consolidado

Thanks for your interest in improving this MCP server! Below is the current
roadmap, built from real-world stress testing (2026-11). Pick an issue, open a
PR, and reference the issue number.

## Roadmap: known defects & improvements

### 🐛 1. SSL connection errors are not retried
Symptoms during stress testing: `WRONG_VERSION_NUMBER`,
`DECRYPTION_FAILED_OR_BAD_RECORD_MAC` raised repeatedly for identical queries,
while other queries succeeded.
- Likely cause: a pooled HTTP connection is reused after corruption.
- Fix: invalidate the pooled connection after any SSL error and retry with
  exponential backoff (max 3 attempts).

### 🐛 2. Heavy verification runs time out and still burn quota
A full run with 60–100 verified candidates exceeds client timeouts, yet the
server keeps verifying in the background — quota continues to be spent with
no output at the client.
- Fix: stream/emit channels as soon as each candidate is verified
  (progressive delivery) and make the run resumable via a checkpoint.

### 🐛 3. Concurrent calls can crash the server
Three parallel tool calls produced `Connection closed` / `Not connected` in
the MCP client. The server apparently shares state (API key pool) across
calls without a guard.
- Fix: a global semaphore/queue for API access; never instantiate per-call
  clients; serialize key rotation.

### ✨ 4. Discovery relies only on YouTube search (structural blind spot)
`find_emerging_channels` samples the top 50–100 search results. Channels that
are <30 days old with 100k+ long-form videos rarely surface there — the
informational layer already includes RSS (0 quota) and yt-dlp (quota-free).
- Improvement: add RSS/yt-dlp-based discovery sources (trending feeds,
  saturation-aware sampling of category feeds) as alternate discovery layers.

## How to run the tests

```bash
uv sync
uv run pytest tests/
```

## Style

- Keep tools returning structured JSON (no raw logs in tool payload).
- Never log or commit API keys; use `YOUTUBE_API_KEYS` env only.
