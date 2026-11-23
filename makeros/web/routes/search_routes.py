"""Full-text search over the whole content index."""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ...indexer import search_documents
from ..deps import ConnDep, UserDep, base_ctx
from ..templates import templates

router = APIRouter()

# FTS snippets come from the raw Markdown body; heading markers and fence
# leftovers only add noise in the result list.
_MARKUP = re.compile(r"(^|\s)(?:#{1,6}\s+|>\s+|```\w*)")


def _clean_snippet(text: str) -> str:
    """Readable snippet: drop Markdown markers FTS left in the excerpt."""
    cleaned = _MARKUP.sub(r"\1", text or "")
    return re.sub(r"\s+", " ", cleaned).strip()


@router.get("/search.json")
async def search_json(request: Request, conn: ConnDep, user: UserDep, q: str = ""):
    """JSON results for the Ctrl+K command palette."""
    if user is None:
        return JSONResponse({"results": []})
    results = search_documents(conn, q) if q.strip() else []
    return JSONResponse(
        {
            "results": [
                {
                    "title": r["title"] or r["path"],
                    "path": r["path"],
                    "collection": r["collection"],
                }
                for r in results[:12]
            ]
        }
    )


@router.get("/search")
async def search(request: Request, conn: ConnDep, user: UserDep, q: str = ""):
    if user is None:
        return RedirectResponse("/login", status_code=303)
    results = search_documents(conn, q) if q.strip() else []
    rows = [
        {
            "path": r["path"],
            "title": r["title"],
            "collection": r["collection"],
            "snippet": _clean_snippet(r["snippet"]),
        }
        for r in results
    ]
    return templates.TemplateResponse(
        request, "search.html", base_ctx(request, conn, user, q=q, results=rows)
    )
