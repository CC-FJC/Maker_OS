"""Ask-the-wiki assistant: FTS5 retrieval + local LLM (RAG).

The assistant searches the whole content index with FTS5, feeds the
best pages as context to the local model (LM Studio, OpenAI-compatible)
and renders the answer with links back to the source pages. Without a
model configured or reachable, the page degrades to a notice — the
platform works fully without it.

Answers can be saved as new wiki pages: the Markdown answer becomes a
file under content/wiki/, written through the frontmatter module like
every other page.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ... import frontmatter
from ...indexer import index_file, search_documents
from .. import event_store, llm, vault
from ..deps import DIAS_ES, ConnDep, UserDep, base_ctx, get_settings
from ..render import render_markdown
from ..templates import templates
from .collection_routes import EDIT_ROLES

router = APIRouter()

SYSTEM_PROMPT = (
    "Eres el asistente de MakerOS, el gestor de documentación de un "
    "laboratorio de prototipado. Responde SIEMPRE en español, breve y "
    "directo, usando solo el contexto de las páginas que se te dan. "
    "Cuando menciones una página del contexto, enlázala con su ruta "
    "relativa entre corchetes dobles, por ejemplo [[guia-del-sistema]]. "
    "Si el contexto no basta para responder, dilo claramente."
)
MAX_SOURCES = 6
MAX_CHUNK = 1500


def _chunk_body(body: str, size: int = 1200, overlap: int = 200) -> list[str]:
    """Split a long page into overlapping chunks (retrieval unit)."""
    body = body.strip()
    if len(body) <= size:
        return [body]
    chunks: list[str] = []
    i = 0
    while i < len(body):
        chunks.append(body[i : i + size])
        i += size - overlap
    return chunks


def _pick_chunks(body: str, query: str, max_chunks: int = 2) -> str:
    """Instead of truncating the head of a long page, keep the chunks
    that actually mention the query terms (cheap relevance pick)."""
    chunks = _chunk_body(body)
    if len(chunks) <= max_chunks:
        return "\n\n".join(chunks)
    tokens = {t for t in re.split(r"\W+", query.lower()) if len(t) > 2}

    def score(chunk: str) -> int:
        low = chunk.lower()
        return sum(low.count(t) for t in tokens)

    ranked = sorted(range(len(chunks)), key=lambda i: score(chunks[i]), reverse=True)
    picked = sorted(ranked[:max_chunks])
    return "\n\n[…]\n\n".join(chunks[i] for i in picked)


def _sources_for(conn: sqlite3.Connection, settings, query: str) -> list[dict]:
    """FTS5 hits with a relevant excerpt each — the RAG context.

    FTS5's implicit AND is too strict for natural questions, so an
    empty result falls back to an OR of the query's words. Hits whose
    title or path contains a query term are boosted to the top.
    """
    hits = list(search_documents(conn, query, limit=MAX_SOURCES * 2))
    if not hits:
        tokens = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
        if tokens:
            hits = list(search_documents(conn, " OR ".join(tokens), limit=MAX_SOURCES * 2))
    tokens = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]

    def boosted(doc) -> int:
        title = (doc["title"] or "").lower()
        path = doc["path"].lower()
        return 0 if any(t in title or t in path for t in tokens) else 1

    hits.sort(key=boosted)
    sources: list[dict] = []
    for doc in hits[:MAX_SOURCES]:
        file = settings.content_dir / f"{doc['path']}.md"
        if not file.is_file():
            continue
        _meta, body = frontmatter.read(file)
        sources.append(
            {
                "path": doc["path"],
                "title": doc["title"] or doc["path"],
                "body": _pick_chunks(body, query)[: MAX_CHUNK * 2],
            }
        )
    return sources


def _sessions(conn: sqlite3.Connection, user) -> list[dict]:
    rows = conn.execute(
        "SELECT id, title, updated_at FROM chat_sessions WHERE user_id = ? "
        "ORDER BY updated_at DESC, id DESC",
        (user["id"],),
    ).fetchall()
    return [dict(r) for r in rows]


def _session_or_403(conn: sqlite3.Connection, user, session_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user["id"]),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=403)
    return dict(row)


TITLE_MAX = 60


def _auto_title(texto: str) -> str:
    """Nombre inicial del chat inferido del primer mensaje del usuario.

    Quita las menciones @ruta (son contexto, no tema) y corta en palabra
    completa para que la pestaña se lea bien.
    """
    limpio = re.sub(r"@[a-zA-Z0-9][\w./-]*", " ", texto)
    limpio = " ".join(limpio.split()).strip(" .,;:")
    if not limpio:
        limpio = " ".join(texto.split())[:TITLE_MAX] or "Nuevo chat"
    if len(limpio) <= TITLE_MAX:
        return limpio
    corte = limpio[:TITLE_MAX].rsplit(" ", 1)[0].rstrip(" .,;:")
    return (corte or limpio[:TITLE_MAX]).strip() + "…"


def _messages(conn: sqlite3.Connection, session_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content, sources_json, tools_json, created_at FROM chat_messages "
        "WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    out = []
    for r in rows:
        m = dict(r)
        for key, dest in (("sources_json", "sources"), ("tools_json", "tools")):
            try:
                m[dest] = json.loads(m.pop(key) or "[]")
            except ValueError:
                m[dest] = []
        if m["role"] == "assistant":
            m["content_html"] = render_markdown(m["content"])
        out.append(m)
    return out


def _chat_shell_ctx(request, conn, user, active_id: int | None) -> dict:
    sessions = _sessions(conn, user)
    if active_id is None and sessions:
        active_id = sessions[0]["id"]
    if active_id is not None:
        _session_or_403(conn, user, active_id)
    return base_ctx(
        request, conn, user,
        chat_sessions=sessions,
        active_chat=active_id,
        chat_messages=_messages(conn, active_id) if active_id else [],
    )


def _resolve_mentions(conn, settings, tokens: list[str]) -> list[dict]:
    """@token -> document: exact path, path prefix, then FTS by title."""
    out: list[dict] = []
    seen: set[str] = set()
    for tok in tokens:
        tok = tok.strip("/")
        if not tok:
            continue
        rows = conn.execute(
            "SELECT path, title FROM documents WHERE path = ? OR path LIKE ? LIMIT 5",
            (tok, tok + "/%"),
        ).fetchall()
        if not rows:
            rows = search_documents(conn, tok, limit=3)
        for row in rows:
            path = row["path"]
            if path in seen:
                continue
            seen.add(path)
            file = settings.content_dir / f"{path}.md"
            if not file.is_file():
                continue
            _meta, body = frontmatter.read(file)
            out.append({"path": path, "title": row["title"] or path,
                        "body": _pick_chunks(body, tok)[: MAX_CHUNK * 2]})
    return out


# ── Calendar as LLM context ──────────────────────────────────────

CALENDAR_KEYWORDS = re.compile(
    r"\b(calendario|evento|eventos|reuni[oó]n|reuniones|cita|citas|agenda)\b", re.I
)


def _calendar_range(spec: str) -> tuple[date, date]:
    """@calendario spec → (start, end). Presets or YYYY-MM-DD/YYYY-MM-DD."""
    today = date.today()
    spec = spec.strip().lower()
    if spec in ("", "proximos", "eventos"):
        return today - timedelta(days=7), today + timedelta(days=60)
    if spec == "hoy":
        return today, today
    if spec == "semana":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6)
    if spec == "mes":
        return today, today + timedelta(days=30)
    if spec == "manana":
        return today + timedelta(days=1), today + timedelta(days=1)
    if spec == "proxima-semana":
        start = today - timedelta(days=today.weekday()) + timedelta(days=7)
        return start, start + timedelta(days=6)
    if spec == "proximo-mes":
        first = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        last = first + timedelta(days=32)
        last = last.replace(day=1) - timedelta(days=1)
        return first, last
    if spec == "todo":
        return date(2000, 1, 1), date(2100, 1, 1)
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})", spec)
    if m:
        return date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
    return today - timedelta(days=7), today + timedelta(days=60)


def _spec_from_text(text: str) -> str | None:
    """Relative date phrases in Spanish → calendar spec."""
    t = text.lower()
    if re.search(r"\bmañana\b", t):
        return "manana"
    if re.search(r"(próxima|proxima|siguiente) semana", t):
        return "proxima-semana"
    if re.search(r"\besta semana\b", t):
        return "semana"
    if re.search(r"(próximo|proximo|siguiente) mes", t):
        return "proximo-mes"
    if re.search(r"\beste mes\b", t):
        return "mes"
    if re.search(r"\bhoy\b|\bel día de hoy\b", t):
        return "hoy"
    return None


def _fmt_when(w) -> str:
    if isinstance(w, datetime):
        return w.strftime("%Y-%m-%d %H:%M")
    return w.strftime("%Y-%m-%d")


def _events_in_range(conn, start: date, end: date, cap: int = 100) -> list[dict]:
    """Confirmed events overlapping [start, end], sorted by start."""
    out = []
    for ev in event_store.iter_events(conn):
        s, e = ev["start"], ev["end"]
        s_date = s.date() if isinstance(s, datetime) else s
        e_date = (e.date() if isinstance(e, datetime) else e) if e else s_date
        if s_date > end or e_date < start:
            continue
        out.append({"ev": ev, "s": s, "e": e, "s_date": s_date, "e_date": e_date})
    out.sort(key=lambda r: (r["s_date"], str(r["s"])))
    return out[:cap]


# ── Tool calling: the model searches the vault and calendar itself ──

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "buscar_documentos",
            "description": (
                "Busca texto libre en TODA la documentación indexada (wiki, proyectos, "
                "tareas, repositorios, inventario, personas). Devuelve rutas, títulos y "
                "extractos. Úsala siempre que la pregunta pueda responderse con la docs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "texto de búsqueda en español"},
                    "coleccion": {"type": "string",
                                  "description": "filtrar por colección: wiki, projects, "
                                                "tasks, research, inventory, people"},
                    "limite": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "leer_pagina",
            "description": "Lee el contenido completo de una página por su ruta "
                           "(ej. «wiki/guia-del-sistema»).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_paginas",
            "description": "Lista las rutas de una colección o carpeta "
                           "(ej. «wiki» o «wiki/reports»).",
            "parameters": {
                "type": "object",
                "properties": {"coleccion": {"type": "string"}},
                "required": ["coleccion"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "eventos_calendario",
            "description": (
                "Eventos del calendario en un rango. Fechas YYYY-MM-DD; si el usuario dice "
                "«hoy», «esta semana» o «este mes», calcula el rango con fecha_actual."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "desde": {"type": "string", "description": "YYYY-MM-DD (opcional)"},
                    "hasta": {"type": "string", "description": "YYYY-MM-DD (opcional)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fecha_actual",
            "description": "Fecha y hora actuales del servidor, para interpretar "
                           "fechas relativas del usuario.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _exec_tool(conn, settings, name: str, args: dict) -> dict:
    """Server-side execution of one model tool call."""
    if name == "fecha_actual":
        now = datetime.now()
        from ..deps import DIAS_ES, MESES_ES

        return {
            "iso": now.strftime("%Y-%m-%d %H:%M:%S"),
            "legible": (f"{DIAS_ES[now.weekday()]} {now.day} de "
                        f"{MESES_ES[now.month - 1]} de {now.year}, {now.strftime('%H:%M')}"),
            "dia_semana": DIAS_ES[now.weekday()],
        }
    if name == "buscar_documentos":
        query = str(args.get("query", "")).strip()
        if not query:
            return {"error": "query vacío"}
        coll = str(args.get("coleccion") or "").strip()
        limit = min(int(args.get("limite") or 5), 10)
        hits = list(search_documents(conn, query, limit=limit * 2))
        if not hits:
            toks = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
            if toks:
                hits = list(search_documents(conn, " OR ".join(toks), limit=limit * 2))
        if coll:
            hits = [h for h in hits if h["collection"] == coll]
        out = []
        for h in hits[:limit]:
            file = settings.content_dir / f"{h['path']}.md"
            snippet = ""
            if file.is_file():
                _m, body = frontmatter.read(file)
                snippet = body.strip()[:300].replace("\n", " ")
            out.append({"path": h["path"], "title": h["title"] or h["path"],
                        "collection": h["collection"], "extracto": snippet})
        return {"resultados": out}
    if name == "leer_pagina":
        path = str(args.get("path", "")).strip().strip("/")
        if not vault.valid_vault_path(path):
            return {"error": "ruta no válida"}
        file = settings.content_dir / f"{path}.md"
        if not file.is_file():
            return {"error": f"no existe la página «{path}»"}
        meta, body = frontmatter.read(file)
        return {"path": path, "title": meta.get("title") or path,
                "body": body.strip()[:6000]}
    if name == "listar_paginas":
        prefix = str(args.get("coleccion", "")).strip().strip("/")
        if not vault.valid_vault_path(prefix):
            return {"error": "colección no válida"}
        rows = conn.execute(
            "SELECT path, title FROM documents WHERE path = ? OR path LIKE ? "
            "ORDER BY path LIMIT 60",
            (prefix, prefix + "/%"),
        ).fetchall()
        return {"paginas": [{"path": r["path"], "title": r["title"] or r["path"]}
                            for r in rows]}
    if name == "eventos_calendario":
        try:
            start = (date.fromisoformat(str(args.get("desde"))) if args.get("desde")
                     else None)
            end = (date.fromisoformat(str(args.get("hasta"))) if args.get("hasta")
                   else None)
        except ValueError:
            return {"error": "fechas inválidas (usa YYYY-MM-DD)"}
        if start is None or end is None:
            d_start, d_end = _calendar_range("")
            start = start or d_start
            end = end or d_end
        events = []
        for row in _events_in_range(conn, start, end, cap=50):
            ev = row["ev"]
            events.append({
                "summary": ev["summary"], "start": _fmt_when(row["s"]),
                "end": _fmt_when(row["e"]) if row["e"] else None,
                "location": ev.get("location") or "",
            })
        return {"desde": str(start), "hasta": str(end), "eventos": events}
    return {"error": f"herramienta desconocida: {name}"}


def _calendar_source(conn, spec: str) -> dict:
    """Pseudo-document with the event list, fed to the LLM as context."""
    start, end = _calendar_range(spec)
    lines = []
    for row in _events_in_range(conn, start, end):
        ev, s, e = row["ev"], row["s"], row["e"]
        when = _fmt_when(s)
        if isinstance(s, datetime):
            if e and isinstance(e, datetime) and s.date() == e.date():
                when += f"–{e.strftime('%H:%M')}"
            elif e:
                when += f" → {_fmt_when(e)}"
        else:
            when += " (todo el día)"
            if e and row["e_date"] != row["s_date"]:
                when += f" → {_fmt_when(e)}"
        parts = [f"- {when} {ev['summary'] or '(sin título)'}"]
        if ev.get("location"):
            parts.append(f"en {ev['location']}")
        desc = (ev.get("description") or "").strip().replace("\n", " ")
        if desc:
            parts.append(f"— {desc[:120]}")
        lines.append(" ".join(parts))
    body = "\n".join(lines) if lines else "No hay eventos en ese rango."
    label = spec.strip() or "próximos 60 días"
    return {
        "path": "calendario",
        "title": f"Calendario ({label})",
        "body": f"Eventos del calendario entre {start} y {end}:\n{body}",
    }


def _conn_ctx(request, conn, user, flash: str = "", flash_ok: bool = False) -> dict:
    """Context for the LM Studio connection bar on the assistant page."""
    st = llm.status(conn)
    # Server reachable but no model chosen yet: pick the first loaded one
    # (or the first available) so the chat works out of the box.
    if st["ready"] and not st["model"] and st["models"]:
        model = next((m["id"] for m in st["models"] if m["loaded"]), st["models"][0]["id"])
        llm.save_config(conn, st["base"], model)
        st["model"] = model
    return base_ctx(
        request, conn, user,
        llm_base_url=st["base"],
        llm_model=st["model"],
        llm_reachable=st["reachable"],
        llm_models_detailed=st["models"],
        llm_model_loaded=st["model_loaded"],
        llm_ready=st["ready"],
        llm_flash=flash,
        llm_flash_ok=flash_ok,
        is_admin=user["role"] == "admin",
        can_manage=user["role"] in EDIT_ROLES,
    )


@router.get("/asistente")
async def assistant(request: Request, conn: ConnDep, user: UserDep):
    if user is None:
        return RedirectResponse("/login", status_code=303)
    active = request.query_params.get("chat", "")
    try:
        active_id = int(active) if active else None
    except ValueError:
        active_id = None
    ctx = _chat_shell_ctx(request, conn, user, active_id)
    ctx.update(_conn_ctx(request, conn, user))
    return templates.TemplateResponse(request, "asistente.html", ctx)


@router.get("/asistente/chat/mencionar")
async def chat_mentions(request: Request, conn: ConnDep, user: UserDep, q: str = ""):
    """@ autocomplete: documents matching the typed token."""
    if user is None:
        return JSONResponse({"items": []})
    q = q.strip().strip("/")
    items: list[dict] = []
    if q.lower().startswith("cal"):
        items.append({"path": "calendario", "title": "Calendario (eventos)",
                      "collection": "calendario"})
    if not q:
        # Bare "@": offer the most recently edited pages.
        for row in conn.execute(
            "SELECT path, title, collection FROM documents ORDER BY indexed_at DESC LIMIT 10"
        ):
            items.append({"path": row["path"], "title": row["title"] or row["path"],
                          "collection": row["collection"]})
    if q:
        for d in search_documents(conn, q, limit=8):
            items.append({"path": d["path"], "title": d["title"] or d["path"],
                          "collection": d["collection"]})
        for row in conn.execute(
            "SELECT path, title, collection FROM documents WHERE path LIKE ? ORDER BY path LIMIT 8",
            (q + "%",),
        ):
            if not any(i["path"] == row["path"] for i in items):
                items.append({"path": row["path"], "title": row["title"] or row["path"],
                              "collection": row["collection"]})
    return JSONResponse({"items": items[:10]})


@router.get("/asistente/eventos")
async def assistant_events(
    request: Request, conn: ConnDep, user: UserDep,
    desde: str = "", hasta: str = "",
):
    """Calendar events in a date range (YYYY-MM-DD), as JSON — the same
    list the chat feeds to the LLM via @calendario."""
    if user is None:
        return JSONResponse({"events": []})
    try:
        start = date.fromisoformat(desde) if desde else date.today() - timedelta(days=7)
        end = date.fromisoformat(hasta) if hasta else date.today() + timedelta(days=60)
    except ValueError:
        raise HTTPException(400, "Fechas inválidas (usa YYYY-MM-DD)") from None
    events = []
    for row in _events_in_range(conn, start, end, cap=200):
        ev = row["ev"]
        events.append({
            "id": ev["id"],
            "summary": ev["summary"],
            "start": _fmt_when(row["s"]),
            "end": _fmt_when(row["e"]) if row["e"] else None,
            "location": ev.get("location") or "",
            "description": (ev.get("description") or "")[:200],
        })
    return JSONResponse({"events": events, "desde": str(start), "hasta": str(end)})


@router.post("/asistente/chat/nuevo")
async def chat_new(request: Request, conn: ConnDep, user: UserDep):
    """Create an empty private chat session; returns the chat shell."""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    cur = conn.execute(
        "INSERT INTO chat_sessions (user_id, title) VALUES (?, 'Nuevo chat')",
        (user["id"],),
    )
    conn.commit()
    ctx = _chat_shell_ctx(request, conn, user, cur.lastrowid)
    return templates.TemplateResponse(request, "_chat_shell.html", ctx)


@router.get("/asistente/chat/{session_id:int}")
async def chat_open(request: Request, conn: ConnDep, user: UserDep, session_id: int):
    """Switch tab: render the shell with this session's messages."""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(f"/asistente?chat={session_id}", status_code=303)
    ctx = _chat_shell_ctx(request, conn, user, session_id)
    return templates.TemplateResponse(request, "_chat_shell.html", ctx)


