from fastapi import APIRouter, UploadFile, File, HTTPException, Request
from auth.auth_handler import verify_token
from core.database import get_db_connection
import pymysql
import os
import uuid

router = APIRouter()

UPLOAD_FOLDER_TEAMS = "uploads/teams"

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

#---------- GET ALL TEAMS ------------
@router.get("/teams")
def get_teams():
    conn = get_db_connection()
    if conn is None:
       return{"error": "Database connection failed"}
    
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        cursor.execute("""
            SELECT 
                team_id,
                name,
                Total_Budget,
                Season_Budget,
                image_path
            FROM teams
            ORDER BY name ASC
        """)

        teams = cursor.fetchall()

        return{
            "success": True,
            "count": len(teams),
            "teams": teams
        }
    
    except Exception as e:
        print("Team route error:", e)
        return{"error": str(e)}

    finally:
        cursor.close()
        conn.close()

#---------- GET TEAM SQUAD -----------
@router.get("/team/{team_id}")
def get_teams(team_id: int):
    conn = get_db_connection()

    if conn is None:
        return{"error": "Database connection failed"}

    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        cursor.execute("""
            SELECT
                p.id AS player_id,
                p.name,
                p.category,
                p.type,
                sp.sold_price,
                sp.sold_time
            FROM sold_players sp
            JOIN players p ON sp.player_id = p.id
            WHERE sp.team_id = %s
            ORDER BY sp.sold_time ASC       
        """, (team_id,))

        squad = cursor.fetchall()

        return{
            "success": True,
            "team_id": team_id,
            "players": squad
        }
    
    except Exception as e:
        print("Team route error:", e)
        return{"error": str(e)}
    
    finally:
        cursor.close()
        conn.close()

@router.post("/upload-team-image")
async def upload_team_image(
    request: Request,
    image: UploadFile = File(...)
):

    token = request.cookies.get("access_token")

    payload = verify_token(token)

    if not payload or payload.get("role") != "admin":
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not image.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    ext = image.filename.split(".")[-1].lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Invalid file type")

    filename = f"{uuid.uuid4().hex}.{ext}"

    filepath = os.path.join(UPLOAD_FOLDER_TEAMS, filename)

    with open(filepath, "wb") as buffer:
        buffer.write(await image.read())

    return {
        "image_path": f"uploads/teams/{filename}"
    }