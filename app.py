#!/usr/bin/env python3
"""
Local Chat UI for OpenAI-compatible APIs.

A single-user, local-network chat interface for any API implementing the
OpenAI /v1/chat/completions protocol (OpenAI, ProxyAPI, OpenRouter,
LM Studio, Ollama, llama.cpp server, vLLM, ...).

Features:
- ChatGPT-like UI, conversation history stored in SQLite
- Streaming (SSE) and non-streaming modes
- Per-message and cumulative token usage (actual numbers from the API when
  available, otherwise a local estimate, marked with "~")
- HTTP/HTTPS/SOCKS5 proxy support
- Reasoning ("thinking") display for models that return reasoning text
- Detection and user-visible handling of truncated / reasoning-only answers

Run:
    python app.py                -> serves on http://0.0.0.0:3080
    python app.py --port 8080    -> custom port
"""

from __future__ import annotations

import argparse
import json
import base64
import math
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "chat.db"
SETTINGS_PATH = DATA_DIR / "settings.json"

MAX_ATTACHMENTS = 5
MAX_IMAGE_BYTES = 5 * 1024 * 1024      # 5 MB per image
MAX_TEXT_BYTES = 512 * 1024            # 512 KB per txt file
MAX_REQUEST_BYTES = 35 * 1024 * 1024   # JSON limit

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".jsonl",
    ".xml", ".yaml", ".yml", ".toml", ".ini", ".log",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css",
    ".sql", ".sh", ".bat", ".ps1", ".go", ".rs", ".java",
    ".c", ".cpp", ".h", ".hpp", ".php", ".rb", ".swift",
}


DEFAULT_SETTINGS: dict[str, Any] = {
    "base_url": "https://api.proxyapi.ru/v1",
    "api_key": "",
    "model": "openai/gpt-oss-20b",
    "system_prompt": "",
    "temperature": 0.7,
    "stream": True,
    "history_limit": 50,
    "max_context_tokens": 0,       # 0 = unlimited; oldest history is trimmed beyond this budget
    "max_tokens": 16384,           # 0 = use provider default; set >0 to allow longer answers/thinking
    "reasoning_budget_tokens": 0,  # optional: Anthropic-like thinking budget_tokens
    "reasoning_reserve_tokens": 4096,
    "extra_params": "",            # optional JSON object merged into API request
    "timeout": 300,
    "theme": "light",
}


TRUNCATION_PARTIAL_NOTICE = (
    "\n\n---\n"
    "⚠ The response was truncated because the output token limit was reached. "
    "Increase Max output tokens or reduce reasoning/thinking budget."
)

TRUNCATION_EMPTY_LIMIT_WITH_REASONING_NOTICE = (
    "⚠ No final answer was generated: the model spent the whole output token limit on reasoning. "
    "Increase Max output tokens or reduce reasoning/thinking budget."
)

TRUNCATION_EMPTY_LIMIT_GENERIC_NOTICE = (
    "⚠ No final answer was generated: the output token limit was reached before the answer started. "
    "Increase Max output tokens."
)

REASONING_ONLY_NOTICE = (
    "⚠ The model returned only reasoning and no final answer. "
    "Increase Max output tokens, check Extra API params, or disable/limit thinking budget."
)

EMPTY_RESPONSE_ERROR = "The model returned an empty response."


app = FastAPI(title="Local Chat UI")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def effective_max_tokens(payload: dict[str, Any]) -> int:
    """
    Return the most conservative positive output-token limit present in payload.

    Some providers use max_tokens, some use max_completion_tokens.
    If both are present, use the smaller one for truncation detection.
    """
    values: list[int] = []
    for key in ("max_tokens", "max_completion_tokens"):
        v = _as_int(payload.get(key), 0)
        if v > 0:
            values.append(v)
    return min(values) if values else 0


def detect_truncated(
    finish_reason: Optional[str],
    completion_tokens: Optional[int],
    max_tokens: int,
) -> bool:
    if finish_reason == "length":
        return True

    if max_tokens > 0 and completion_tokens is not None:
        try:
            if int(completion_tokens) >= int(max_tokens):
                return True
        except (TypeError, ValueError):
            pass

    return False


def apply_truncation_notice(
    content: str,
    reasoning: str,
    truncated: bool,
    finish_reason: str = "",
) -> str:
    """
    Make truncated / reasoning-only answers visible even if the frontend
    does not yet render the separate `truncated` flag.
    """
    if not truncated:
        return content

    content = content or ""
    reasoning = reasoning or ""

    if not content.strip():
        if reasoning.strip():
            if finish_reason == "length":
                return TRUNCATION_EMPTY_LIMIT_WITH_REASONING_NOTICE
            return REASONING_ONLY_NOTICE
        return TRUNCATION_EMPTY_LIMIT_GENERIC_NOTICE

    if TRUNCATION_PARTIAL_NOTICE.strip() in content:
        return content

    return content.rstrip() + TRUNCATION_PARTIAL_NOTICE
    
