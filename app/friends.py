from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.auth import csrf_token_for_request, current_user, set_csrf_cookie, valid_csrf_token
from app.database import get_db
from app.models import Friendship, User
from pathlib import Path

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def _login(): return RedirectResponse("/login", status_code=303)


def _connection(db: Session, first: int, second: int):
    return db.scalar(select(Friendship).where(or_(and_(Friendship.requester_id == first, Friendship.addressee_id == second), and_(Friendship.requester_id == second, Friendship.addressee_id == first))))


@router.get("/friends", response_class=HTMLResponse)
def friends_page(request: Request, q: str = "", db: Session = Depends(get_db)):
    user = current_user(db, request)
    if not user: return _login()
    connections = list(db.scalars(select(Friendship).where(or_(Friendship.requester_id == user.id, Friendship.addressee_id == user.id)).order_by(Friendship.created_at.desc())))
    users = {item.id: item for item in db.scalars(select(User).where(User.id.in_({value for item in connections for value in (item.requester_id, item.addressee_id)})))} if connections else {}
    friends, incoming, outgoing = [], [], []
    for item in connections:
        peer = users.get(item.addressee_id if item.requester_id == user.id else item.requester_id)
        if not peer: continue
        pair = {"connection": item, "peer": peer}
        if item.status == "accepted": friends.append(pair)
        elif item.addressee_id == user.id: incoming.append(pair)
        else: outgoing.append(pair)
    query = q.strip().lower(); result = db.scalar(select(User).where(User.username == query)) if query and query != user.username else None
    result_connection = _connection(db, user.id, result.id) if result else None
    csrf = csrf_token_for_request(request)
    response = templates.TemplateResponse(request=request, name="friends.html", context={"user": user, "friends": friends, "incoming": incoming, "outgoing": outgoing, "query": q.strip(), "result": result, "result_connection": result_connection, "csrf_token": csrf})
    set_csrf_cookie(response, csrf); return response


@router.post("/friends/request")
def send_request(request: Request, username: str = Form(...), csrf_token: str = Form(...), db: Session = Depends(get_db)):
    user = current_user(db, request)
    if not user or not valid_csrf_token(request, csrf_token): return _login() if not user else RedirectResponse("/friends", status_code=303)
    target = db.scalar(select(User).where(User.username == username.strip().lower()))
    if target and target.id != user.id:
        existing = _connection(db, user.id, target.id)
        if existing is None: db.add(Friendship(requester_id=user.id, addressee_id=target.id)); db.commit()
        elif existing.status == "pending" and existing.requester_id == target.id: existing.status = "accepted"; db.commit()
    return RedirectResponse("/friends", status_code=303)


def _act(connection_id: int, action: str, request: Request, csrf_token: str, db: Session):
    user = current_user(db, request)
    if not user or not valid_csrf_token(request, csrf_token): return _login() if not user else RedirectResponse("/friends", status_code=303)
    item = db.get(Friendship, connection_id)
    allowed = item and user.id in (item.requester_id, item.addressee_id)
    if allowed and action == "accept" and item.status == "pending" and item.addressee_id == user.id: item.status = "accepted"; db.commit()
    elif allowed and action in {"decline", "cancel", "remove"}: db.delete(item); db.commit()
    return RedirectResponse("/friends", status_code=303)


@router.post("/friends/{connection_id}/{action}")
def friend_action(connection_id: int, action: str, request: Request, csrf_token: str = Form(...), db: Session = Depends(get_db)):
    if action not in {"accept", "decline", "cancel", "remove"}: return RedirectResponse("/friends", status_code=303)
    return _act(connection_id, action, request, csrf_token, db)
