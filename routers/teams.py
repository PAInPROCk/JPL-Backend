from fastapi import APIRouter, UploadFile, File, HTTPException, Request, Form
from auth.auth_handler import verify_token, get_token_from_request
from core.database import get_db_connection
import pymysql
import os
import io
import uuid
from typing import Optional
from PIL import Image
from core.image_handler import (
    validate_image_bytes,
    crop_and_resize_to_square,
    compress_to_webp,
    delete_image_from_supabase
)





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
                team_rank AS trank,
                total_budget AS total_budget,
                season_budget AS current_budget,
                players_bought AS players_bought,
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
    password: Optional[str] = Form(None), # 👈 1. ADDED: Password field for team user 

    # -------- FILE --------
    image: Optional[UploadFile] = File(None)
):
    # ================= AUTH =================
    token = get_token_from_request(request)

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
        img_content = await image.read()
        from core.image_handler import validate_image_bytes
        validate_image_bytes(img_content)
        
        try:
            from PIL import Image
            from core.image_handler import crop_and_resize_to_square, compress_to_webp, upload_image_to_supabase
            
            pil_image = Image.open(io.BytesIO(img_content))
            processed_image = crop_and_resize_to_square(pil_image, 400)
            webp_bytes = compress_to_webp(processed_image)
            
            filename = f"{uuid.uuid4().hex}.webp"
            storage_path = f"teams/{filename}"
            image_path = upload_image_to_supabase(webp_bytes, storage_path)
        except HTTPException:
            raise
        except Exception as e:
            print("❌ Error processing/uploading team logo in add_team:", e)
            raise HTTPException(status_code=500, detail=f"Image upload failed: {str(e)}")

    # ================= NORMALIZE VALUES =================
    teamRank = teamRank or 0
    totalBudget = totalBudget or 0
    seasonBudget = seasonBudget or 0
    playersBought = playersBought or 0

    # ================= DB =================
    conn = get_db_connection()
    if conn is None:
        raise HTTPException(status_code=500, detail="Database connection failed")

    cursor = conn.cursor(pymysql.cursors.DictCursor)

    try:
        cursor.execute("""
            INSERT INTO teams 
            (name, captain, mobile_no, email_id, team_rank, total_budget, season_budget, purse, players_bought, image_path)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING team_id
        """, (
            teamName,
            captain,
            mobile,
            emailId,
            teamRank,
            totalBudget,
            seasonBudget,
            seasonBudget,
            playersBought,
            image_path
        ))
        new_team_row = cursor.fetchone()
        new_team_id = new_team_row["team_id"] if new_team_row else None
        conn.commit()

        # 3. Added: Automated Supabase auth user creation
        if emailId and new_team_id:
            try:
                from core.supabase_client import get_supabase_admin_client
                supabase_admin = get_supabase_admin_client()

                # Use provided password or fallback to default initial password                                     
                team_password = password if password else "JPLTeam@2026"                                                                                                                                                 
                supabase_admin.auth.admin.create_user({                                                             
                        "email": emailId,                                                                               
                        "password": team_password,                                                                      
                        "email_confirm": True, # Auto-confirm email                                                     
                        "user_metadata": {                                                                              
                            "name": teamName,                                                                           
                            "role": "team",                                                                             
                            "team_id": new_team_id                                                                      
                        },                                                                                              
                        "app_metadata": {                                                                               
                            "role": "team"                                                                              
                        }                                                                                               
                    })                                                                                                  
                print(f"✅ Created Supabase Auth User for '{teamName}' (Email: {emailId}, Team ID: {new_team_id})") 
            except Exception as auth_err:                                                                           
                print("⚠ Warning: Team created in DB, but failed to create Supabase Auth User:", auth_err)          

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


