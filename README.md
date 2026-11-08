# Local AI Chat UI (Python)

A self-hosted, single-file Python web chat client for **any OpenAI-compatible API**
(OpenAI, ProxyAPI, OpenRouter, LM Studio, Ollama, llama.cpp server, vLLM, ...).

![stack](https://img.shields.io/badge/python-3.9%2B-blue) — backend: FastAPI + httpx · frontend: vanilla JS, no build step

## Features

- ChatGPT-like interface: sidebar with conversation **history**, markdown rendering,
  code blocks with copy buttons, collapsible **reasoning / thinking** display
  (works with models that return `reasoning` / `reasoning_content` fields).
- **Token usage tracking**: per-message chips (`↑ prompt · ↓ completion · thought N · time`)
  and per-conversation / all-time totals. Actual numbers are used when the API
  reports `usage`; otherwise a local heuristic estimate is shown, marked with `≈`.
- **Streaming (SSE)** by default, with automatic fallback:
  - if a provider rejects `stream_options`, the request is retried once without it;
  - if the provider does not return usage in the final chunk, tokens are estimated.
- Everything is stored locally in `data/` (SQLite for chats, `settings.json` for config).
- Serves on **0.0.0.0:3080** — reachable from any device on your local network.

## Quick start

```bash
cd ai_webui
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
python app.py
```

Then open:

- this machine: `http://127.0.0.1:3080`
- other LAN devices: `http://<your-machine-ip>:3080` (the IP is printed on startup)

On first launch click **Settings** and fill in:

| Setting | Example |
|---|---|
| API Base URL | `https://api.proxyapi.ru/v1` (a bare host like `https://api.openai.com` also works — `/v1` is added automatically) |
| API Key | `sk-...` |
| Model | `openai/gpt-oss-20b` — press *Test connection & load models* to fetch the list from `/v1/models` |

### CLI options

```
python app.py --host 0.0.0.0 --port 3080 --data-dir /path/to/data
```

| Flag | Default | Description |
|---|---|---|
| `--host` | `0.0.0.0` | Bind address. Use `127.0.0.1` to keep the UI private to this machine. |
| `--port` | `3080` | Port to listen on. |
| `--data-dir` | `./data` | Where `chat.db` and `settings.json` are stored. |


## Context window — limiting what gets sent to the API

Long conversations eventually exceed what a model can accept. Two settings in
**Settings → Chat** control how much of the history is included in each request:

| Setting | What it does |
|---|---|
| **History messages sent** | Hard cap on the *number* of most recent messages in each request (default 50). |
| **Max context window (tokens)** | Cap on the estimated *size* of the request in tokens (`0` = unlimited). When the budget is exceeded, the **oldest** messages are dropped first until it fits. |

How the token limit works:

- The prompt size is estimated locally (~4 characters per token, ~1 token per CJK
  character, plus a small per-message overhead) — no tokenizer dependency. Actual billed
  usage is still shown per message from the API's `usage` field when available.
- The **system prompt and the newest message are always sent**, even if they alone exceed
  the limit — pick a value that fits your model (e.g. `openai/gpt-oss-20b` accepts ~131k
  tokens, so `16000` is a comfortable, cheap default).
- Trimmed messages **stay in your saved history** — only the outgoing request is reduced.
  Whenever older messages were left out, a notice appears above that reply in the chat.
- Both limits apply together (whichever trims more wins).

## Token usage — how it is counted

- **Per assistant message**: `↑ prompt` (what the API billed for the whole request,
  i.e. it includes the resent history), `↓ completion`, `thought` (reasoning tokens
  when reported), response time, and the model name.
- **Per user message**: an `≈` estimate of the input size.
- **Header chip**: cumulative totals for the open conversation; hover for details.
- **Token usage button** (sidebar): all-time totals, breakdown per model, and a
  14-day mini chart.
- When the API does not report usage (e.g. some providers in streaming mode), the
  client estimates tokens locally (`~4 chars per token, ~1 token per CJK char`) and
  marks the values with `≈`.

## Security notes

- The UI has **no authentication** — it is designed for a trusted home/office LAN.
  Do not port-forward it to the internet. Use `--host 127.0.0.1` if you want it
  accessible only from the local machine.
- Your API key is stored in plain text in `data/settings.json` on this machine.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Address already in use` on port 3080 | `python app.py --port 3081` |
| Cannot open from another device | Check the firewall allows inbound TCP 3080; use the network IP printed at startup |
| `HTTP 401` in chat | Wrong API key in Settings |
| `stream_options` errors | Handled automatically; or uncheck *Stream responses* in Settings |
| Self-signed cert / SSL error behind a proxy | Uncheck *Verify TLS certificates* |

## Project layout

```
ai_webui/
├── app.py              # FastAPI backend (API proxying, history, usage, settings)
├── requirements.txt
├── static/
│   ├── index.html      # single-page UI
│   ├── styles.css
│   └── app.js
└── data/               # created at runtime: chat.db, settings.json
```

Works offline against local runtimes too — e.g. LM Studio
(`http://127.0.0.1:1234/v1`) or Ollama (`http://127.0.0.1:11434/v1`), no API key
needed (leave the field empty).
