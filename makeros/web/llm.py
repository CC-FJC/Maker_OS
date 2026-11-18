"""Local LLM layer (LM Studio / Ollama, OpenAI-compatible).

Optional brain of the platform: an admin points it at a local
OpenAI-compatible server (LM Studio's default is
http://localhost:1234/v1). Every caller must tolerate ``None`` — the
platform works fully without a model; UIs degrade to a plain
"modelo no disponible" state.
"""

from __future__ import annotations

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
    set_ui(conn, KEY_BASE_URL, base_url.strip().rstrip("/"))
    set_ui(conn, KEY_MODEL, model.strip())


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


def chat(conn: sqlite3.Connection, messages: list[dict]) -> str | None:
    """One-shot chat completion; None = unconfigured or offline."""
    base, model = llm_config(conn)
    if not base or not model:
        return None
    try:
        response = httpx.post(
            f"{base}/chat/completions",
            json={"model": model, "messages": messages},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return content.strip() if content else None
    except Exception:  # noqa: BLE001 — model offline is a normal state
        return None