def clean_attachments(raw: Any) -> list[dict[str, Any]]:
    """
    Минимальная поддержка:
    - image: data URL base64
    - text: уже прочитанный на фронтенде текст

    Важно:
    это намеренно простой вариант без файлового хранилища.
    """
    if not raw:
        return []

    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="Attachments must be an array")

    if len(raw) > MAX_ATTACHMENTS:
        raise HTTPException(status_code=400, detail=f"Too many attachments, max {MAX_ATTACHMENTS}")

    out: list[dict[str, Any]] = []

    for item in raw:
        if not isinstance(item, dict):
            continue

        name = Path(str(item.get("name") or "file")).name[:180]
        mime = str(item.get("mime") or "").lower()
        kind = str(item.get("kind") or "")
        size = _as_int(item.get("size"), 0)
        suffix = Path(name).suffix.lower()

        if kind == "image" or mime.startswith("image/"):
            data = str(item.get("data") or "")

            if not data.startswith("data:image/") or "," not in data:
                raise HTTPException(status_code=400, detail=f"Invalid image data URL: {name}")

            b64 = data.split(",", 1)[1]

            try:
                decoded = base64.b64decode(b64, validate=True)
            except Exception:
                raise HTTPException(status_code=400, detail=f"Invalid base64 image: {name}")

            if len(decoded) > MAX_IMAGE_BYTES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Image is too large: {name}. Max {MAX_IMAGE_BYTES // 1024 // 1024} MB",
                )

            out.append({
                "kind": "image",
                "name": name,
                "mime": mime or "image/*",
                "size": len(decoded),
                "data": data,
            })

        elif kind == "text" or mime.startswith("text/") or suffix in TEXT_EXTS:
            text = str(item.get("text") or "")
            encoded = text.encode("utf-8", errors="replace")

            if len(encoded) > MAX_TEXT_BYTES:
                encoded = encoded[:MAX_TEXT_BYTES]
                text = encoded.decode("utf-8", errors="ignore")
                text += "\n\n[File truncated by Local AI Chat]"

            out.append({
                "kind": "text",
                "name": name,
                "mime": mime or "text/plain",
                "size": len(encoded),
                "text": text,
            })

        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type: {name}. For now use images or text/code files.",
            )

    return out


def parse_stored_attachments(row: sqlite3.Row) -> list[dict[str, Any]]:
    if "attachments" not in row.keys():
        return []

    try:
        value = json.loads(row["attachments"] or "[]")
        return value if isinstance(value, list) else []
    except Exception:
        return []


