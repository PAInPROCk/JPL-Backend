from sockets.socket_manager import sio
import asyncio
from core.database import get_db_connection
import pymysql
from auth.auth_handler import verify_token
from decimal import Decimal


MIN_INCREAMENT = 500

bid_lock = asyncio.Lock()

def normalize_decimal(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    return obj

def register_socket_events():
    @sio.event
    async def connect(sid, eviron):
        print("✅ Socket Connected:", sid)

    @sio.event
    async def disconnect(sid):
        print("❌ Socket Disconnected:", sid)

    @sio.event
    async def join_auction(sid, data=None):
        print("JOIN AUCTION EVENT TRIGGERED")
        print(f"📡 Client joined auction: {sid}")

        conn = get_db_connection()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        try:
            cursor.execute("""
                SELECT
                    ca.player_id,
                    ca.expires_at,
                    ca.auction_duration,
                    p.name,
                    p.image_path,
                    p.base_price,
                    p.jersey,
                    p.category,
                    p.type,
                    p.highest_runs
                FROM current_auction ca
                JOIN players p ON ca.player_id = p.id
                LIMIT 1
            """)

            auction = cursor.fetchone()

            if not auction:
                await sio.emit(
                    "auction_state",
                    {"status":"no_active_auction"},
                    to = sid
                )
                return
        
            #Fetch highest bid
            cursor.execute("""
                SELECT b.team_id,t.name AS team_name, b.bid_amount
                FROM live_bids b
                JOIN teams t ON b.team_id = t.team_id
                WHERE b.player_id = %s
                ORDER BY b.bid_amount DESC
                LIMIT 1
            """, (auction["player_id"],))

            top_bid = cursor.fetchone()

            await sio.emit("auction_status", {
                "status": "auction_active",
                "player": {
                    "id": auction["player_id"],
                    "name": auction["name"],
                    "image_path": auction["image_path"],
                    "jersey": auction["jersey"],
                    "category": auction["category"],
                    "type": auction["type"],
                    "base_price": float(auction.get("base_price") or 0),
                    "highest_runs": auction.get("highest_runs") or 0
                },
                "highest_bid": {
                    "team_id": top_bid["team_id"],
                    "team_name": top_bid["team_name"],
                    "bid_amount": float(top_bid.get("bid_amount") or 0)
                } if top_bid else None

            }, to=sid)
            
        except Exception as e:
            print("❌ join_auction error: ", e)
        finally:
            cursor.close()
            conn.close()
        
    @sio.event
    async def place_bid(sid, data):

        async with bid_lock:
            
            team_id = data.get("team_id")
            player_id = data.get("player_id")
            bid_value = data.get("bid_amount")

            if bid_value is None:
                await sio.emit(
                    "bid_rejected",
                    {"error": "Bid amount is required"},
                    to=sid
                )
                return
            
            bid_amount = float(bid_value)

            if bid_amount is None:
                await sio.emit(
                    "bid_rejected",
                    {"error": "Bid amount missing"},
                    to=sid
                )
                return

            try:
                bid_amount = float(bid_amount)
            except (TypeError, ValueError):
                await sio.emit(
                    "bid_rejected",
                    {"error": "Invalid bid amount"}
                )
                return

            conn = get_db_connection()
            conn.begin()
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            try:

                # ---------------- ACTIVE AUCTION ----------------
                cursor.execute(
                    "SELECT * FROM current_auction LIMIT 1 FOR UPDATE"
                )
                auction = cursor.fetchone()

                if not auction:
                    await sio.emit(
                        "bid_rejected",
                        {"error": "No active auction"},
                        to=sid
                    )
                    return

                if auction.get("paused"):
                    await sio.emit(
                        "bid_rejected",
                        {"error": "Auction is paused"},
                        to=sid
                    )
                    return

                active_player = auction["player_id"]

                if str(player_id) != str(active_player):
                    await sio.emit(
                        "bid_rejected",
                        {"error": "Invalid player"},
                        to=sid
                    )
                    return

                # ---------------- TEAM CHECK ----------------
                cursor.execute(
                    "SELECT team_id, name, purse FROM teams WHERE team_id = %s",
                    (team_id,)
                )

                team = cursor.fetchone()

                if not team:
                    await sio.emit(
                        "bid_rejected",
                        {"error": "Team not found"},
                        to=sid
                    )
                    return

                if float(team["purse"]) < bid_amount:
                    await sio.emit(
                        "bid_rejected",
                        {"error": "Insufficient purse"},
                        to=sid
                    )
                    return

                # ---------------- PLAYER BASE PRICE ----------------
                cursor.execute(
                    "SELECT base_price FROM players WHERE id=%s",
                    (active_player,)
                )

                player = cursor.fetchone()
                base_price = float(player.get("base_price") or 0) if player else 0

                # ---------------- CURRENT HIGHEST BID ----------------
                cursor.execute(
                    """
                    SELECT MAX(bid_amount) AS highest_bid
                    FROM live_bids
                    WHERE player_id = %s
                    """,
                    (active_player,)
                )

                row = cursor.fetchone()
                highest_bid = float(row["highest_bid"]) if row and row["highest_bid"] else 0

                MIN_INCREMENT = 500

                required = max(highest_bid + MIN_INCREMENT, base_price)

                if bid_amount < required:
                    await sio.emit(
                        "bid_rejected",
                        {"error": f"Minimum bid ₹{required}"},
                        to=sid
                    )
                    return

                # ---------------- INSERT LIVE BID ----------------
                cursor.execute(
                    """
                    INSERT INTO live_bids
                    (player_id, team_id, bid_amount, bid_time)
                    VALUES (%s,%s,%s,NOW())
                    ON DUPLICATE KEY UPDATE
                        bid_amount = VALUES(bid_amount),
                        bid_time = NOW()
                    """,
                    (
                        active_player,
                        team_id,
                        bid_amount
                    )
                )

                # ---------------- BID HISTORY ----------------
                cursor.execute(
                    """
                    INSERT INTO bids
                    (player_id, team_id, bid_amount, bid_time)
                    VALUES (%s,%s,%s,NOW())
                    """,
                    (
                        active_player,
                        team_id,
                        bid_amount
                    )
                )

                conn.commit()

                print(f"💰 Bid accepted: Team {team_id} ➜ ₹{bid_amount}")

                # ---------------- ACK TO BIDDER ----------------
                await sio.emit(
                    "bid_accepted",
                    {
                        "player_id": active_player,
                        "team_id": team_id,
                        "bid_amount": float(bid_amount)
                    },
                    to=sid
                )


                # ---------- FETCH HIGHEST BID ----------
                cursor.execute("""
                SELECT b.team_id, b.bid_amount, t.name AS team_name
                FROM live_bids b
                JOIN teams t ON b.team_id = t.team_id
                WHERE b.player_id = %s
                ORDER BY b.bid_amount DESC
                LIMIT 1
                """, (active_player,))

                highest = cursor.fetchone()

                if highest:
                    if isinstance(highest["bid_amount"], Decimal):
                        highest["bid_amount"] = float(highest["bid_amount"])
                
                    if isinstance(highest["team_id"], Decimal):
                        highest["team_id"] = int(highest["team_id"])

                # ---------- FETCH BID HISTORY ----------
                cursor.execute("""
                SELECT b.team_id, t.name AS team_name, b.bid_amount, b.bid_time
                FROM live_bids b
                JOIN teams t ON b.team_id = t.team_id
                WHERE b.player_id = %s
                ORDER BY b.bid_time ASC
                """, (active_player,))

                history = cursor.fetchall()

                for h in history:
                    if isinstance(h["bid_amount"], Decimal):
                        h["bid_amount"] = float(h["bid_amount"])

                    if h.get("bid_time"):
                        h["bid_time"] = h["bid_time"].isoformat()

                if highest:
                    highest_bid_amount = float(highest["bid_amount"])
                else:
                    highest_bid_amount = base_price
                # ---------- BROADCAST UPDATE ----------
                await sio.emit("auction_update", {
                    "player_id": active_player,
                    "current_bid": highest_bid_amount,
                    "highest_bid": highest,
                    "history": history,
                })

            except Exception as e:

                conn.rollback()

                print("⚠ place_bid error:", e)

                await sio.emit(
                    "bid_rejected",
                    {"error": str(e)},
                    to=sid
                )

            finally:
                cursor.close()
                conn.close()