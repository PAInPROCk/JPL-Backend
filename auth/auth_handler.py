from datetime import datetime, timedelta
from jose import jwt, JWTError

SECRET_KEY = "JPL_SECRET_KEY"
ALGORITHM ="HS256"
ACCESS_TOKEN_EXPIRE_HOUR = 6

def create_access_token(data: dict):
    to_encode = data.copy()

    expire = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOUR)
    to_encode.update({"exp":expire})

    token = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

    return token

def verify_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=ALGORITHM)
        return payload
    
    except JWTError:
        return None
    
def get_token_from_request(request):
    # 1️⃣ Check Authorization header (Android)
    auth_header = request.headers.get("Authorization")

    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.split(" ")[1]

    # 2️⃣ Fallback to Cookie (Web)
    return request.cookies.get("access_token")