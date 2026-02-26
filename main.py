from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from auth import create_access_token, verify_token
from socket_server import socket_app, sio
from auction_engine import *
from auction_state import auction_state
# from flask_socketio import 
import socketio
import mysql.connector
import bcrypt
import json
import os

# ---------------- Models ----------------
class LoginRequest(BaseModel):
    email: str
    password: str

app = FastAPI()


# ✅ Database connection
def get_db_connection():
    return mysql.connector.connect(
        host="localhost",
        user="root",
        password="",   # XAMPP default (empty) – change if you set a password
        database="jpl"
    )

# ---------------- CORS ----------------
FRONTEND_PORT = "3000"

origins = [
    f"http://localhost:{FRONTEND_PORT}",
    f"http://127.0.0.1:{FRONTEND_PORT}",
    "http://192.168.29.135:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# MOCK: replace with DB
FAKE_USER = {
    "id": 1,
    "email": "admin1@example.com",
    "password": bcrypt.hashpw(b"admin123", bcrypt.gensalt()).decode(),
    "role": "admin"
}

# MOCK PLAYER (replace with DB later)
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

    result = await place_bid(team, data["bid_amount"])
    return result

# ---------------- REST API ----------------

@app.post("/login")
async def login(data: LoginRequest, response: Response):

    email = data.email
    password = data.password

    if email != FAKE_USER["email"]:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not bcrypt.checkpw(password.encode(), FAKE_USER["password"].encode()):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({
        "id": FAKE_USER["id"],
        "email": FAKE_USER["email"],
        "role": FAKE_USER["role"],
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
        "role": FAKE_USER["role"]
    }

@app.post("/logout")
async def logout(response: Response):
    response.delete_cookie(
        key="access_token",
        samesite="lax",
        secure=False
    )
    return {"message": "Logged out"}


# # ---------------- LOGIN ----------------
# @app.post("/login")
# def login(data: LoginRequest, response: Response):
#     email = data.email
#     password = data.password

#     conn = get_db_connection()
#     cursor = conn.cursor(dictionary=True)

#     cursor.execute("SELECT * FROM users WHERE email=%s", (email,))
#     user = cursor.fetchone()

#     cursor.close()
#     conn.close()

#     if not user:
#         raise HTTPException(status_code=404, detail="User not found")

#     if not bcrypt.checkpw(password.encode(), user["password"].encode()):
#         raise HTTPException(status_code=401, detail="Invalid credentials")

#     # ---- Build session-like user object ----
#     session_user = {
#         "id": user["id"],
#         "email": user["email"],
#         "role": user["role"],
#         "team_id": user.get("team_id"),
#     }

#     # ---- Store in cookie (HTTP-only) ----
#     response.set_cookie(
#         key="user",
#         value=json.dumps(session_user),
#         httponly=True,
#         samesite="lax",
#         max_age=60 * 60 * 2
#     )

#     return {
#         "authenticated": True,
#         "message": "Login successful",
#         "role": user["role"],
#         "user": session_user
#     }

# ---------------- CHECK AUTH ----------------
def get_current_user(request: Request):
    token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = verify_token(token)

    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")

    return payload


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

@app.get("/db-test")
async def db_test():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT 1")
    cursor.fetchone()

    cursor.close()
    conn.close()

    return {"db": "connected"}


@app.get("/players")
async def get_players(user=Depends(get_current_user)):

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT 
            p.id AS player_id, 
            p.name, 
            p.nickname, 
            p.jersey, 
            p.category, 
            p.type,
            p.image_path, 
            p.base_price, 
            p.total_runs, 
            p.highest_runs, 
            p.wickets_taken, 
            p.times_out, 
            GROUP_CONCAT(t.name SEPARATOR ', ') AS teams_played
        FROM players p
        LEFT JOIN player_teams pt ON p.id = pt.player_id
        LEFT JOIN teams t ON pt.team_id = t.team_id   -- ✅ FIXED HERE
        GROUP BY p.id
        ORDER BY p.name ASC
    """)
    rows = cursor.fetchall()

    cursor.close()
    conn.close()

    return {
        "success": True,
        "count": len(rows),
        "players": rows
    }

@app.get("/players-with-teams")
async def players_with_teams(user=Depends(get_current_user)):

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT 
            p.id AS player_id,
            p.name,
            p.nickname,
            p.jersey,
            p.category,
            p.type,
            p.image_path,
            p.base_price,
            GROUP_CONCAT(t.name SEPARATOR ', ') AS teams_played
        FROM players p
        LEFT JOIN player_teams pt ON p.id = pt.player_id
        LEFT JOIN teams t ON pt.team_id = t.team_id
        GROUP BY p.id
        ORDER BY p.name ASC
    """)

    rows = cursor.fetchall()

    cursor.close()
    conn.close()

    return rows

@app.get("/teams")
async def get_teams(user=Depends(get_current_user)):

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM teams")
    rows = cursor.fetchall()

    cursor.close()
    conn.close()

    return {
    "success": True,
    "count": len(rows),
    "teams": rows
    }

@app.get("/team/{team_id}")
async def get_team_by_id(
    team_id: int,
    user=Depends(get_current_user)
):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # ✅ Get team
        cursor.execute(
            "SELECT * FROM teams WHERE team_id = %s",
            (team_id,)
        )
        team = cursor.fetchone()

        if not team:
            raise HTTPException(
                status_code=404,
                detail="Team not found"
            )

        # ✅ Get sold players (OLD APP LOGIC)
        cursor.execute("""
            SELECT p.*, sp.sold_price, sp.sold_time
            FROM sold_players sp
            JOIN players p ON sp.player_id = p.id
            WHERE sp.team_id = %s
        """, (team_id,))

        sold_players = cursor.fetchall()

        return {
            "team": team,
            "sold_players": sold_players
        }

    finally:
        cursor.close()
        conn.close()

@app.get("/current-auction")
async def current_auction():
    if auction_state["status"] == "auction_active":
        return auction_state
    return {"status": "idle"}


@app.post("/start-auction")
async def api_start():
    await start_auction(MOCK_PLAYER)
    return {"status": "auction_started"}


@app.post("/pause-auction")
async def api_pause():
    await pause_auction()
    return {"status": "paused"}


@app.post("/resume-auction")
async def api_resume():
    await resume_auction()
    return {"status": "resumed"}

@sio.event
async def start_auction(sid):
    session = await sio.get_session(sid)
    user = session.get("user")

    if user["role"] != "admin":
        await sio.emit("error", {"msg": "Unauthorized"}, to=sid)
        return

    await sio.emit("auction_started", {"by": user["email"]})

# MOCK PLAYERS LIST (temporary until DB added)
MOCK_PLAYERS = [
    {
        "id": 1,
        "name": "MS Dhoni",
        "category": "Wicketkeeper",
        "type": "Right-hand",
        "base_price": 5000
    },
    {
        "id": 2,
        "name": "Virat Kohli",
        "category": "Batsman",
        "type": "Right-hand",
        "base_price": 6000
    }
]

# MOCK TEAMS LIST
MOCK_TEAMS = [
    {
        "team_id": 1,
        "name": "Mumbai Warriors",
        "purse": 500000
    },
    {
        "team_id": 2,
        "name": "Delhi Titans",
        "purse": 480000
    }
]


socket_app = socketio.ASGIApp(sio, other_asgi_app=app)