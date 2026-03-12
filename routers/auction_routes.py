from fastapi import APIRouter, HTTPException, Request
from datetime import datetime, timedelta, timezone
import pymysql
import asyncio
from decimal import Decimal
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


def seconds_remaining(expires_at):
    now = datetime.now(timezone.utc)
    remaining = (expires_at - now).total_seconds()
    return max(0, int(remaining))

@router.get("/current-auction")
async def get_current_auction(request: Request):

    token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user = verify_token(token)

    conn = get_db_connection()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    try:
        #STEP 1 : Fetch Current auction
        cursor.execute("""
            SELECT 
                ca.player_id,
                ca.start_time,
                ca.expires_at,
                ca.auction_duration,
                ca.paused,
                ca.paused_remaining,
                p.name, p.image_path, p.jersey,
                p.category, p.type, p.base_price,
                p.highest_runs, p.total_runs
            FROM current_auction ca
            JOIN players p ON ca.player_id = p.id
            LIMIT 1
        """)
        auction = cursor.fetchone()

        if not auction:
            return {"status": "no_active_auction"}

        player_id = auction["player_id"]

        base_price = auction["base_price"] or 0

        if isinstance(base_price, Decimal):
            base_price = float(base_price)

        # Remaining time
        expires_at = auction["expires_at"]

        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        paused = bool(auction.get("paused"))
        paused_remaining = int(auction.get("paused_remaining") or 0)

        remaining = paused_remaining if paused else seconds_remaining(expires_at)

        # STEP 3: Highest bid
        cursor.execute("""
            SELECT b.team_id, t.name AS team_name, b.bid_amount
            FROM live_bids b
            JOIN teams t ON b.team_id = t.team_id
            WHERE b.player_id = %s
            ORDER BY b.bid_amount DESC, b.bid_time ASC
            LIMIT 1
        """, (player_id,))

        top_bid = cursor.fetchone()

        current_bid = float(top_bid["bid_amount"]) if top_bid else float(base_price)

        # STEP 4: TEAM BALANCE
        team_balance = 0

        if user.get("role") == "team":

            cursor.execute(
                "SELECT purse FROM teams WHERE team_id=%s",
                (user.get("team_id"),)
            )

            team = cursor.fetchone()

            team_balance = float(team["purse"]) if team else 0

        # STEP 5: BID HISTORY
        cursor.execute("""
            SELECT 
                b.team_id,
                t.name AS team_name,
                b.bid_amount,
                b.bid_time
            FROM live_bids b
            JOIN teams t ON b.team_id = t.team_id
            WHERE b.player_id = %s
            ORDER BY b.bid_time ASC
        """, (player_id,))

        history_raw = cursor.fetchall() or []

        history = []

        for row in history_raw:

            bt = row.get("bid_time")

            bid_time_str = (
                bt.strftime("%Y-%m-%d %H:%M:%S") if bt else None
            )

            history.append({
                "team_id": row["team_id"],
                "team_name": row["team_name"],
                "bid_amount": float(row["bid_amount"]),
                "bid_time": bid_time_str,
            })

        return {
            "status": "auction_active",
            "player": {
                "id": auction["player_id"],
                "name": auction["name"],
                "jersey": auction["jersey"],
                "category": auction["category"],
                "type": auction["type"],
                "image_path": auction["image_path"],
                "base_price": float(base_price),
                "highest_runs": auction["highest_runs"],
            },
            "currentBid": current_bid,
            "highest_bid": top_bid,
            "remaining_seconds": remaining,
            "auction_duration": auction["auction_duration"],
            "teamBalance": team_balance,
            "nextSteps": [
                current_bid + 500,
                current_bid + 1000,
                current_bid + 1500
            ],
            "paused": paused,
            "canBid": user.get("role") == "team",
            "history": history
        }

    except Exception as e:
        print("❌ ERROR in /current-auction:", e)
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        cursor.close()
        conn.close()