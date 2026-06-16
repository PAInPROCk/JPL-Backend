from fastapi import APIRouter, Response, Request, HTTPException
import bcrypt

from core.database import get_db_connection
from auth.auth_handler import create_access_token, verify_token, get_token_from_request

router = APIRouter()

#------------LOGIN------------
@router.post("/login")
def login(data: dict, response: Response):
    try:
        from supabase import create_client
        import os
        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_KEY")
        supabase = create_client(supabase_url, supabase_key)
        
        # Authenticate user via Supabase Auth
        auth_response = supabase.auth.sign_in_with_password({
            "email": data["email"],
            "password": data["password"]
        })
        
        token = auth_response.session.access_token
        sb_user = auth_response.user
        
        # Get role and team_id from metadata
        role = sb_user.app_metadata.get("role", "team")
        team_id = sb_user.user_metadata.get("team_id")
        name = sb_user.user_metadata.get("name", "")
        
        # Fetch team details from Postgres
        conn = get_db_connection()
        team_purse = 0.0
        team_logo = None
        
        if conn:
            cursor = conn.cursor()
            try:
                if team_id:
                    cursor.execute("SELECT purse, image_path FROM teams WHERE team_id = %s", (int(team_id),))
                    team_row = cursor.fetchone()
                    if team_row:
                        team_purse = float(team_row["purse"]) if team_row["purse"] else 0.0
                        team_logo = team_row["image_path"]
            except Exception as dbe:
                print("DB team query error:", dbe)
            finally:
                cursor.close()
                conn.close()
        
        # Set HTTP-only Cookie for browser compatibility
        response.set_cookie(
            key="access_token",
            value=token,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=60 * 60 * 6
        )
        
        return {
            "message": "Login Successful",
            "token": token,
            "user": {
                "id": sb_user.id,
                "name": name,
                "role": role,
                "team_id": team_id,
                "team_purse": team_purse,
                "team_logo": team_logo
            }
        }
    except Exception as e:
        print("❌ Login error:", e)
        raise HTTPException(status_code=401, detail="Invalid Credentials or Login Failed")
        

#-------------LOGOUT-------------
@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("access_token")
    return{"message": "Logged Out"}

#-------------CHECK AUTH--------------
@router.get("/check-auth")
def check_auth(request: Request):
    token = get_token_from_request(request)
    if not token:
        return {"Aunthenticated": False}
    
    payload = verify_token(token)

    if not payload:
        return {"Aunthenticated": False}
    
    return{
        "authenticated": True,
        "user":payload
    }