@router.post("/asistente/chat/{session_id:int}/borrar")
async def chat_delete(request: Request, conn: ConnDep, user: UserDep, session_id: int):
    if user is None:
        return RedirectResponse("/login", status_code=303)
    _session_or_403(conn, user, session_id)
    conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
    conn.commit()
    ctx = _chat_shell_ctx(request, conn, user, None)
    return templates.TemplateResponse(request, "_chat_shell.html", ctx)


@router.post("/asistente/chat/{session_id:int}/renombrar")
async def chat_rename(
    request: Request,
    conn: ConnDep,
    user: UserDep,
    session_id: int,
    titulo: Annotated[str, Form()] = "",
    activo: Annotated[str, Form()] = "",
):
    """Renombrar un chat desde la lista. El título lo pone el usuario; el
    nombre inicial se infiere del primer mensaje (ver chat_send)."""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    _session_or_403(conn, user, session_id)
    nombre = " ".join(titulo.split())[:TITLE_MAX]
    if not nombre:
        raise HTTPException(status_code=400, detail="El nombre no puede estar vacío")
    # Solo cambia el nombre: updated_at intacto para no reordenar la lista.
    conn.execute("UPDATE chat_sessions SET title = ? WHERE id = ?", (nombre, session_id))
    conn.commit()
    # Renombrar una pestaña no debe cambiar el chat que se está viendo.
    abierto = session_id
    if activo.strip().isdigit():
        abierto = int(activo)
        _session_or_403(conn, user, abierto)
    ctx = _chat_shell_ctx(request, conn, user, abierto)
    return templates.TemplateResponse(request, "_chat_shell.html", ctx)


