from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from auth import create_access_token, verify_token
import mysql.connector
import bcrypt
import json
import os

app = FastAPI()

# ---------------- CORS ----------------
FRONTEND_PORT = "3000"

origins = [
    f"http://localhost:{FRONTEND_PORT}",
    f"http://127.0.0.1:{FRONTEND_PORT}",
    "http://192.168.29.135:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
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


@app.post("/login")
async def login(request: Request, response: Response):
    data = await request.json()
    email = data.get("email")
    password = data.get("password")

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
# ---------------- Models ----------------
class LoginRequest(BaseModel):
    email: str
    password: str

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
