from fastapi import APIRouter, UploadFile, File, HTTPException, Request, Form
from auth.auth_handler import verify_token
from core.database import get_db_connection
import pymysql
import os
import uuid
from typing import Optional





router = APIRouter()

UPLOAD_FOLDER_TEAMS = "uploads/teams"

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp" , "jfif"}

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
                captain,
                `Team_Rank` AS trank,
                Total_Budget AS total_budget,
                Season_Budget AS current_budget,
                Players_Bought AS players_bought,
                image_path
            FROM teams
            ORDER BY name ASC
        """)

        teams = cursor.fetchall()
        for t in teams:
            if not t.get("image_path"):
                t["image_path"] = None

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
def get_team_by_id(team_id: int):
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
                p.image_path,
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


@router.post("/add-team")
async def add_team(
    request: Request,

    # -------- FORM FIELDS --------
    teamName: Optional[str] = Form(None),
    captain: Optional[str] = Form(None),
    teamRank: Optional[int] = Form(None),
    totalBudget: Optional[float] = Form(None),
    seasonBudget: Optional[float] = Form(None),
    playersBought: Optional[int] = Form(None),
    mobile: Optional[str] = Form(None),
    emailId: Optional[str] = Form(None),

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

    # ================= VALIDATION =================
    if not teamName:
        raise HTTPException(status_code=400, detail="Team name is required")

    # ================= IMAGE UPLOAD =================
    image_path = None

    if image:
        ext = image.filename.split(".")[-1].lower()

        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail="Invalid image format")

        os.makedirs(UPLOAD_FOLDER_TEAMS, exist_ok=True)

        filename = f"{uuid.uuid4().hex}.{ext}"
        filepath = os.path.join(UPLOAD_FOLDER_TEAMS, filename)

        with open(filepath, "wb") as f:
            f.write(await image.read())

        image_path = f"uploads/teams/{filename}"

    # ================= NORMALIZE VALUES =================
    teamRank = teamRank or 0
    totalBudget = totalBudget or 0
    seasonBudget = seasonBudget or 0
    playersBought = playersBought or 0

    # ================= DB =================
    conn = get_db_connection()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    try:
        cursor.execute("""
            INSERT INTO teams 
            (name, captain, mobile_No, email_Id, Team_Rank, Total_Budget, Season_Budget, Players_Bought, image_path)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            teamName,
            captain,
            mobile,
            emailId,
            teamRank,
            totalBudget,
            seasonBudget,
            playersBought,
            image_path
        ))

        conn.commit()

        return {
            "message": "Team added successfully!"
        }

    except pymysql.IntegrityError:
        conn.rollback()
        raise HTTPException(
            status_code=400,
            detail="Team name already exists"
        )

    except Exception as e:
        conn.rollback()
        print("❌ add-team error:", e)
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        cursor.close()
        conn.close()