@router.post("/asistente/chat/mensaje")
async def chat_send(
    request: Request,
    conn: ConnDep,
    user: UserDep,
    session_id: Annotated[str, Form()] = "",
    texto: Annotated[str, Form()] = "",
):
    """Send a message: @mentions + FTS retrieval become context, the local
    model answers with the session history, both sides are stored.
    Without session_id a new private chat is created on the fly."""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    settings = get_settings(request)
    try:
        session_id = int(session_id) if session_id.strip() else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Sesión no válida") from None
    if session_id is None:
        cur = conn.execute(
            "INSERT INTO chat_sessions (user_id, title) VALUES (?, 'Nuevo chat')",
            (user["id"],),
        )
        conn.commit()
        session_id = cur.lastrowid
    session = _session_or_403(conn, user, session_id)
    texto = texto.strip()
    if not texto:
        raise HTTPException(status_code=400, detail="Mensaje vacío")

    mentions = re.findall(r"@([a-zA-Z0-9][\w./-]*)", texto)
    cal_specs = [
        m[len("calendario"):].strip("/")
        for m in mentions
        if m == "calendario" or m.startswith("calendario/")
    ]
    doc_tokens = [m for m in mentions
                  if not (m == "calendario" or m.startswith("calendario/"))]
    attached = _resolve_mentions(conn, settings, doc_tokens)
    plain = re.sub(r"@[a-zA-Z0-9][\w./-]*", " ", texto)
    rag = _sources_for(conn, settings, plain)
    sources: list[dict] = []
    seen: set[str] = set()
    per_coll: dict[str, int] = {}
    for s in attached + rag:
        coll = s["path"].split("/", 1)[0]
        if s["path"] in seen or per_coll.get(coll, 0) >= 3:  # diversity across collections
            continue
        seen.add(s["path"])
        per_coll[coll] = per_coll.get(coll, 0) + 1
        sources.append(s)
    for spec in cal_specs:
        sources.append(_calendar_source(conn, spec))
    if not cal_specs:
        spec = _spec_from_text(plain)
        if spec or CALENDAR_KEYWORDS.search(plain):
            sources.append(_calendar_source(conn, spec or ""))
    source_paths = [s["path"] for s in sources]

    history = conn.execute(
        "SELECT role, content FROM chat_messages WHERE session_id = ? ORDER BY id DESC LIMIT 12",
        (session_id,),
    ).fetchall()
    now = datetime.now()
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": (
            f"Fecha y hora actuales del laboratorio: {now.strftime('%Y-%m-%d %H:%M')} "
            f"({DIAS_ES[now.weekday()]}). Úsala para interpretar expresiones relativas "
            "del usuario como «hoy», «esta semana», «este mes» o «mañana»."
        )},
    ]
    if sources:
        context = "\n\n".join(f"### Página: {s['path']}\n{s['body']}" for s in sources)
        messages.append(
            {"role": "system", "content": f"Contexto de la documentación:\n\n{context}"}
        )
    for row in reversed(history):
        messages.append({"role": row["role"], "content": row["content"]})
    messages.append({"role": "user", "content": texto})

    # Tool-calling loop: the model can search/read the vault and the
    # calendar by itself; pages it reads become source chips too.
    answer, tool_trace = llm.chat_with_tools(
        conn, messages, TOOL_SCHEMAS,
        lambda name, args: _exec_tool(conn, settings, name, args),
    )
    for t in tool_trace:
        if t["name"] == "leer_pagina" and t.get("ok"):
            p = str(t.get("args", {}).get("path", ""))
            if p and p not in source_paths:
                source_paths.append(p)
    if answer is None:
        answer = (
            "El modelo no está disponible ahora mismo. Revisa que LM Studio "
            "esté encendido y con el modelo cargado (botón «Servidor» arriba)."
        )
    conn.execute(
        "INSERT INTO chat_messages (session_id, role, content, sources_json, tools_json) "
        "VALUES (?, 'user', ?, ?, '[]')",
        (session_id, texto, json.dumps(source_paths)),
    )
    conn.execute(
        "INSERT INTO chat_messages (session_id, role, content, sources_json, tools_json) "
        "VALUES (?, 'assistant', ?, ?, ?)",
        (session_id, answer, json.dumps(source_paths), json.dumps(tool_trace)),
    )
    if session["title"] in ("", "Nuevo chat"):
        conn.execute(
            "UPDATE chat_sessions SET title = ?, updated_at = datetime('now') WHERE id = ?",
            (_auto_title(texto), session_id),
        )
    else:
        conn.execute(
            "UPDATE chat_sessions SET updated_at = datetime('now') WHERE id = ?",
            (session_id,),
        )
    conn.commit()

    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request, "_chat_shell.html", _chat_shell_ctx(request, conn, user, session_id)
        )
    return RedirectResponse(f"/asistente?chat={session_id}", status_code=303)


