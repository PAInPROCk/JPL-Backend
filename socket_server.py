import socketio
from socket_manager import sio
from auth import verify_token
from auction_engine import (
    auction_state,
    engine_place_bid
)


async def get_user_from_cookie(environ):
    cookie = environ.get("HTTP_COOKIE", "")
    cookies = dict(
        item.split("=") for item in cookie.split("; ") if "=" in item
    )
    token = cookies.get("access_token")
    if not token:
        return None
    return verify_token(token)

connected_admins = set()
connected_teams = set()

@sio.event
async def connect(sid, environ):
    print("✅ Socket connected:", sid)

@sio.event
async def disconnect(sid):
    session = await sio.get_session(sid)
    connected_admins.discard(sid)
    connected_teams.discard(sid)
    print("🔌 Socket disconnected:", session.get("user"))

# ---------------- TEAM JOIN ----------------
@sio.event
async def join_auction(sid, data):
    session = await sio.get_session(sid)
    user = session.get("user")

    print(f"🏏 Team joined auction: {user['email']}")

    await sio.emit("auction_update", auction_state, to=sid)

# ---------------- ADMIN JOIN ----------------
@sio.event
async def admin_join(sid, data):
    session = await sio.get_session(sid)
    user = session.get("user")

    if user["role"] != "admin":
        print("❌ Non-admin tried admin_join")
        return

    print(f"👑 Admin joined: {user['email']}")

    await sio.emit("auction_update", auction_state, to=sid)

# ---------------- PLACE BID ----------------
@sio.event
async def place_bid(sid, data):

    session = await sio.get_session(sid)
    user = session.get("user")

    if auction_state["paused"]:
        return {"error": "Auction paused"}

    bid_amount = data.get("bid_amount")

    team = {
        "id": user.get("team_id"),
        "name": user.get("team_name")
    }

    result = await engine_place_bid(team, bid_amount)

    if result.get("error"):
        return result

    return {"success": True}

socket_app = socketio.ASGIApp(sio)
