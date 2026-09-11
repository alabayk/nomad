from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import current_user
from app.database import get_db
from app.models import Friendship, Memory, User, VisitedCountry
from pathlib import Path

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def _hidden(request: Request, user: User | None) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="page_not_found.html", context={"user": user}, status_code=404)


@router.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, q: str = "", db: Session = Depends(get_db)) -> HTMLResponse:
    user = current_user(db, request)
    if user is None or not user.is_admin:
        return _hidden(request, user)

    query = select(User).order_by(User.created_at.desc(), User.id.desc()).limit(100)
    search = q.strip()
    if search:
        pattern = f"%{search.lower()}%"
        query = query.where(or_(func.lower(User.username).like(pattern), func.lower(User.full_name).like(pattern)))
    users = list(db.scalars(query))
    ids = [item.id for item in users]
    memory_counts = dict(db.execute(select(Memory.user_id, func.count(Memory.id)).where(Memory.user_id.in_(ids)).group_by(Memory.user_id)).all()) if ids else {}
    totals = {
        "users": db.scalar(select(func.count(User.id))) or 0,
        "memories": db.scalar(select(func.count(Memory.id))) or 0,
        "countries": db.scalar(select(func.count(VisitedCountry.id))) or 0,
        "friendships": db.scalar(select(func.count(Friendship.id)).where(Friendship.status == "accepted")) or 0,
    }
    response = templates.TemplateResponse(request=request, name="admin.html", context={
        "user": user, "users": users, "memory_counts": memory_counts, "totals": totals, "query": search,
    })
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response