@router.get("/asistente/conexion")
async def assistant_conn(request: Request, conn: ConnDep, user: UserDep):
    """Re-render the LM Studio connection bar (model states included)."""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request, "_assistant_conn.html", _conn_ctx(request, conn, user)
    )


@router.get("/asistente/conexion/form")
async def assistant_conn_form(request: Request, conn: ConnDep, user: UserDep):
    """Server settings form as a modal fragment (admin only)."""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user["role"] != "admin":
        raise HTTPException(status_code=403)
    base, model = llm.llm_config(conn)
    ctx = base_ctx(request, conn, user, llm_base_url=base, llm_model=model)
    return templates.TemplateResponse(request, "_assistant_conn_form.html", ctx)


@router.post("/asistente/conexion")
async def assistant_conn_save(
    request: Request, conn: ConnDep, user: UserDep,
    base_url: Annotated[str, Form()] = "",
):
    """Save the LLM server address (admin only; instance-wide setting)."""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user["role"] != "admin":
        raise HTTPException(status_code=403)
    _base, model = llm.llm_config(conn)
    llm.save_config(conn, base_url, model)
    if request.headers.get("HX-Request") == "true":
        # Posted from the floating modal: close it, toast, refresh the bar.
        msg = "Servidor guardado." if base_url.strip() else "Servidor eliminado."
        return HTMLResponse(
            '<span hidden data-toast="' + msg + '" data-toast-type="ok"></span>'
            '<div hidden hx-get="/asistente/conexion" hx-target="#assistant-conn"'
            ' hx-swap="outerHTML" hx-trigger="load"></div>'
            "<script>makerosCloseModal();</script>"
        )
    return RedirectResponse("/asistente", status_code=303)


