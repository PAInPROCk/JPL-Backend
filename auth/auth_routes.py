from fastapi import APIRouter, Response, Request, HTTPException
import bcrypt

from core.database import get_db_connection
from auth.auth_handler import create_access_token, verify_token

router = APIRouter()

#------------LOGIN------------
@router.post("/login")
def login(data: dict, response: Response):
    conn = get_db_connection()

    if conn is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    cursor = conn.cursor()

    cursor.execute("""
    SELECT u.id, u.name, u.email, u.password, u.role, u.team_id, t.purse, t.image_path, t.name AS team_name
    FROM users u
    LEFT JOIN teams t ON u.team_id = t.team_id
    WHERE u.email=%s
    """,(data["email"],))

    user = cursor.fetchone()

    cursor.close()
    conn.close()

    if not user:
        raise HTTPException(status_code=401, detail="Invalid Credentials")
    
    if not bcrypt.checkpw(data["password"].encode(), user["password"].encode()):
        raise HTTPException(status_code=401, detail="Invalid Credentials")
    
    token = create_access_token({
        "id": user["id"],
        "email":user["email"],
        "role": user["role"],
        "team_id": user["team_id"],
        "name": user["name"],
        "team_purse": float(user["purse"]) if user["purse"] else 0,
        "team_logo": user["image_path"]
    })

    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=60 * 60 * 6
    )

    return{
        "message": "Login Successful",
        "user": {
            "id": user["id"],
            "name": user["name"],
            "role": user["role"],
            "team_id": user["team_id"],
            "team_purse": float(user["purse"]) if user["purse"] else 0,
            "team_logo": user["image_path"]
        }
    }
        

#-------------LOGOUT-------------
@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("access_token")
    return{"message": "Logged Out"}

#-------------CHECK AUTH--------------
@router.get("/check-auth")
def check_auth(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        return {"Aunthenticated": False}
    
    payload = verify_token(token)

    if not payload:
        return {"Aunthenticated": False}
    
    return{
        "authenticated": True,
        "user":payload
    }