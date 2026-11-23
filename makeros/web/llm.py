"""Local LLM layer (LM Studio / Ollama, OpenAI-compatible).

Optional brain of the platform: an admin points it at a local
OpenAI-compatible server (LM Studio's default is
http://localhost:1234/v1). Every caller must tolerate ``None`` — the
platform works fully without a model; UIs degrade to a plain
"modelo no disponible" state.
"""

from __future__ import annotations

import json
import sqlite3

import httpx

from ..ui import get_ui, set_ui

# ui_settings keys
KEY_BASE_URL = "llm:base_url"
KEY_MODEL = "llm:model"

TIMEOUT = 120.0  # local models can be slow on the first token


def llm_config(conn: sqlite3.Connection) -> tuple[str, str]:
    """(base_url, model); an empty pair means the layer is off."""
    return get_ui(conn, KEY_BASE_URL), get_ui(conn, KEY_MODEL)


def save_config(conn: sqlite3.Connection, base_url: str, model: str) -> None:
    base = normalize_base_url(base_url) if base_url.strip() else ""
    set_ui(conn, KEY_BASE_URL, base)
    set_ui(conn, KEY_MODEL, model.strip())


def api_root(base_url: str) -> str:
    """LM Studio's own API lives at the server root, next to /v1."""
    base = base_url.strip().rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def normalize_base_url(url: str) -> str:
    """Accept both http://host:1234 and http://host:1234/v1."""
    base = url.strip().rstrip("/")
    if base and not base.endswith("/v1"):
        base += "/v1"
    return base


def ping(base_url: str, timeout: float = 3.0) -> bool:
    """True when the OpenAI-compatible server answers /models."""
    base = base_url.strip().rstrip("/")
    if not base:
        return False
    try:
        r = httpx.get(f"{base}/models", timeout=timeout)
        r.raise_for_status()
        return True
    except Exception:  # noqa: BLE001 — offline is a normal state
        return False


def list_models_detailed(base_url: str, timeout: float = 5.0) -> list[dict]:
    """All models known to the server with their load state.

    LM Studio's `/v1/models` only shows loaded models; `/api/v0/models`
    lists everything downloaded with `state: loaded|not-loaded`. Ollama
    only has `/v1/models`, so that list marks everything as loaded.
    """
    base = base_url.strip().rstrip("/")
    if not base:
        return []
    out: dict[str, dict] = {}

    def put(mid: str, **kw) -> None:
        if not mid:
            return
        entry = out.setdefault(mid, {"id": mid, "loaded": False, "state": "not-loaded"})
        entry.update({k: v for k, v in kw.items() if v is not None})

    try:
        r = httpx.get(f"{base}/models", timeout=timeout)
        r.raise_for_status()
        for item in r.json().get("data", []):
            if isinstance(item, dict):
                put(item.get("id", ""), loaded=True, state="loaded", type=item.get("type"))
            else:
                put(str(item), loaded=True, state="loaded")
    except Exception:  # noqa: BLE001
        pass
    try:
        r = httpx.get(f"{api_root(base)}/api/v0/models", timeout=timeout)
        r.raise_for_status()
        for item in r.json().get("data", []):
            if not isinstance(item, dict):
                continue
            put(
                item.get("id", ""),
                type=item.get("type"),
                state=item.get("state"),
                loaded=item.get("state") == "loaded",
                quantization=item.get("quantization"),
                max_context_length=item.get("max_context_length"),
                publisher=item.get("publisher"),
            )
    except Exception:  # noqa: BLE001
        pass
    models = sorted(out.values(), key=lambda m: (not m["loaded"], str(m["id"]).lower()))
    return models


def load_model(base_url: str, model_id: str, timeout: float = 120.0) -> tuple[bool, str]:
    """Load a model into LM Studio memory (v1 API, v0 fallback)."""
    root = api_root(base_url)
    if not root or not model_id:
        return False, "Servidor o modelo vacío"
    payloads = [{"model": model_id}, {"instance_id": model_id}]
    errors: list[str] = []
    for path in ("/api/v1/models/load", "/api/v0/models/load"):
        for payload in payloads:
            try:
                r = httpx.post(f"{root}{path}", json=payload, timeout=timeout)
                if r.status_code < 300:
                    return True, f"Modelo {model_id} cargado"
                errors.append(f"{path}: HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path}: {exc}")
    return False, "; ".join(errors) or "sin respuesta"