def public_attachments(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Не отдаём base64 и полный текст файлов обратно во фронтенд при каждом открытии истории.
    В БД они есть, backend сможет использовать их при следующих запросах.
    """
    out = []

    for a in attachments:
        out.append({
            "kind": a.get("kind", ""),
            "name": a.get("name", "file"),
            "mime": a.get("mime", ""),
            "size": a.get("size", 0),
            "text_chars": len(a.get("text") or "") if a.get("kind") == "text" else 0,
        })

    return out


def content_for_api(content: str, attachments: list[dict[str, Any]]) -> Any:
    """
    Собирает content для OpenAI-compatible chat/completions.

    Если есть картинки — возвращаем массив content parts.
    Если только текст — обычную строку.
    """
    text_parts = [content or ""]

    for a in attachments:
        if a.get("kind") == "text":
            file_text = a.get("text") or ""
            name = a.get("name") or "file"

            text_parts.append(
                f"\n\n--- Attached file: {name} ---\n"
                f"{file_text}\n"
                f"--- End attached file: {name} ---"
            )

    text = "".join(text_parts).strip()

    images = [a for a in attachments if a.get("kind") == "image" and a.get("data")]

    if images:
        parts: list[dict[str, Any]] = []

        if text:
            parts.append({
                "type": "text",
                "text": text,
            })

        for img in images:
            parts.append({
                "type": "image_url",
                "image_url": {
                    "url": img["data"],
                },
            })

        return parts

    return text


def estimate_api_content(content: Any) -> int:
    """
    Примерная оценка токенов для строки или multimodal content array.
    Картинки оцениваем грубо, потому что реальные vision-токены зависят от провайдера.
    """
    if isinstance(content, list):
        total = 0

        for part in content:
            if not isinstance(part, dict):
                continue

            if part.get("type") == "text":
                total += estimate_tokens(str(part.get("text") or ""))
            elif part.get("type") == "image_url":
                total += 1024

        return total

    return estimate_tokens(str(content or ""))


def estimate_user_message_tokens(content: str, attachments: list[dict[str, Any]]) -> int:
    return estimate_api_content(content_for_api(content, attachments))


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def load_settings() -> dict[str, Any]:
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                s.update(stored)
        except (OSError, ValueError):
            pass
    return s


def save_settings(update: dict[str, Any]) -> dict[str, Any]:
    s = load_settings()
    s.update(_clean_settings(update))
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SETTINGS_PATH)
    return s


def _clean_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only known keys and coerce types."""
    out: dict[str, Any] = {}

    for key, value in (payload or {}).items():
        if key not in DEFAULT_SETTINGS:
            continue

        if key in ("stream"):
            out[key] = bool(value)

        elif key == "temperature":
            try:
                out[key] = min(2.0, max(0.0, float(value)))
            except (TypeError, ValueError):
                continue

        elif key in (
            "history_limit",
            "timeout",
            "max_context_tokens",
            "max_tokens",
            "reasoning_budget_tokens",
            "reasoning_reserve_tokens",
        ):
            try:
                if key == "history_limit":
                    lo, hi = 1, 500
                elif key == "timeout":
                    lo, hi = 10, 3600
                elif key == "max_context_tokens":
                    lo, hi = 0, 2_000_000
                elif key == "max_tokens":
                    lo, hi = 0, 2_000_000
                elif key == "reasoning_budget_tokens":
                    lo, hi = 0, 2_000_000
                else:  # reasoning_reserve_tokens
                    lo, hi = 0, 2_000_000

                out[key] = int(min(hi, max(lo, int(value))))
            except (TypeError, ValueError):
                continue

        else:
            out[key] = str(value if value is not None else "")

    return out


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def init_db() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id         TEXT PRIMARY KEY,
                title      TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id                TEXT PRIMARY KEY,
                conversation_id   TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role              TEXT NOT NULL,
                content           TEXT NOT NULL DEFAULT '',
                attachments       TEXT NOT NULL DEFAULT '[]',
                reasoning         TEXT NOT NULL DEFAULT '',
                model             TEXT NOT NULL DEFAULT '',
                prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens  INTEGER NOT NULL DEFAULT 0,
                tokens_estimated  INTEGER NOT NULL DEFAULT 0,
                elapsed_ms        INTEGER NOT NULL DEFAULT 0,
                created_at        REAL NOT NULL,
                finish_reason     TEXT NOT NULL DEFAULT '',
                truncated         INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, created_at);
            """
        )

        # Migration for databases created before finish_reason/truncated existed.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "attachments" not in cols:
            conn.execute(
                "ALTER TABLE messages ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'"
            )

        if "finish_reason" not in cols:
            conn.execute(
                "ALTER TABLE messages ADD COLUMN finish_reason TEXT NOT NULL DEFAULT ''"
            )

        if "truncated" not in cols:
            conn.execute(
                "ALTER TABLE messages ADD COLUMN truncated INTEGER NOT NULL DEFAULT 0"
            )


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def message_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    attachments = parse_stored_attachments(row)

    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "attachments": public_attachments(attachments),
        "reasoning": row["reasoning"],
        "model": row["model"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "reasoning_tokens": row["reasoning_tokens"],
        "estimated": bool(row["tokens_estimated"]),
        "elapsed_ms": row["elapsed_ms"],
        "created_at": row["created_at"],
        "finish_reason": row["finish_reason"] if "finish_reason" in keys else "",
        "truncated": bool(row["truncated"]) if "truncated" in keys else False,
    }


# --------------------------------------------------------------------------
# Token estimation (heuristic, no external tokenizer dependency)
# --------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Rough estimate: ~1 token per CJK char, ~4 chars per token otherwise."""
    if not text:
        return 0

    cjk = 0
    for ch in text:
        o = ord(ch)
        if 0x2E80 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF or 0xF900 <= o <= 0xFAFF:
            cjk += 1

    other = len(text) - cjk
    return max(1, math.ceil(cjk + other / 4) + 4)  # +4: chat message overhead


def build_context(
    history: list[dict[str, str]],
    system_prompt: str,
    max_tokens: int,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """
    Fit the chat history into the configured context window (token budget).

    Newest messages are kept; the oldest are dropped first. The system prompt
    and the newest message are always sent, even when the newest message alone
    exceeds the budget (the API then decides how to handle it).

    Dropped messages stay in the stored history - trimming affects only the request.
    max_tokens <= 0 disables the limit.

    Returns (messages_for_api, info) where info describes what was sent:
        {limit, sent, dropped, estimated_prompt_tokens}.
    """
    system: list[dict[str, str]] = []
    if system_prompt and system_prompt.strip():
        system = [{"role": "system", "content": system_prompt.strip()}]

    used = sum(estimate_api_content(m["content"]) for m in system)
    kept: list[dict[str, str]] = []
    dropped = 0

    for pos, msg in enumerate(reversed(history)):
        cost = estimate_api_content(msg["content"])

        if max_tokens > 0 and kept and used + cost > max_tokens:
            dropped = len(history) - pos
            break

        used += cost
        kept.append(msg)

    kept.reverse()

    info = {
        "limit": max_tokens if max_tokens > 0 else None,
        "sent": len(kept),
        "dropped": dropped,
        "estimated_prompt_tokens": used,
    }

    return system + kept, info


# --------------------------------------------------------------------------
# Upstream (OpenAI-compatible API) helpers
# --------------------------------------------------------------------------

def normalize_base_url(base: str) -> str:
    base = (base or "").strip().rstrip("/")
    if not base:
        raise HTTPException(status_code=400, detail="API Base URL is not configured")

    parts = urlsplit(base)
    if parts.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail=f"Unsupported URL scheme: {parts.scheme!r}")

    if parts.path in ("", "/"):
        base += "/v1"  # allow pasting just the host, e.g. https://api.openai.com

    return base


