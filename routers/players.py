from fastapi import APIRouter
from core.database import get_db_connection
import pymysql

router = APIRouter()

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
        