import re
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import Friendship


def csrf(response):
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text); assert match; return match.group(1)


def register(client, username):
    page = client.get("/register")
    response = client.post("/register", data={"username": username, "full_name": username.title(), "password": "a-secure-test-password", "password_confirm": "a-secure-test-password", "csrf_token": csrf(page)}, follow_redirects=False)
    assert response.status_code == 303


def test_friend_request_accept_remove_and_no_duplicates(test_session_factory):
    with TestClient(app) as alice, TestClient(app) as bob:
        register(alice, "friend_alice"); register(bob, "friend_bob")
        page = alice.get("/friends?q=friend_bob")
        sent = alice.post("/friends/request", data={"username": "friend_bob", "csrf_token": csrf(page)}, follow_redirects=False)
        assert sent.status_code == 303
        page = alice.get("/friends")
        alice.post("/friends/request", data={"username": "friend_bob", "csrf_token": csrf(page)})
        with test_session_factory() as db:
            items = list(db.scalars(select(Friendship))); assert len(items) == 1; connection_id = items[0].id
        inbox = bob.get("/friends"); assert "Friend_Alice" in inbox.text
        accepted = bob.post(f"/friends/{connection_id}/accept", data={"csrf_token": csrf(inbox)}, follow_redirects=False)
        assert accepted.status_code == 303 and "Friend_Bob" in alice.get("/friends").text
        profile = alice.get("/friends/friend_bob")
        assert profile.status_code == 200 and "ПРОФИЛЬ ДРУГА" in profile.text
        assert alice.get("/friends/friend_alice").status_code == 404
        with TestClient(app) as stranger:
            register(stranger, "friend_stranger")
            assert stranger.get("/friends/friend_bob").status_code == 404
        page = alice.get("/friends")
        alice.post(f"/friends/{connection_id}/remove", data={"csrf_token": csrf(page)})
        with test_session_factory() as db: assert db.get(Friendship, connection_id) is None