#---------- UPDATE TEAM (PUT /team/{team_id}) ------------
@router.put("/team/{team_id}")
async def update_team(
    team_id: int,
    request: Request,
    teamName: Optional[str] = Form(None),
    captain: Optional[str] = Form(None),
    mobile: Optional[str] = Form(None),
    emailId: Optional[str] = Form(None),
    teamRank: Optional[int] = Form(None),
    totalBudget: Optional[float] = Form(None),
    seasonBudget: Optional[float] = Form(None),
    image: Optional[UploadFile] = File(None)
):
    token = get_token_from_request(request)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication token required")
    payload = verify_token(token)
    if not payload or payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")

    conn = get_db_connection()
    if conn is None:
        raise HTTPException(status_code=500, detail="Database connection failed")

    cursor = conn.cursor(pymysql.cursors.DictCursor)
    try:
        cursor.execute("SELECT * FROM teams WHERE team_id = %s", (team_id,))
        existing_team = cursor.fetchone()
        if not existing_team:
            raise HTTPException(status_code=404, detail="Team not found")

        image_path = existing_team.get("image_path")
        if image and image.filename:
            image_bytes = await image.read()
            validate_image_bytes(image_bytes)
            pil_img = Image.open(io.BytesIO(image_bytes))
            cropped_img = crop_and_resize_to_square(pil_img, 400)
            webp_bytes = compress_to_webp(cropped_img)

            from core.supabase_client import get_supabase_admin_client
            supabase_admin = get_supabase_admin_client()
            filename = f"teams/{uuid.uuid4().hex}.webp"
            supabase_admin.storage.from_("auctra-uploads").upload(
                path=filename,
                file=webp_bytes,
                file_options={"content-type": "image/webp", "upsert": "true"}
            )
            new_image_path = supabase_admin.storage.from_("auctra-uploads").get_public_url(filename)
            if image_path:
                delete_image_from_supabase(image_path)
            image_path = new_image_path

        updated_name = teamName if teamName is not None else existing_team["name"]
        updated_captain = captain if captain is not None else existing_team.get("captain")
        updated_mobile = mobile if mobile is not None else existing_team.get("mobile_no")
        updated_email = emailId if emailId is not None else existing_team.get("email_id")
        updated_rank = teamRank if teamRank is not None else existing_team.get("team_rank", 0)
        updated_total_budget = totalBudget if totalBudget is not None else existing_team.get("total_budget", 0.0)
        updated_season_budget = seasonBudget if seasonBudget is not None else existing_team.get("season_budget", 0.0)

        cursor.execute("""
            UPDATE teams
            SET name = %s,
                captain = %s,
                mobile_no = %s,
                email_id = %s,
                team_rank = %s,
                total_budget = %s,
                season_budget = %s,
                image_path = %s
            WHERE team_id = %s
        """, (
            updated_name,
            updated_captain,
            updated_mobile,
            updated_email,
            updated_rank,
            updated_total_budget,
            updated_season_budget,
            image_path,
            team_id
        ))
        conn.commit()

        return {
            "success": True,
            "message": "Team updated successfully",
            "team_id": team_id
        }

    except HTTPException:
        conn.rollback()
        raise
    except pymysql.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=400, detail="Team name already exists")
    except Exception as e:
        conn.rollback()
        print("❌ update-team error:", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conn.close()


#---------- DELETE TEAM (DELETE /team/{team_id}) ------------
@router.delete("/team/{team_id}")
def delete_team(team_id: int, request: Request):
    token = get_token_from_request(request)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication token required")
    payload = verify_token(token)
    if not payload or payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")

    conn = get_db_connection()
    if conn is None:
        raise HTTPException(status_code=500, detail="Database connection failed")

    cursor = conn.cursor(pymysql.cursors.DictCursor)
    try:
        cursor.execute("SELECT * FROM teams WHERE team_id = %s", (team_id,))
        team = cursor.fetchone()
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")

        image_path = team.get("image_path")
        if image_path:
            delete_image_from_supabase(image_path)

        cursor.execute("DELETE FROM teams WHERE team_id = %s", (team_id,))
        conn.commit()

        return {
            "success": True,
            "message": "Team deleted successfully",
            "team_id": team_id
        }

    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        print("❌ delete-team error:", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conn.close()
