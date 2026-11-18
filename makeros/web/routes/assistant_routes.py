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

import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ... import frontmatter
from ...indexer import index_file, search_documents
from .. import llm, vault
from ..deps import ConnDep, UserDep, base_ctx, get_settings
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


def _sources_for(conn: sqlite3.Connection, settings, query: str) -> list[dict]:
    """FTS5 hits with a truncated body each — the RAG context.

    FTS5's implicit AND is too strict for natural questions, so an
    empty result falls back to an OR of the query's words.
    """
    hits = search_documents(conn, query, limit=MAX_SOURCES)
    if not hits:
        tokens = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
        if tokens:
            hits = search_documents(conn, " OR ".join(tokens), limit=MAX_SOURCES)
    sources: list[dict] = []
    for doc in hits:
        file = settings.content_dir / f"{doc['path']}.md"
        if not file.is_file():
            continue
        _meta, body = frontmatter.read(file)
        sources.append(
            {
                "path": doc["path"],
                "title": doc["title"] or doc["path"],
                "body": body.strip()[:MAX_CHUNK],
            }
        )
    return sources


@router.get("/asistente")
async def assistant(request: Request, conn: ConnDep, user: UserDep):
    if user is None:
        return RedirectResponse("/login", status_code=303)
    base, model = llm.llm_config(conn)
    return templates.TemplateResponse(
        request,
        "asistente.html",
        base_ctx(
            request, conn, user,
            llm_ready=llm.configured(conn),
            llm_base_url=base,
            llm_model=model,
            llm_models=llm.list_models(base) if base else [],
            is_admin=user["role"] == "admin",
        ),
    )


@router.get("/asistente/modelos")
async def assistant_models(request: Request, conn: ConnDep, user: UserDep):
    """Live model list from the configured local server (any logged-in user)."""
    if user is None:
        return JSONResponse({"models": []})
    base, model = llm.llm_config(conn)
    return JSONResponse(
        {"models": llm.list_models(base) if base else [], "model": model, "base_url": base}
    )


@router.post("/asistente/modelo")
async def assistant_set_model(
    request: Request, conn: ConnDep, user: UserDep,
    modelo: Annotated[str, Form()],
):
    """Switch the assistant model (admin only; instance-wide setting)."""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user["role"] != "admin":
        raise HTTPException(status_code=403)
    base, _current = llm.llm_config(conn)
    llm.save_config(conn, base, modelo)
    return JSONResponse({"ok": True, "model": modelo.strip()})


TASK_EXTRACT_PROMPT = (
    "De las notas de reunión que siguen, extrae SOLO las tareas accionables. "
    "Devuelve únicamente un checklist markdown, una línea por tarea, así: "
    "- [ ] Tarea concreta (máx. 8 palabras)\n"
    "Sin texto adicional, sin numeración, sin explicaciones. Si no hay tareas, responde: NINGUNA"
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
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user["role"] not in EDIT_ROLES:
        raise HTTPException(status_code=403)
    settings = get_settings(request)
    if not llm.configured(conn):
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
