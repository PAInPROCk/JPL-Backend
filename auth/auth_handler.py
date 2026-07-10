from datetime import datetime, timedelta
from jose import jwt, JWTError

import os
from dotenv import load_dotenv

# Load env variables
load_dotenv()

SECRET_KEY = os.getenv("SUPABASE_JWT_SECRET", "JPL_SECRET_KEY")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOUR = 6

def create_access_token(data: dict):
    to_encode = data.copy()

    expire = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOUR)
    to_encode.update({"exp":expire})

    token = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

    return token

def verify_token(token: str):
    if not token:
        return None
        
    try:
        from supabase import create_client
        import os
        
        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_KEY")
        
        if supabase_key and supabase_key.startswith("sb_"):
            # Use the full JWT anon key if the publishable key starts with sb_
            supabase_key = os.getenv("SUPABASE_ANON_KEY")
            
        supabase = create_client(supabase_url, supabase_key)
        
        # Verify the token via Supabase Auth API
        res = supabase.auth.get_user(token)
        user = res.user
        
        if not user:
            return None
            
        return {
            "id": user.id,
            "email": user.email,
            "role": user.app_metadata.get("role", "team") if user.app_metadata else "team",
            "team_id": user.user_metadata.get("team_id") if user.user_metadata else None,
            "name": user.user_metadata.get("name", "") if user.user_metadata else ""
        }
    
    except Exception as e:
        print("❌ Token verification failed via Supabase API:", e)
        return None
    
def get_token_from_request(request):
    # 1️⃣ Check Authorization header (Android)
    auth_header = request.headers.get("Authorization")

    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.split(" ")[1]

    # 2️⃣ Fallback to Cookie (Web)
    return request.cookies.get("access_token")