def auth_headers(settings: dict[str, Any]) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.get("api_key"):
        headers["Authorization"] = f"Bearer {settings['api_key']}"
    return headers


def build_client(settings: dict[str, Any], read_timeout: Optional[float] = None) -> httpx.AsyncClient:
    timeout = httpx.Timeout(
        connect=15.0,
        read=read_timeout or float(settings.get("timeout", 300)),
        write=60.0,
        pool=30.0,
    )

    return httpx.AsyncClient(
        timeout=timeout,
        verify=bool(settings.get("verify_ssl", True)),
        trust_env=False,
        follow_redirects=True,
    )


def extract_error_message(text: str) -> str:
    try:
        data = json.loads(text)
        err = data.get("error")

        if isinstance(err, dict):
            return str(err.get("message") or err)

        if isinstance(err, str):
            return err

        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])

    except ValueError:
        pass

    text = text.strip().replace("\n", " ")
    return text[:300] if text else "(empty response body)"


def chunk_delta(chunk: dict[str, Any]):
    """Extract (content, reasoning, finish_reason, usage) from a streamed chunk."""
    usage = chunk.get("usage")
    choices = chunk.get("choices") or []

    if not choices:
        return None, None, None, usage

    first = choices[0] or {}
    delta = first.get("delta") or {}

    content = delta.get("content")
    reasoning = delta.get("reasoning") or delta.get("reasoning_content")

    if not reasoning:
        rd = delta.get("reasoning_details")
        if isinstance(rd, list):
            reasoning = "".join(p.get("text", "") for p in rd if isinstance(p, dict))

    return content, reasoning, first.get("finish_reason"), usage


def message_fields(message: dict[str, Any]) -> tuple[str, str]:
    """Extract (content, reasoning) from a non-streamed response message."""
    content = message.get("content") or ""
    reasoning = message.get("reasoning") or message.get("reasoning_content") or ""

    if not reasoning:
        rd = message.get("reasoning_details")
        if isinstance(rd, list):
            reasoning = "".join(p.get("text", "") for p in rd if isinstance(p, dict))

    return content, reasoning


def friendly_http_error(exc: Exception) -> str:
    if isinstance(exc, ImportError):
        return str(exc)

    name = type(exc).__name__

    if isinstance(exc, httpx.ProxyError):
        return f"Proxy error: {exc} — check the proxy URL in Settings."

    if isinstance(exc, httpx.ConnectError):
        return f"Connection failed: {exc} — check the API Base URL, proxy settings and your network."

    if isinstance(exc, httpx.ConnectTimeout):
        return f"Connection timed out: {exc}"

    if isinstance(exc, httpx.ReadTimeout):
        return f"The server took too long to respond (read timeout). {exc}"

    url = ""
    if hasattr(exc, "request") and getattr(exc, "request", None) is not None:
        url = str(getattr(exc.request, "url", ""))

    return f"{name}: {exc}{(' (' + url + ')') if url else ''}"