@router.get("/asistente/modelos")
async def assistant_models(request: Request, conn: ConnDep, user: UserDep):
    """Live model list from the configured local server (any logged-in user)."""
    if user is None:
        return JSONResponse({"models": []})
    base, model = llm.llm_config(conn)
    reachable = llm.ping(base) if base else False
    detailed = llm.list_models_detailed(base) if base and reachable else []
    return JSONResponse(
        {
            "models": [m["id"] for m in detailed],
            "detailed": detailed,
            "model": model,
            "base_url": base,
            "reachable": reachable,
        }
    )


def _manage_fail(user) -> bool:
    return user["role"] not in EDIT_ROLES


@router.post("/asistente/modelo")
async def assistant_set_model(
    request: Request, conn: ConnDep, user: UserDep,
    modelo: Annotated[str, Form()],
):
    """Switch the assistant model (admin/member; instance-wide setting)."""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if _manage_fail(user):
        raise HTTPException(status_code=403)
    base, current = llm.llm_config(conn)
    new = modelo.strip()
    flash, ok = "Modelo seleccionado.", True
    if base and new and new != current:
        if current:
            llm.unload_model(base, current)  # free memory: best effort
        loaded, msg = llm.load_model(base, new)
        flash, ok = (msg, loaded)
    llm.save_config(conn, base, new)
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request, "_assistant_conn.html", _conn_ctx(request, conn, user, flash, ok)
        )
    return JSONResponse({"ok": True, "model": new})


