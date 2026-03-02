from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from team_state import team_wallets
from auth import create_access_token, verify_token
from socket_server import socket_app, sio
from auction_engine import (
    start_auction,
    pause_auction,
    resume_auction,
    end_auction,
    emit_update,
    engine_place_bid
)
from auction_state import auction_state

import socketio
import bcrypt

app = FastAPI()

# ---------------- CORS ----------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- MOCK DATA ----------------
FAKE_USERS = {
    "admin1@example.com": {
        "id": 1,
        "email": "admin1@example.com",
        "password": bcrypt.hashpw(b"admin123", bcrypt.gensalt()).decode(),
        "role": "admin",
        "team_id": None,
        "team_name": None,
    },

    "team1@example.com": {
        "id": 2,
        "email": "team1@example.com",
        "password": bcrypt.hashpw(b"team123", bcrypt.gensalt()).decode(),
        "role": "team",
        "team_id": 101,
        "team_name": "Mumbai Warriors",
    },

    "team2@example.com": {
        "id": 3,
        "email": "team2@example.com",
        "password": bcrypt.hashpw(b"team123", bcrypt.gensalt()).decode(),
        "role": "team",
        "team_id": 102,
        "team_name": "Delhi Titans",
    }
}

MOCK_PLAYER = {
    "id": 1,
    "name": "MS Dhoni",
    "category": "Wicketkeeper",
    "type": "Right-hand",
    "jersey": 7,
    "base_price": 5000,
    "image_path": None
}

# ---------------- SOCKET EVENTS ----------------

@sio.event
async def connect(sid, environ):
    print("✅ Connected:", sid)

@sio.event
async def disconnect(sid):
    print("❌ Disconnected:", sid)

@sio.event
async def join_auction(sid, data):
    team_id = data["team_id"]

    # 🔥 Save wallet properly
    team_wallets[team_id] = {
        "team_name": data["team_name"],
        "purse": data.get("purse", 50000)  # default test purse
    }


    await sio.save_session(sid, {
        "user": {
            "team_id": data["team_id"],
            "team_name": data["team_name"],
            "role": "team"
        }
    })

    print("👤 Team joined:", data)
    await emit_update()

@sio.event
async def admin_join(sid, data):
    print("🛡 Admin joined")
    await emit_update()

@sio.event
async def place_bid(sid, data):

    print("💰 Bid attempt:", data)

    team = {
        "id": data["team_id"],
        "name": f"Team {data['team_id']}"
    }

    result = await engine_place_bid(team, data["bid_amount"])

    return result

# ---------------- REST API ----------------

@app.post("/login")
async def login(request: Request, response: Response):

    data = await request.json()
    email = data.get("email")
    password = data.get("password")

    user = FAKE_USERS.get(email)

    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not bcrypt.checkpw(password.encode(), user["password"].encode()):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({
        "id": user["id"],
        "email": user["email"],
        "role": user["role"],
        "team_id": user["team_id"],
        "team_name": user["team_name"],
    })

    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=60 * 60 * 6
    )

    return {
        "authenticated": True,
        "role": user["role"]
    }


@app.post("/logout")
async def logout(response: Response):
    response.delete_cookie(key="access_token")
    return {"message": "Logged out"}


@app.get("/check-auth")
async def check_auth(request: Request):
    token = request.cookies.get("access_token")

    if not token:
        return {"authenticated": False}

    payload = verify_token(token)

    if not payload:
        return {"authenticated": False}

    return {
        "authenticated": True,
        "user": payload,
        "role": payload.get("role")
    }


@app.get("/current-auction")
async def current_auction():
    if auction_state["status"] == "auction_active":
        return auction_state
    return {"status": "idle"}

@app.get("/auction-status")
async def auction_status():
    return {
        "active": auction_state["status"] == "auction_active"
    }

@app.post("/start-auction")
async def api_start():
    print("🚀 Auction started")
    await start_auction(MOCK_PLAYER)
    return {"status": "auction_started"}


@app.post("/pause-auction")
async def api_pause():
    print("⏸ Auction paused")
    await pause_auction()
    return {"status": "paused"}


@app.post("/resume-auction")
async def api_resume():
    print("▶ Auction resumed")
    await resume_auction()
    return {"status": "resumed"}


@app.post("/mark-sold")
async def api_mark_sold():

    if auction_state["status"] != "auction_active":
        return {"error": "No active auction"}

    if not auction_state["highest_bid"]:
        return {"error": "No bids available"}

    print("✅ Admin manually SOLD player")

    await end_auction()

    return {"success": True}


@app.post("/next-auction")
async def api_next_player():

    print("➡ Admin moving to next player")

    await start_auction(MOCK_PLAYER)

    return {"status": "auction_moved"}


# ---------------- ASGI APP ----------------
socket_app = socketio.ASGIApp(sio, other_asgi_app=app)