"""Shared request dependencies: DB connection, settings, current user."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, Request

from ..auth import get_user
from ..collections import COLLECTIONS, STATUS_HELP, STATUS_LABELS, TASK_STATUSES
from ..config import Settings
from ..db import connect
from ..indexer import collection_counts, list_collection
from ..ui import collection_icons, get_ui, nav_items, theme_accent, theme_bg
from . import event_store
from .forms import TASK_PRIORITIES
from .icons import icon_names
from .listing import todays_events


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """One SQLite connection per request, closed on completion."""
    conn = connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


# Annotated aliases keep route signatures readable and satisfy lint
# (no function call in argument defaults).
ConnDep = Annotated[sqlite3.Connection, Depends(get_conn)]


async def current_user(request: Request, conn: ConnDep):
    """The logged-in user row, or None (SSR routes redirect themselves)."""
    uid = request.session.get("uid")
    if uid is None:
        return None
    return get_user(conn, uid)


UserDep = Annotated[sqlite3.Row | None, Depends(current_user)]


DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
           "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def ahora_es() -> tuple[str, str]:
    """(texto legible en español, ISO) de la hora actual del servidor."""
    parts = ahora_partes()
    return f"{parts['date_full']} · {parts['time']}", parts["iso"]


def ahora_partes() -> dict[str, str]:
    """Hora del servidor desglosada: hora (grande) + fecha (secundaria)."""
    now = datetime.now()
    fecha = f"{DIAS_ES[now.weekday()]} {now.day} de {MESES_ES[now.month - 1]}"
    return {
        "time": now.strftime("%H:%M"),
        "date": fecha,
        "date_full": f"{fecha} de {now.year}",
        "iso": now.strftime("%Y-%m-%d %H:%M"),
    }


def base_ctx(
    request: Request, conn: sqlite3.Connection, user: sqlite3.Row | None, **extra: Any
) -> dict[str, Any]:
    """Common template context: user, collection nav with live counts + icons."""
    counts = {row["collection"]: row["n"] for row in collection_counts(conn)}
    # Calendar events are SQLite rows now, not Markdown documents.
    counts["calendario"] = event_store.event_count(conn)
    # Sidebar entries in this user's saved order; collections carry counts.
    items = nav_items(conn, user)
    for item in items:
        item["count"] = counts.get(item["id"], 0) if item["id"] in COLLECTIONS else 0
    reloj = ahora_partes()
    ctx: dict[str, Any] = {
        "user": user,
        "counts": counts,
        "collections": COLLECTIONS,
        "nav_entries": items,
        "nav_icons": collection_icons(conn),
        "icon_list": icon_names(),
        "today_events": todays_events(conn),
        "status_labels": STATUS_LABELS,
        "status_help": STATUS_HELP,
        "task_status_labels": TASK_STATUSES,
        "priority_labels": TASK_PRIORITIES,
        "site_name": get_ui(conn, "site:name", "Makerspace"),
        "theme_accent": theme_accent(conn),
        "theme_bg": theme_bg(conn),
        # Event categories (id -> label/color) for chips and forms.
        "cat_colors": {c["id"]: c["color"] for c in event_store.list_categories(conn)},
        "cat_labels": {c["id"]: c["label"] for c in event_store.list_categories(conn)},
        # People for assignment selects (tasks, projects, events).
        "people": [
            {"slug": doc["path"][len("people/"):], "title": doc["title"] or doc["path"]}
            for doc in list_collection(conn, "people")
        ],
        # ?panel=1 renders a page without the sidebar (used by /dividir panes).
        "panel": request.query_params.get("panel") == "1",
        # Server clock: hora en primer plano, fecha como dato secundario.
        "now_time": reloj["time"],
        "now_date": reloj["date"],
        "now_es": f"{reloj['date_full']} · {reloj['time']}",
        "now_iso": reloj["iso"],
    }
    ctx.update(extra)
    return ctx