@router.post("/asistente/modelo/cargar")
async def assistant_model_load(
    request: Request, conn: ConnDep, user: UserDep,
    modelo: Annotated[str, Form()],
):
    """Load a model into the LM Studio server memory and select it."""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if _manage_fail(user):
        raise HTTPException(status_code=403)
    base, _current = llm.llm_config(conn)
    if not base:
        raise HTTPException(status_code=400, detail="Configura primero el servidor LLM")
    ok, msg = llm.load_model(base, modelo.strip())
    if ok:
        llm.save_config(conn, base, modelo.strip())
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request, "_assistant_conn.html", _conn_ctx(request, conn, user, msg, ok)
        )
    return JSONResponse({"ok": ok, "detail": msg})


@router.post("/asistente/modelo/descargar")
async def assistant_model_unload(
    request: Request, conn: ConnDep, user: UserDep,
    modelo: Annotated[str, Form()],
):
    """Unload a model from the LM Studio server memory."""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if _manage_fail(user):
        raise HTTPException(status_code=403)
    base, _current = llm.llm_config(conn)
    if not base:
        raise HTTPException(status_code=400, detail="Configura primero el servidor LLM")
    ok, msg = llm.unload_model(base, modelo.strip())
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request, "_assistant_conn.html", _conn_ctx(request, conn, user, msg, ok)
        )
    return JSONResponse({"ok": ok, "detail": msg})


