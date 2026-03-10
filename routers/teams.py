from fastapi import APIRouter
from core.database import get_db_connection
import pymysql

router = APIRouter()

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