def sse(obj: dict[str, Any]) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def conversation_totals(conn: sqlite3.Connection, cid: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN role='assistant' THEN prompt_tokens END), 0)     AS prompt_tokens,
            COALESCE(SUM(CASE WHEN role='assistant' THEN completion_tokens END), 0) AS completion_tokens,
            COALESCE(SUM(CASE WHEN role='assistant' THEN reasoning_tokens END), 0)  AS reasoning_tokens,
            COALESCE(SUM(CASE WHEN role='user' THEN prompt_tokens END), 0)          AS user_est_tokens,
            COALESCE(MAX(CASE WHEN role='assistant' THEN tokens_estimated END), 0)  AS any_estimated
        FROM messages
        WHERE conversation_id = ?
        """,
        (cid,),
    ).fetchone()

    return {
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "reasoning_tokens": row["reasoning_tokens"],
        "user_est_tokens": row["user_est_tokens"],
        "estimated": bool(row["any_estimated"]),
    }


# --------------------------------------------------------------------------
# Static frontend
# --------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/api/health")
def health():
    return {"ok": True}


# --------------------------------------------------------------------------
# Settings / test / models
# --------------------------------------------------------------------------

@app.get("/api/settings")
def api_settings_get():
    return load_settings()


@app.post("/api/settings")
def api_settings_post(payload: dict[str, Any]):
    return save_settings(payload or {})


@app.post("/api/test")
async def api_test(payload: dict[str, Any]):
    """Validate connection settings and list available models."""
    settings = load_settings()
    settings.update(_clean_settings(payload or {}))

    try:
        base = normalize_base_url(settings.get("base_url", ""))
        t0 = time.time()

        async with build_client(settings, read_timeout=20) as client:
            resp = await client.get(f"{base}/models", headers=auth_headers(settings))

        latency = int((time.time() - t0) * 1000)

        if resp.status_code != 200:
            return {
                "ok": False,
                "error": f"HTTP {resp.status_code}: {extract_error_message(resp.text)}",
            }

        data = resp.json()
        models = [m.get("id") for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]

        return {"ok": True, "latency_ms": latency, "models": models, "base_url": base}

    except HTTPException as exc:
        return {"ok": False, "error": str(exc.detail)}
    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        return {"ok": False, "error": friendly_http_error(exc)}


@app.get("/api/models")
async def api_models():
    settings = load_settings()

    try:
        base = normalize_base_url(settings.get("base_url", ""))
    except HTTPException as exc:
        raise HTTPException(status_code=400, detail=str(exc.detail))

    try:
        async with build_client(settings, read_timeout=20) as client:
            resp = await client.get(f"{base}/models", headers=auth_headers(settings))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=friendly_http_error(exc))

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"HTTP {resp.status_code}: {extract_error_message(resp.text)}",
        )

    data = resp.json()
    models = [m.get("id") for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
    return {"models": models}


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------

@app.post("/api/chat")
async def api_chat(request: Request):
    raw_body = await request.body()

    if len(raw_body) > MAX_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="Request is too large")

    try:
        body = json.loads(raw_body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    settings = load_settings()
    base = normalize_base_url(settings.get("base_url", ""))

    content = str(body.get("content") or "").strip()
    attachments = clean_attachments(body.get("attachments"))
    regenerate = bool(body.get("regenerate"))
    cid = str(body.get("conversation_id") or "") or None

    if not regenerate and not content and not attachments:
        raise HTTPException(status_code=400, detail="Message is empty")

    user_message: Optional[dict[str, Any]] = None

    with get_db() as conn:
        if cid:
            row = conn.execute("SELECT id FROM conversations WHERE id=?", (cid,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Conversation not found")
        else:
            cid = uuid.uuid4().hex
            title = (content or (attachments[0]["name"] if attachments else "New chat"))[:80]
            now = time.time()
            conn.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?,?,?,?)",
                (cid, title, now, now),
            )

        if regenerate:
            last_user = conn.execute(
                "SELECT MAX(created_at) FROM messages WHERE conversation_id=? AND role='user'",
                (cid,),
            ).fetchone()[0]

            if last_user is None:
                raise HTTPException(status_code=400, detail="Nothing to regenerate")

            conn.execute(
                "DELETE FROM messages WHERE conversation_id=? AND role='assistant' AND created_at > ?",
                (cid, last_user),
            )
            conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (time.time(), cid))
        else:
            mid = uuid.uuid4().hex
            now = time.time()
            user_est = estimate_user_message_tokens(content, attachments)

            conn.execute(
                """
                INSERT INTO messages
                (id, conversation_id, role, content, attachments, prompt_tokens, tokens_estimated, created_at)
                VALUES (?,?,?,?,?,?,1,?)
                """,
                (
                    mid,cid,"user",content,json.dumps(attachments, ensure_ascii=False),user_est,now,
                ),
            )

            user_message = {
                "id": mid,
                "role": "user",
                "content": content,
                "reasoning": "",
                "model": "",
                "attachments": public_attachments(attachments),
                "prompt_tokens": user_est,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "estimated": True,
                "elapsed_ms": 0,
                "created_at": now,
                "finish_reason": "",
                "truncated": False,
            }

        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at, id",
            (cid,),
        ).fetchall()

    history: list[dict[str, str]] = []

    for r in rows:
        if r["role"] == "assistant":
            row_keys = r.keys()
            truncated = bool(r["truncated"]) if "truncated" in row_keys else False

            # Do not send empty or truncated/incomplete assistant messages back to the API.
            # Reasoning is not sent to the API anyway, and truncated answers can confuse the model.
            if not str(r["content"] or "").strip() or truncated:
                continue

        atts = parse_stored_attachments(r) if r["role"] == "user" else []
        api_content = content_for_api(r["content"], atts)

        history.append({
            "role": r["role"],
            "content": api_content,
        })

    limit = int(settings.get("history_limit", 50))
    history = history[-limit:]

    max_ctx = int(settings.get("max_context_tokens", 0) or 0)
    messages, ctx_info = build_context(history, settings.get("system_prompt", ""), max_ctx)

    payload: dict[str, Any] = {
        "model": settings.get("model", ""),
        "messages": messages,
    }

    if settings.get("temperature") is not None:
        payload["temperature"] = float(settings["temperature"])

    # Output token limit.
    # 0 means: do not send max_tokens and use the provider default.
    max_out = _as_int(settings.get("max_tokens"), 0)
    if max_out > 0:
        payload["max_tokens"] = max_out

    # Optional Anthropic-like reasoning/thinking budget.
    # Disabled by default (0). Can be set in data/settings.json.
    reasoning_budget = _as_int(settings.get("reasoning_budget_tokens"), 0)
    if reasoning_budget > 0:
        payload.setdefault(
            "thinking",
            {
                "type": "enabled",
                "budget_tokens": reasoning_budget,
            },
        )

    # Optional provider-specific extra parameters.
    # Examples:
    #   {"max_completion_tokens": 32768}
    #   {"reasoning": {"effort": "low"}}
    #   {"thinking": {"type": "enabled", "budget_tokens": 8192}}
    extra_raw = str(settings.get("extra_params") or "").strip()
    if extra_raw:
        try:
            extra = json.loads(extra_raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid Extra API params JSON: {exc}")

        if not isinstance(extra, dict):
            raise HTTPException(status_code=400, detail="Extra API params must be a JSON object")

        # Protect transport-critical fields.
        for protected_key in ("messages", "model", "stream", "stream_options"):
            extra.pop(protected_key, None)

        payload.update(extra)

    # Safety guard: if a thinking/reasoning budget is present, make sure the total
    # output limit is larger than the budget. Otherwise the model may spend all
    # tokens on reasoning and return no final answer.
    thinking = payload.get("thinking")
    thinking_budget = 0

    if isinstance(thinking, dict):
        thinking_budget = _as_int(thinking.get("budget_tokens"), 0)

    if thinking_budget > 0:
        reserve = _as_int(settings.get("reasoning_reserve_tokens"), 4096)
        if reserve <= 0:
            reserve = 4096

        current_max = effective_max_tokens(payload)

        if current_max <= thinking_budget:
            new_max = thinking_budget + reserve
            payload["max_tokens"] = new_max

            if "max_completion_tokens" in payload:
                payload["max_completion_tokens"] = new_max

    # Keep streaming control under app control, after extra params,
    # so users cannot accidentally break SSE handling.
    stream = bool(settings.get("stream", True))
    payload["stream"] = stream

    if stream:
        payload["stream_options"] = {"include_usage": True}

    est_prompt = ctx_info["estimated_prompt_tokens"]
    url = f"{base}/chat/completions"
    headers = auth_headers(settings)

    if not stream:
        return await chat_non_stream(
            settings,
            url,
            headers,
            payload,
            cid,
            user_message,
            est_prompt,
            ctx_info,
        )

    return StreamingResponse(
        chat_stream(
            settings,
            url,
            headers,
            payload,
            cid,
            user_message,
            est_prompt,
            ctx_info,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def chat_non_stream(
    settings,
    url,
    headers,
    payload,
    cid,
    user_message,
    est_prompt,
    ctx_info=None,
):
    t0 = time.time()
    err: Optional[str] = None
    data: Optional[dict[str, Any]] = None
    elapsed = 0

    try:
        async with build_client(settings) as client:
            resp = await client.post(url, headers=headers, json=payload)

        elapsed = int((time.time() - t0) * 1000)

        if resp.status_code != 200:
            err = f"HTTP {resp.status_code}: {extract_error_message(resp.text)}"
        else:
            data = resp.json()

    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.time() - t0) * 1000)
        err = friendly_http_error(exc)

    if err and data is None:
        return JSONResponse(status_code=502, content={"error": err})

    if data is None:
        return JSONResponse(status_code=502, content={"error": "Empty upstream response"})

    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    finish_reason = str(choice.get("finish_reason") or "")

    content, reasoning = message_fields(msg)

    usage = data.get("usage") or {}
    pt = usage.get("prompt_tokens")
    ct = usage.get("completion_tokens")
    rt = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")

    estimated = not usage

    if pt is None:
        pt = est_prompt

    if ct is None:
        ct = estimate_tokens(content) + (estimate_tokens(reasoning) if reasoning else 0)

    if rt is None and reasoning:
        rt = estimate_tokens(reasoning)

    max_lim = effective_max_tokens(payload)

    # Only trust token-count-based truncation detection when the API reported usage.
    ct_for_truncation = ct if not estimated else None
    truncated = detect_truncated(finish_reason, ct_for_truncation, max_lim)

    # If the model returned only reasoning and no final answer, treat it as incomplete.
    if not str(content or "").strip() and str(reasoning or "").strip():
        truncated = True

    # Completely empty answer with no reasoning is an error, not a saved assistant message.
    if not str(content or "").strip() and not str(reasoning or "").strip() and not err:
        return JSONResponse(status_code=502, content={"error": EMPTY_RESPONSE_ERROR})

    content = apply_truncation_notice(content, reasoning, truncated, finish_reason)

    mid = uuid.uuid4().hex

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO messages
                (id, conversation_id, role, content, reasoning, model,
                 prompt_tokens, completion_tokens, reasoning_tokens,
                 tokens_estimated, elapsed_ms, created_at,
                 finish_reason, truncated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                mid,
                cid,
                "assistant",
                content,
                reasoning,
                payload.get("model", ""),
                int(pt or 0),
                int(ct or 0),
                int(rt or 0),
                1 if estimated else 0,
                elapsed,
                time.time(),
                finish_reason,
                1 if truncated else 0,
            ),
        )

        assistant = message_dict(
            conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        )
        totals = conversation_totals(conn, cid)

    return {
        "conversation_id": cid,
        "user_message": user_message,
        "message": assistant,
        "usage": totals,
        "context": ctx_info,
    }


async def chat_stream(
    settings,
    url,
    headers,
    payload,
    cid,
    user_message,
    est_prompt,
    ctx_info=None,
):
    """SSE generator: proxies the upstream stream and emits our own events."""
    yield sse(
        {
            "type": "start",
            "conversation_id": cid,
            "user_message": user_message,
            "context": ctx_info,
        }
    )

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: Optional[dict[str, Any]] = None
    finish: Optional[str] = None
    err: Optional[str] = None
    saved = False

    t0 = time.time()
    pl = payload

    try:
        attempt = 0

        while attempt < 2:
            attempt += 1

            async with build_client(settings) as client:
                async with client.stream("POST", url, headers=headers, json=pl) as resp:
                    if resp.status_code != 200:
                        body_text = (await resp.aread()).decode("utf-8", "replace")

                        # Some providers reject stream_options; retry once without it.
                        if (
                            resp.status_code == 400
                            and attempt == 1
                            and "stream_options" in body_text
                            and "stream_options" in pl
                        ):
                            pl = {k: v for k, v in payload.items() if k != "stream_options"}
                            continue

                        err = f"HTTP {resp.status_code}: {extract_error_message(body_text)}"
                        break

                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue

                        data_str = line[5:].strip()

                        if data_str == "[DONE]":
                            break

                        try:
                            chunk = json.loads(data_str)
                        except ValueError:
                            continue

                        c, r, f, u = chunk_delta(chunk)

                        if c:
                            content_parts.append(c)
                            yield sse({"type": "delta", "text": c})

                        if r:
                            reasoning_parts.append(r)
                            yield sse({"type": "reasoning", "text": r})

                        if u:
                            usage = u

                        if f:
                            finish = f

                    break  # streamed to completion (or non-retryable error) -> leave loop

    except HTTPException as exc:
        err = str(exc.detail)
    except Exception as exc:  # noqa: BLE001
        err = friendly_http_error(exc)

    content = "".join(content_parts)
    reasoning = "".join(reasoning_parts)
    elapsed = int((time.time() - t0) * 1000)

    pt = (usage or {}).get("prompt_tokens")
    ct = (usage or {}).get("completion_tokens")
    rt = ((usage or {}).get("completion_tokens_details") or {}).get("reasoning_tokens")

    estimated = not usage

    if pt is None:
        pt = est_prompt

    if ct is None:
        ct = estimate_tokens(content) + (estimate_tokens(reasoning) if reasoning else 0)

    if rt is None and reasoning:
        rt = estimate_tokens(reasoning)

    max_lim = effective_max_tokens(pl)

    # Only trust token-count-based truncation detection when the API reported usage.
    ct_for_truncation = ct if not estimated else None
    truncated = detect_truncated(finish, ct_for_truncation, max_lim)

    # If the model returned only reasoning and no final answer, treat it as incomplete.
    if not str(content or "").strip() and str(reasoning or "").strip():
        truncated = True

    # Completely empty answer with no reasoning is an error, not a saved assistant message.
    if not str(content or "").strip() and not str(reasoning or "").strip() and not err:
        err = EMPTY_RESPONSE_ERROR

    content = apply_truncation_notice(content, reasoning, truncated, finish or "")

    def save(finish_reason: str, truncated_flag: bool = False) -> dict[str, Any]:
        nonlocal saved
        saved = True

        mid = uuid.uuid4().hex

        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO messages
                    (id, conversation_id, role, content, reasoning, model,
                     prompt_tokens, completion_tokens, reasoning_tokens,
                     tokens_estimated, elapsed_ms, created_at,
                     finish_reason, truncated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    mid,
                    cid,
                    "assistant",
                    content,
                    reasoning,
                    payload.get("model", ""),
                    int(pt or 0),
                    int(ct or 0),
                    int(rt or 0),
                    1 if estimated else 0,
                    elapsed,
                    time.time(),
                    finish_reason or "",
                    1 if truncated_flag else 0,
                ),
            )

            row = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
            totals = conversation_totals(conn, cid)
            result = message_dict(row)
            result["finish_reason"] = finish_reason or ""
            result["truncated"] = bool(truncated_flag)
            return result

    message_json: Optional[dict[str, Any]] = None

    try:
        if content or reasoning or not err:
            message_json = save(finish or ("error" if err else "stop"), truncated)
    finally:
        if not saved and (content or reasoning):
            # Client disconnected mid-stream: persist the partial answer.
            try:
                save("aborted", False)
            except Exception:
                pass

    totals: Optional[dict[str, Any]] = None

    if message_json is not None:
        with get_db() as conn:
            totals = conversation_totals(conn, cid)

    if truncated:
        yield sse(
            {
                "type": "warning",
                "warning": "Response truncated: output token limit reached or no final answer was generated.",
            }
        )

    if err:
        yield sse({"type": "error", "error": err})

    yield sse({"type": "done", "message": message_json, "usage": totals})


# --------------------------------------------------------------------------
# Conversations
# --------------------------------------------------------------------------

@app.get("/api/conversations")
def api_conversations():
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                c.id,
                c.title,
                c.updated_at,
                COUNT(m.id) AS msg_count,
                COALESCE(SUM(CASE WHEN m.role='assistant' THEN m.prompt_tokens END), 0)     AS prompt_tokens,
                COALESCE(SUM(CASE WHEN m.role='assistant' THEN m.completion_tokens END), 0) AS completion_tokens,
                COALESCE(SUM(CASE WHEN m.role='assistant' THEN m.reasoning_tokens END), 0)  AS reasoning_tokens,
                COALESCE(SUM(CASE WHEN m.role='user' THEN m.prompt_tokens END), 0)          AS user_est_tokens
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            GROUP BY c.id
            ORDER BY c.updated_at DESC
            """
        ).fetchall()

        return [dict(r) for r in rows]