TASK_EXTRACT_PROMPT = (
    "De las notas de reunión que siguen, extrae SOLO las tareas accionables. "
    "Devuelve únicamente un checklist markdown, una línea por tarea, así: "
    "- [ ] Tarea concreta (máx. 8 palabras)\n"
    "Sin texto adicional, sin numeración, sin explicaciones. Si no hay tareas, responde: NINGUNA"
)


@router.get("/asistente/tareas/form")
async def assistant_tasks_form(request: Request, conn: ConnDep, user: UserDep):
    """Notes→tasks form as a modal fragment (from the chat "+" menu)."""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user["role"] not in EDIT_ROLES:
        raise HTTPException(status_code=403)
    return templates.TemplateResponse(
        request, "_assistant_tasks_form.html", base_ctx(request, conn, user)
    )


@router.post("/asistente/tareas")
async def assistant_tasks(
    request: Request, conn: ConnDep, user: UserDep,
    notas: Annotated[str, Form()],
):
    """Meeting notes -> task files: the local model extracts a checklist,
    each item becomes content/tasks/<slug>.md (status todo)."""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user["role"] not in EDIT_ROLES:
        raise HTTPException(status_code=403)
    settings = get_settings(request)
    notas = notas.strip()
    created: list[dict] = []
    error = ""
    if notas and llm.configured(conn):
        raw = llm.chat(
            conn,
            [
                {"role": "system", "content": TASK_EXTRACT_PROMPT},
                {"role": "user", "content": notas},
            ],
        )
        if raw is None:
            error = "El modelo no respondió; las tareas no se crearon."
        elif raw.strip() == "NINGUNA":
            error = "El modelo no encontró tareas accionables en el texto."
        else:
            import re as _re
            for line in raw.splitlines():
                m = _re.match(r"^\s*-\s*\[\s*[xX]?\s*\]\s*(.+)$", line)
                if not m:
                    continue
                title = m.group(1).strip().strip("*_`")
                if not title:
                    continue
                slug = _re.sub(r"[^a-zA-Z0-9-]", "-", title.lower()).strip("-")[:60] or "tarea"
                file = settings.content_dir / "tasks" / f"{slug}.md"
                n = 2
                while file.exists():
                    file = settings.content_dir / "tasks" / f"{slug}-{n}.md"
                    n += 1
                frontmatter.write(file, {"title": title, "status": "todo"}, "")
                index_file(conn, settings.content_dir, file)
                created.append(
                    {"title": title, "path": f"tasks/{file.stem}"}
                )
            if not created:
                error = "No se pudo crear ninguna tarea del texto."
    elif notas:
        error = "El asistente no tiene modelo configurado."
    return templates.TemplateResponse(
        request,
        "_assistant_tasks.html",
        base_ctx(request, conn, user, created=created, error=error),
    )


DIGEST_PROMPT = (
    "Eres el asistente de MakerOS, un laboratorio de prototipado. Escribe un "
    "digest semanal en español (máx. 250 palabras) sobre la actividad del lab: "
    "qué páginas se crearon o editaron y qué eventos hubo esta semana. Usa un "
    "título H1, secciones con H2 y viñetas; enlaza las páginas con [[ruta-sin-.md]]. "
    "Devuelve SOLO el markdown del digest."
)


