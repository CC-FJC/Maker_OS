"""Full-text search over the whole content index."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ...indexer import search_documents
from ..deps import ConnDep, UserDep, base_ctx
from ..templates import templates

router = APIRouter()


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
    return templates.TemplateResponse(
        request, "search.html", base_ctx(request, conn, user, q=q, results=results)
    )