@app.get("/api/conversations/{cid}")
def api_conversation_detail(cid: str):
    with get_db() as conn:
        conv = conn.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()

        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")

        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at, id",
            (cid,),
        ).fetchall()

        return {
            "id": conv["id"],
            "title": conv["title"],
            "created_at": conv["created_at"],
            "updated_at": conv["updated_at"],
            "messages": [message_dict(r) for r in rows],
        }


@app.patch("/api/conversations/{cid}")
def api_conversation_rename(cid: str, payload: dict[str, Any]):
    title = str((payload or {}).get("title") or "").strip()

    if not title:
        raise HTTPException(status_code=400, detail="Title is empty")

    with get_db() as conn:
        cur = conn.execute("UPDATE conversations SET title=? WHERE id=?", (title[:120], cid))

        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Conversation not found")

        return {"ok": True, "title": title[:120]}


@app.delete("/api/conversations/{cid}")
def api_conversation_delete(cid: str):
    with get_db() as conn:
        cur = conn.execute("DELETE FROM conversations WHERE id=?", (cid,))

        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Conversation not found")

        return {"ok": True}


@app.get("/api/usage")
def api_usage():
    with get_db() as conn:
        overall = conn.execute(
            """
            SELECT
                COUNT(*) AS calls,
                COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(reasoning_tokens), 0)  AS reasoning_tokens
            FROM messages
            WHERE role='assistant'
            """
        ).fetchone()

        by_model = conn.execute(
            """
            SELECT
                COALESCE(NULLIF(model, ''), 'unknown') AS model,
                COUNT(*) AS calls,
                COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(reasoning_tokens), 0)  AS reasoning_tokens
            FROM messages
            WHERE role='assistant'
            GROUP BY model
            ORDER BY calls DESC
            """
        ).fetchall()

        by_day = conn.execute(
            """
            SELECT
                date(created_at, 'unixepoch') AS day,
                COUNT(*) AS calls,
                COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens
            FROM messages
            WHERE role='assistant'
              AND created_at >= strftime('%s', 'now', '-13 days')
            GROUP BY day
            ORDER BY day
            """
        ).fetchall()

        return {
            "overall": dict(overall),
            "by_model": [dict(r) for r in by_model],
            "by_day": [dict(r) for r in by_day],
        }


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main() -> None:
    global DATA_DIR, DB_PATH, SETTINGS_PATH

    parser = argparse.ArgumentParser(description="Local chat UI for OpenAI-compatible APIs")
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: 0.0.0.0 = LAN visible)")
    parser.add_argument("--port", type=int, default=3080, help="port (default: 3080)")
    parser.add_argument("--data-dir", default=None, help="directory for chat.db / settings.json")

    args = parser.parse_args()

    if args.data_dir:
        DATA_DIR = Path(args.data_dir).expanduser().resolve()
        DB_PATH = DATA_DIR / "chat.db"
        SETTINGS_PATH = DATA_DIR / "settings.json"

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    init_db()

    print()
    print("  Local AI Chat UI")
    print(f"  Local:    http://127.0.0.1:{args.port}")
    print(f"  Network:  http://{local_ip()}:{args.port}")
    print("  Settings (API key / model / proxy) are configured in the web interface.")
    print()

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()