@router.post("/asistente/digest")
async def assistant_digest(request: Request, conn: ConnDep, user: UserDep):
    """Weekly digest: summarize the last 7 days of edits + events with the
    local model and save it as content/wiki/reports/digest-<date>.md."""
    hx = request.headers.get("HX-Request") == "true"

    def hx_note(msg: str, ok: bool = True) -> HTMLResponse:
        t = "ok" if ok else "err"
        return HTMLResponse(f'<span hidden data-toast="{msg}" data-toast-type="{t}"></span>')

    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user["role"] not in EDIT_ROLES:
        raise HTTPException(status_code=403)
    settings = get_settings(request)
    if not llm.configured(conn):
        if hx:
            return hx_note("El asistente no tiene modelo configurado.", False)
        raise HTTPException(status_code=400, detail="El asistente no tiene modelo configurado.")
    from ...indexer import recent_documents
    from .. import event_store

    week_ago = date.today() - timedelta(days=7)
    docs = [
        d for d in recent_documents(conn, 60)
        if _day(d["indexed_at"]) and _day(d["indexed_at"]) >= week_ago
    ]
    events = [
        e for e in event_store.iter_events(conn)
        if _day(e["start"]) and week_ago <= _day(e["start"]) <= date.today() + timedelta(days=7)
    ]
    if not docs and not events:
        if hx:
            return hx_note("No hay actividad esta semana.", False)
        raise HTTPException(status_code=400, detail="No hay actividad esta semana.")
    context = "Páginas tocadas esta semana:\n" + "\n".join(
        f"- [{d['collection']}] {d['path']} ({d['indexed_at']})" for d in docs[:25]
    )
    context += "\n\nEventos de la semana:\n" + "\n".join(
        f"- {e['start']} — {e['summary']}" for e in events[:25]
    )
    digest = llm.chat(
        conn,
        [
            {"role": "system", "content": DIGEST_PROMPT},
            {"role": "user", "content": context},
        ],
    )
    if digest is None:
        if hx:
            return hx_note("El modelo no respondió.", False)
        raise HTTPException(status_code=400, detail="El modelo no respondió.")
    stamp = date.today().isoformat()
    reports = settings.content_dir / "wiki" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    file = reports / f"digest-{stamp}.md"
    n = 2
    while file.exists():
        file = reports / f"digest-{stamp}-{n}.md"
        n += 1
    frontmatter.write(
        file, {"title": f"Digest semanal — {stamp}", "tags": ["digest", "semanal"]}, digest
    )
    index_file(conn, settings.content_dir, file)
    if hx:
        return hx_note(f"Digest guardado en /wiki/reports/{file.stem}")
    return RedirectResponse(f"/wiki/reports/{file.stem}", status_code=303)


def _day(value) -> object | None:
    if value is None:
        return None
    if hasattr(value, "date"):
        return value.date() if isinstance(value, datetime) else value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


@router.post("/asistente/preguntar")
async def assistant_ask(
    request: Request,
    conn: ConnDep,
    user: UserDep,
    pregunta: Annotated[str, Form()],
):
    """Answer a question from the docs; returns the answer partial."""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    settings = get_settings(request)
    pregunta = pregunta.strip()
    sources: list[dict] = []
    answer = None
    offline_msg = ""
    if not pregunta:
        offline_msg = "Escribe una pregunta sobre la documentación."
    else:
        sources = _sources_for(conn, settings, pregunta)
        if not sources:
            offline_msg = "No encontré páginas relevantes para esa pregunta."
        else:
            context = "\n\n".join(
                f"### Página: {s['path']}\n{s['body']}" for s in sources
            )
            answer = llm.chat(
                conn,
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Contexto:\n\n{context}\n\nPregunta: {pregunta}"},
                ],
            )
            if answer is None:
                offline_msg = (
                    "El modelo no está disponible. Configura LM Studio en el "
                    "panel de administración (Integraciones) y asegúrate de que "
                    "el servidor local está encendido."
                )
    return templates.TemplateResponse(
        request,
        "_assistant_answer.html",
        base_ctx(
            request, conn, user,
            pregunta=pregunta,
            sources=sources,
            answer=answer,
            answer_html=render_markdown(answer) if answer else "",
            offline_msg=offline_msg,
        ),
    )


@router.post("/asistente/guardar")
async def assistant_save(
    request: Request,
    conn: ConnDep,
    user: UserDep,
    respuesta: Annotated[str, Form()],
    ruta: Annotated[str, Form()],
):
    """Save an assistant answer as a new wiki page."""
    if user is None or user["role"] not in EDIT_ROLES:
        raise HTTPException(status_code=403)
    settings = get_settings(request)
    ruta = ruta.strip().strip("/")
    if not vault.valid_vault_path(ruta):
        raise HTTPException(status_code=400, detail="ruta no válida")
    # Slug collision: fall back to -2, -3... like page creation does.
    candidate = ruta
    n = 1
    while (settings.content_dir / "wiki" / f"{candidate}.md").exists():
        n += 1
        candidate = f"{ruta}-{n}"
    file = settings.content_dir / "wiki" / f"{candidate}.md"
    frontmatter.write(file, {"title": candidate.rsplit("/", 1)[-1].replace("-", " ")}, respuesta)
    index_file(conn, settings.content_dir, file)
    return RedirectResponse(f"/wiki/{candidate}", status_code=303)