def unload_model(base_url: str, model_id: str, timeout: float = 60.0) -> tuple[bool, str]:
    """Unload a model from LM Studio memory (v1 API, v0 fallback)."""
    root = api_root(base_url)
    if not root or not model_id:
        return False, "Servidor o modelo vacío"
    payloads = [{"instance_id": model_id}, {"model": model_id}]
    errors: list[str] = []
    for path in ("/api/v1/models/unload", "/api/v0/models/unload"):
        for payload in payloads:
            try:
                r = httpx.post(f"{root}{path}", json=payload, timeout=timeout)
                if r.status_code < 300:
                    return True, f"Modelo {model_id} descargado"
                errors.append(f"{path}: HTTP {r.status_code}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path}: {exc}")
    return False, "; ".join(errors) or "sin respuesta"


def list_models(base_url: str, timeout: float = 5.0) -> list[str]:
    """Model ids from an OpenAI-compatible server (LM Studio / Ollama).

    `/v1/models` only lists *loaded* models in LM Studio, so the LM
    Studio developer API `/api/v0/models` is merged in to also offer
    the downloaded-but-not-loaded ones.
    """
    ids: list[str] = []
    seen: set[str] = set()

    def add(items) -> None:
        for item in items:
            mid = item.get("id") if isinstance(item, dict) else str(item)
            if mid and mid not in seen:
                seen.add(mid)
                ids.append(mid)

    base = base_url.strip().rstrip("/")
    if base:
        try:
            r = httpx.get(f"{base}/models", timeout=timeout)
            r.raise_for_status()
            add(r.json().get("data", []))
        except Exception:  # noqa: BLE001 — offline is a normal state
            pass
        root = base[:-3] if base.endswith("/v1") else base
        try:
            r = httpx.get(f"{root}/api/v0/models", timeout=timeout)
            r.raise_for_status()
            add(r.json().get("data", []))
        except Exception:  # noqa: BLE001
            pass
    return sorted(ids, key=str.lower)


def configured(conn: sqlite3.Connection) -> bool:
    base, model = llm_config(conn)
    return bool(base and model)


def status(conn: sqlite3.Connection) -> dict:
    """Live health of the LLM layer: server reachable? selected model loaded?

    LM Studio loads models on demand (JIT), so "not loaded" still works —
    it just may take longer on the first message.
    """
    base, model = llm_config(conn)
    reachable = ping(base) if base else False
    models = list_models_detailed(base) if base and reachable else []
    loaded_ids = {m["id"] for m in models if m["loaded"]}
    return {
        "base": base,
        "model": model,
        "reachable": reachable,
        "models": models,
        "model_loaded": (model in loaded_ids) if model else False,
        "ready": bool(base and reachable),
    }


def chat(conn: sqlite3.Connection, messages: list[dict]) -> str | None:
    """One-shot chat completion; None = unconfigured or offline."""
    msg = chat_raw(conn, messages)
    if msg is None:
        return None
    content = msg.get("content") or ""
    return content.strip() or None


def chat_raw(
    conn: sqlite3.Connection, messages: list[dict], tools: list | None = None
) -> dict | None:
    """Raw assistant message dict (may carry tool_calls); None = offline."""
    base, model = llm_config(conn)
    if not base or not model:
        return None
    payload: dict = {"model": model, "messages": messages}
    if tools:
        payload["tools"] = tools
    try:
        response = httpx.post(
            f"{base}/chat/completions", json=payload, timeout=TIMEOUT
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]
    except Exception:  # noqa: BLE001 — model offline is a normal state
        return None


def chat_with_tools(
    conn: sqlite3.Connection,
    messages: list[dict],
    tools: list,
    executor,
    max_steps: int = 5,
) -> tuple[str | None, list[dict]]:
    """Agentic loop: the model may call tools; we execute them server-side
    and feed the results back until it answers in plain text.

    Returns (final_answer, tool_trace). Models without function-calling
    support just answer directly on the first step — the loop degrades
    gracefully to a normal RAG answer.
    """
    trace: list[dict] = []
    conversation = list(messages)
    for _step in range(max_steps):
        msg = chat_raw(conn, conversation, tools=tools)
        if msg is None:
            return None, trace
        calls = msg.get("tool_calls") or []
        if not calls:
            content = (msg.get("content") or "").strip()
            return (content or None), trace
        conversation.append(msg)
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            try:
                result = executor(name, args)
            except Exception as exc:  # noqa: BLE001 — a bad tool call shouldn't kill the chat
                result = {"error": str(exc)}
            trace.append({"name": name, "args": args, "ok": "error" not in result})
            conversation.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", name),
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False)[:8000],
                }
            )
    return None, trace  # ran out of steps: treat as no answer
