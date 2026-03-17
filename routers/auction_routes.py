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

    token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    payload = verify_token(token)

    if not payload or payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    mode = data.mode
    duration = data.duration or 40
    player_id = data.player_id

    conn = get_db_connection()

    if conn is None:
        raise HTTPException(500, "Database connection failed")

    cursor = conn.cursor(pymysql.cursors.DictCursor)

    try:

        # 🚫 prevent double auction
        cursor.execute("SELECT * FROM current_auction LIMIT 1")
        if cursor.fetchone():
            raise HTTPException(400, "Auction already running")

        # -------- SELECT PLAYER --------

        if mode == "manual":

            if not player_id:
                raise HTTPException(400, "player_id required")

            cursor.execute(
                "SELECT * FROM players WHERE id=%s",
                (player_id,)
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
                WHERE p.id NOT IN (
                    SELECT player_id FROM sold_players
                )
                ORDER BY u.id ASC
                LIMIT 1
            """)

            player = cursor.fetchone()

        else:
            raise HTTPException(400, "Invalid mode")

        if not player:
            print("🏁 No eligible players remaining")
            await sio.emit("auction_finished", {
                "message": "No players available for auction"
            })

            return{
                "status": "finished",
                "message": "No players available for auction"
            }

        player_id = player["id"]

        # -------- RESET TABLES --------

        cursor.execute("DELETE FROM current_auction")
        cursor.execute("DELETE FROM live_bids")

        # -------- INSERT CURRENT AUCTION --------

        start_time = datetime.now(timezone.utc)
        expires_at = start_time + timedelta(seconds=duration)

        cursor.execute("""
            INSERT INTO current_auction
            (player_id, start_time, expires_at, auction_duration, mode)
            VALUES (%s,%s,%s,%s,%s)
        """, (
            player_id,
            start_time,
            expires_at,
            duration,
            mode
        ))

        conn.commit()

        # -------- SOCKET EVENTS --------

        await sio.emit("timer_update", {
            "remaining_seconds": duration,
            "server_time": start_time.isoformat()
        })

        await sio.emit("auction_started", {
            "player_id": player_id,
            "player_name": player["name"],
            "mode": mode,
            "duration": duration,
            "expires_at": expires_at.isoformat()
        })

        # -------- START TIMER --------

        asyncio.create_task(
            background_timer(
                player_id,
                expires_at,
                mode,
                payload.get("session_id")
            )
        )

        print(f"🚀 Auction started for {player['name']}")

        return {
            "status": "auction_started",
            "player_id": player_id,
            "player_name": player["name"],
            "duration": duration,
            "expires_at": expires_at.isoformat()
        }

    except Exception as e:
        conn.rollback()
        raise e

    except Exception as e:
        conn.rollback()
        print("❌ start-auction error: ",e)
        raise HTTPException(status_code=500, detail="Internal server error")

    finally:
        cursor.close()
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

@router.post("/pause-auction")
async def pause_auction(request: Request):

    # ---------- AUTH CHECK ----------
    token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    payload = verify_token(token)

    if not payload or payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    conn = get_db_connection()

    if conn is None:
        raise HTTPException(status_code=500, detail="Database connection failed")

    cursor = conn.cursor(pymysql.cursors.DictCursor)

    try:

        # ---------- GET ACTIVE AUCTION ----------
        cursor.execute("""
            SELECT player_id, expires_at, paused
            FROM current_auction
            LIMIT 1
        """)

        auction = cursor.fetchone()

        if not auction:
            raise HTTPException(status_code=400, detail="No active auction")

        if auction["paused"] == 1:
            raise HTTPException(status_code=400, detail="Auction already paused")

        player_id = auction["player_id"]
        expires_at = auction["expires_at"]

        # ---------- Normalize datetime ----------
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)

        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)

        remaining = max(0, int((expires_at - now).total_seconds()))

        # ---------- UPDATE DB ----------
        cursor.execute("""
            UPDATE current_auction
            SET paused = 1,
                paused_remaining = %s
            WHERE player_id = %s
        """, (remaining, player_id))

        conn.commit()

        print(f"⏸ Auction paused for player {player_id} with {remaining}s remaining")

        # ---------- SOCKET EVENT ----------
        await sio.emit("auction_paused", {
            "paused": True,
            "remaining_seconds": remaining
        })

        return {
            "status": "auction_paused",
            "player_id": player_id,
            "remaining_seconds": remaining
        }

    except Exception as e:
        conn.rollback()
        print("❌ Pause auction error:", e)
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        cursor.close()
        conn.close()

@router.post("/resume-auction")
async def resume_auction(request: Request):

    # ------------- AUTH CHECK -------------
    token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    payload = verify_token(token)

    if not payload or payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    
    conn = get_db_connection()

    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    try:

        # -------------- FIND PAUSED AUCTION --------------
        cursor.execute("""
            SELECT player_id, paused_remaining, mode
            FROM current_auction
            WHERE paused = 1
            LIMIT 1
        """)

        auction = cursor.fetchone()

        if not auction:
            raise HTTPException(status_code=400, detail="No paused auction found")
        
        remaining = auction["paused_remaining"] or 0

        if remaining <= 0:
            raise HTTPException(status_code=400, detail="Auction time already ended")
        
        # ---------------- NEW EXPIRY ------------------
        new_end_time = datetime.now(timezone.utc) + timedelta(seconds=remaining)

        cursor.execute("""
            UPDATE current_auction
            SET paused = 0,
                paused_remaining = NULL,
                expires_at = %s
            WHERE player_id = %s
        """,(new_end_time, auction["player_id"]))

        conn.commit()

        player_id = auction["player_id"]
        mode = auction["mode"]

        print(f"▶ Auction resumed for player {player_id} - {remaining}s remaining")

        # # --------------- START TIMER AGAIN -----------------
        # asyncio.create_task(
        #     background_timer(
        #         player_id,
        #         new_end_time,
        #         mode,
        #         payload.get("session_id")
        #     )
        # )

        # ---------------- NOTIFY CLIENTS ----------------
        await sio.emit("auction_resumed",{
            "paused": False,
            "remaining_seconds": remaining,
            "expires_at": new_end_time.isoformat()
        })

        return{
            "message": "Auction resumed successfully",
            "player_id": player_id,
            "remaining": remaining
        }
    
    except Exception as e:
        conn.rollback()
        print("❌ Resume auction error: ", e)
        raise HTTPException(status_code=500, detail=str(e))
    
    finally:
        cursor.close()
        conn.close()


@router.post("/next-auction")
async def next_auction(request: Request):

    # -------------- AUTH -------------
    token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    payload = verify_token(token)

    if not payload or payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    
    conn = get_db_connection()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    try:
        # --------------- CURRENT AUCTION ------------------

        cursor.execute("SELECT * FROM current_auction LIMIT 1")
        auction = cursor.fetchone()

        if not auction:
            raise HTTPException(status_code=400, detail="No active auction")
        
        player_id = auction["player_id"]

        # --------------- TOP BID ---------------
        cursor.execute("""
            SELECT b.team_id, b.bid_amount, t.name AS team_name
            FROM live_bids b
            JOIN teams t ON b.team_id = t.team_id
            WHERE b.player_id = %s
            ORDER BY b.bid_amount DESC
            LIMIT 1
        """, (player_id,))

        top_bid = cursor.fetchone()

        # ------------- SOLD ------------
        if top_bid:
            cursor.execute("""
                UPDATE teams
                SET purse = purse - %s
                WHERE team_id = %s
            """, (top_bid["bid_amount"], top_bid["team_id"]))

            cursor.execute("""
                INSERT INTO sold_player
                (player_id, team_id, sold_price, sold_time)
                VALUES (%s, %s, %s, NOW())
            """,(
                player_id,
                top_bid["team_id"],
                top_bid["bid_amount"]
            ))

            await sio.emit("auction_ended", {
                "status": "sold",
                "player_id": player_id,
                "team_bid": top_bid["team_id"],
                "team_name": top_bid["team_name"],
                "bid_amount": float(top_bid["bid_amount"])
            })

        # ------------ UNSOLD ------------
        else:

            cursor.execute("""
                INSERT INTO unsold_players
                (player_id, reason, added_on)
                VALUES(%s, %s,NOW())
            """, (player_id,"Admin skipped"))

            await sio.emit("auction_ended",{
                "status": "unsold",
                "player_id": player_id
            })

        # ------------- CLEANUP ------------
        cursor.execute("DELETE FROM current_auction WHERE player_id=%s", (player_id,))
        cursor.execute("DELETE FROM live_bids WHERE player_id = %s", (player_id,))

        conn.commit()

        print(f"⏭ Admin skipped player {player_id}")

        return{
            "status": "auction_moved",
            "player_id": player_id
        }
    except Exception as e:

        conn.rollback()
        print("Next auction error: ", e)
        raise HTTPException(status_code=500, detail=str(e))
    
    finally:
        cursor.close()
        conn.close()

@router.post("/cancel-auction")
async def cancel_auction(request: Request):

    # ---------- AUTH CHECK ----------
    token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    payload = verify_token(token)

    if not payload or payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    
    conn = get_db_connection()

    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    try:
        # ----------- CHECK CURRENT AUCTION ------------
        cursor.execute("SELECT * FROM current_auction LIMIT 1")
        auction = cursor.fetchone()

        if not auction:
            raise HTTPException(status_code=400, detail="No active auction")
        
        player_id = auction["player_id"]

        # --------------- FETCH PLAYER INFO ---------------
        cursor.execute("""
            SELECT id, name, category, type, image_path, base_price
            FROM players
            WHERE id = %s
        """, (player_id,))

        player_info = cursor.fetchone()

        if player_info and isinstance(player_info.get("base_price"), Decimal):
            player_info["base_price"] = float(player_info["base_price"])

        # if not player_info:
        #     player_info = {
        #         "id": player_id,
        #         "name": "Unknown"
        #     }

        # -------------- MARK UNSOLD --------------
        cursor.execute("""
            INSERT INTO unsold_players
            (player_id, reason, added_on)
            VALUES (%s, %s, NOW())
        """, (
            player_id,
            "Auction manually cancelled by admin"
        ))

        # ------------- CLEANUP ----------------
        cursor.execute("DELETE FROM current_auction WHERE player_id = %s", (player_id,))

        cursor.execute("DELETE FROM live_bids WHERE player_id = %s", (player_id,))

        conn.commit()

        # ------------ EMIT EVENT --------------
        await sio.emit("auction_ended", {
            "status":"unsold",
            "player": player_info,
            "message": "🛑 Auction cancelled by admin - player marked unsold manually"
        })

        print(f"🛑 Auction cancelled manually for player {player_info.get('name')}")

        return{
            "message": f"Auction cancelled for {player_info.get('name')}",
            "player": player_info
        }
    
    except Exception as e:

        conn.rollback()
        print("❌ cancel-auction error: ", e)
        raise HTTPException(status_code=500, detail= str(e))
    
    finally:
        cursor.close()
        conn.close()


@router.get("/auction-state")
async def auction_state(request: Request):

    token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    payload = verify_token(token)

    if not payload:
        raise HTTPException(status_code=403, detail="Invalid token")

    conn = get_db_connection()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    try:

        # ---------------- CURRENT AUCTION ----------------
        cursor.execute("""
        SELECT 
            ca.player_id,
            ca.start_time,
            ca.expires_at,
            ca.auction_duration,
            ca.paused,
            ca.paused_remaining,
            p.name,
            p.image_path,
            p.jersey,
            p.category,
            p.type,
            p.base_price,
            p.highest_runs,
            p.total_runs
        FROM current_auction ca
        JOIN players p ON ca.player_id = p.id
        LIMIT 1
        """)

        auction = cursor.fetchone()

        if not auction:
            return {
                "status": "no_active_auction"
            }

        player_id = auction["player_id"]

        # ---------------- REMAINING TIME ----------------
        if auction["paused"]:
            remaining = int(auction["paused_remaining"] or 0)
        else:
            expires_at = auction["expires_at"]

            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at)

            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)

            now = datetime.now(timezone.utc)

            remaining = max(
                0,
                int((expires_at - now).total_seconds())
            )

        # ---------------- HIGHEST BID ----------------
        cursor.execute("""
        SELECT b.team_id, t.name AS team_name, b.bid_amount
        FROM live_bids b
        JOIN teams t ON b.team_id = t.team_id
        WHERE b.player_id = %s
        ORDER BY b.bid_amount DESC
        LIMIT 1
        """, (player_id,))

        highest_bid = cursor.fetchone()

        # ---------------- BID HISTORY ----------------
        cursor.execute("""
        SELECT b.team_id, t.name AS team_name, b.bid_amount, b.bid_time
        FROM live_bids b
        JOIN teams t ON b.team_id = t.team_id
        WHERE b.player_id = %s
        ORDER BY b.bid_time ASC
        """, (player_id,))

        history = cursor.fetchall()

        current_bid = (
            float(highest_bid["bid_amount"])
            if highest_bid
            else float(auction["base_price"])
        )

        return {
            "status": "auction_active",
            "player": {
                "id": player_id,
                "name": auction["name"],
                "jersey": auction["jersey"],
                "category": auction["category"],
                "type": auction["type"],
                "image_path": auction["image_path"],
                "base_price": float(auction["base_price"]),
                "highest_runs": auction["highest_runs"],
                "total_runs": auction["total_runs"]
            },
            "current_bid": current_bid,
            "highest_bid": highest_bid,
            "remaining_seconds": remaining,
            "paused": bool(auction["paused"]),
            "history": history
        }

    except Exception as e:

        raise HTTPException(status_code=500, detail=str(e))

    finally:
        cursor.close()
        conn.close()

@router.get("/auction-status")
async def auction_status():

    conn = get_db_connection()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    try:

        cursor.execute("SELECT player_id FROM current_auction LIMIT 1")
        auction = cursor.fetchone()

        if auction:
            return {
                "status": "auction_live",
                "player_id": auction["player_id"]
            }

        return {
            "status": "waiting"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        cursor.close()
        conn.close()