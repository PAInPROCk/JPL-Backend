from fastapi import APIRouter, HTTPException, Request
from datetime import datetime, timedelta, timezone
import pymysql
import asyncio

from auction.auction_engine import background_timer
from core.database import get_db_connection
from auth.auth_handler import verify_token
from sockets.socket_manager import sio
from models.schemas import StartAuctionRequest

router = APIRouter()

@router.post("/start-auction")
async def start_auction(data: StartAuctionRequest, request: Request):
    #-------------- AUTH CHECK ---------------
    token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    payload = verify_token(token)

    if not payload or payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    
    mode = data.mode
    player_id = data.player_id
    duration = data.duration

    conn = get_db_connection()

    if conn is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        #------------ SELECT PLAYER --------------
        if mode == "manual":
            if not player_id:
                raise HTTPException(status_code=400, detail="player_id required")
            
            cursor.execute(
                "SELECT * FROM players WHERE id=%s",(player_id,)
            )

            player = cursor.fetchone()

        elif mode == "random":
            cursor.execute("""
                    SELECT * FROM players
                    WHERE id NOT IN (
                            SELECT player_id FROM sold_players
                            UNION 
                            SELECT player_id FROM unsold_players
                        )
                        ORDER BY RAND()
                        LIMIT 1
                """)
            player = cursor.fetchone()

        elif mode == "unsold":
            cursor.execute("""
                    SELECT p.*
                    FROM players p
                    JOIN unsold_players u ON p.id = u.player_id
                    WHERE p.id NOT IN (SELECT player_id FROM sold_players)
                    ORDER BY u.id ASC
                    LIMIT 1
                """)
            player = cursor.fetchone()

        else:
            raise HTTPException(status_code=400, detail="Invalid mode")
        
        if not player:
            raise HTTPException(status_code=404, detail="No eligible player found")
        
        player_id = player["id"]

        #-------------- RESET AUCTION TABLES --------------
        cursor.execute("DELETE FROM current_auction")
        cursor.execute("DELETE FROM live_bids")

        conn.commit()

        #-------------- SET AUCTION TIMER --------------
        start_time = datetime.now(timezone.utc)
        expires_at = start_time + timedelta(seconds=duration)

        cursor.execute("""
                INSERT INTO current_auction
                (player_id, start_time, expires_at, auction_duration, mode)
                VALUES (%s, %s, %s, %s, %s)
            """,(
                player_id,
                start_time,
                expires_at,
                duration,
                mode
            ))
        
        conn.commit()

        #----------------- EMIT SOCKET EVENTS ------------------
        await sio.emit("timer_update", {
            "remaining_seconds": duration,
            "server_time": datetime.now(timezone.utc).isoformat()
        })

        await sio.emit("auction_started", {
            "player_id": player_id,
            "player_name": player["name"],
            "mode":mode,
            "duration": duration,
            "expires_at": expires_at.isoformat()
        })

        asyncio.create_task(
            background_timer(player_id, expires_at, mode, payload.get("session_id"))
        )
        
        print(f"🚀 Auction started for {player['name']}")

        return{
            "message": f"Auction Started for {player['name']}",
            "player_id": player_id,
            "player_name": player["name"],
            "mode": mode,
            "duration": duration,
            "expires_at": expires_at.isoformat(),
            "status": "auction_started"
        }
    
    except Exception as e:
        conn.rollback()
        print("Auction start error: ", e)
        raise HTTPException(status_code=500, detail=str(e))
    
    finally:
        if cursor:
            cursor.close()

        if conn:
            conn.close()
