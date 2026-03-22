from fastapi import APIRouter, UploadFile, File, HTTPException, Request, Form
from auth.auth_handler import verify_token
from core.database import get_db_connection
import pymysql
import os
import uuid

from typing import List, Optional


router = APIRouter()

UPLOAD_FOLDER_PLAYERS = "uploads/players"

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

@router.get("/players")
def get_players():
    conn = get_db_connection()

    if conn is None:
        return{"error": "Database Connection Failed"}
    
    try:
        cursor  = conn.cursor(pymysql.cursors.DictCursor)

        cursor.execute(
            """SELECT 
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
                    COALESCE(GROUP_CONCAT(DISTINCT t.name ORDER BY t.name SEPARATOR ', '), '') AS teams_played
                FROM players p
                LEFT JOIN player_teams pt 
                    ON p.id = pt.player_id
                LEFT JOIN teams t 
                    ON pt.team_id = t.team_id
                GROUP BY 
                    p.id,
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
                    p.times_out
                ORDER BY p.name;"""
        )

        rows = cursor.fetchall()

        return{
            "success": True,
            "count": len(rows),
            "players": rows
        }
    
    except Exception as e:
        print("Player route error:", e)
        return{"error": str(e)}
    
    finally:
        cursor.close()
        conn.close()

@router.get("/player-with-teams")
def players_with_teams():

    conn = get_db_connection()

    if conn is None:
        return {"error":"Database Connection Failed"}
    
    try:
        cursor = conn.cursor()

        cursor.execute(
            """
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
            """
        )

        rows = cursor.fetchall()

        return{
            "success": True,
            "count": len(rows),
            "players": rows
        }
    
    except Exception as e:
        print("player-with-teams route error:", e)
        return{"error": str(e)}
    
    finally:
        cursor.close()
        conn.close()
        

@router.post("/upload-player-image")
async def upload_player_image(
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

    filepath = os.path.join(UPLOAD_FOLDER_PLAYERS, filename)

    with open(filepath, "wb") as buffer:
        buffer.write(await image.read())

    return {
        "image_path": f"uploads/players/{filename}"
    }




@router.post("/add-player")
async def add_player(
    request: Request,

    # -------- FORM FIELDS --------
    playerName: Optional[str] = Form(None),
    fatherName: Optional[str] = Form(None),
    surName: Optional[str] = Form(None),
    nickName: Optional[str] = Form(None),
    age: Optional[int] = Form(None),
    category: Optional[str] = Form(None),
    style: Optional[str] = Form(None),
    basePrice: Optional[float] = Form(0),
    totalRuns: Optional[int] = Form(0),
    highestRuns: Optional[int] = Form(0),
    wickets: Optional[int] = Form(0),
    outs: Optional[int] = Form(0),
    jerseyNo: Optional[int] = Form(None),
    mobile: Optional[str] = Form(None),
    emailId: Optional[str] = Form(None),
    gender: Optional[str] = Form(None),

    # -------- MULTI-SELECT TEAMS --------
    teams: Optional[List[int]] = Form([]),

    # -------- FILE --------
    image: Optional[UploadFile] = File(None)
):
    # ================= AUTH =================
    token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user = verify_token(token)

    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    # ================= NAME =================
    full_name = " ".join(
        part for part in [playerName, fatherName, surName] if part
    ).strip()

    if not full_name:
        raise HTTPException(status_code=400, detail="Player full name is required")

    # ================= IMAGE UPLOAD =================
    image_path = None

    if image:
        ext = image.filename.split(".")[-1].lower()

        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail="Invalid image format")

        os.makedirs(UPLOAD_FOLDER_PLAYERS, exist_ok=True)

        filename = f"{uuid.uuid4().hex}.{ext}"
        filepath = os.path.join(UPLOAD_FOLDER_PLAYERS, filename)

        with open(filepath, "wb") as f:
            f.write(await image.read())

        image_path = f"uploads/players/{filename}"

    # ================= DB =================
    conn = get_db_connection()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    try:
        # -------- INSERT PLAYER --------
        cursor.execute("""
            INSERT INTO players 
            (name, nickname, age, category, type, base_price, total_runs, highest_runs, 
             wickets_taken, times_out, image_path, jersey, mobile_No, email_Id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            full_name,
            nickName,
            age,
            category,
            style,
            basePrice,
            totalRuns,
            highestRuns,
            wickets,
            outs,
            image_path,
            jerseyNo,
            mobile,
            emailId
        ))

        player_id = cursor.lastrowid

        # -------- INSERT PLAYER-TEAMS --------
        for team_id in teams:
            cursor.execute(
                "INSERT INTO player_teams (player_id, team_id) VALUES (%s, %s)",
                (player_id, int(team_id))
            )

        conn.commit()

        return {
            "message": "Player added successfully!",
            "player_id": player_id
        }

    except pymysql.IntegrityError:
        conn.rollback()
        raise HTTPException(
            status_code=400,
            detail="Player with same name or jersey number exists"
        )

    except Exception as e:
        conn.rollback()
        print("❌ add-player error:", e)
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        cursor.close()
        conn.close()