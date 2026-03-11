from sockets.socket_manager import sio
import asyncio
from core.database import get_db_connection
import pymysql
from auth.auth_handler import verify_token

MIN_INCREAMENT = 500

bid_lock = asyncio.Lock()

def register_socket_events():
    @sio.event
    async def connect(sid, eviron):
        print("✅ Socket Connected:", sid)

    @sio.event
    async def disconnect(sid):
        print("❌ Socket Disconnected:", sid)

@sio.event
async def place_bid(sid, data):
    async with bid_lock:

        try:
            team_id = data.get("team_id")
            player_id = data.get("player_id")
            bid_amount = float(data.get("bid_amount"))

        except Exception:
            await sio.emit("bid_rejected",
                    {"error" : "Invalid bid amount"},
                    to = sid
                )
            return
        
        conn = get_db_connection()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        try:
            #--------------- Active Auction --------------
            cursor.execute("SELECT * FROM current_auction LIMIT 1")
            auction = cursor.fetchone()

            if not auction:
                await sio.emit("bid_rejected", 
                        {"error": "No active auction"},
                        to = sid
                    )
                return
            
            if auction.get("paused"):
                await sio.emit("bid_rejected",
                        {"error": "Auction is paused"},
                        to = sid
                    )
                return
            
            active_player = auction["player_id"]

            if str(player_id) != str(active_player):
                await sio.emit("bid_rejected",
                        {"error": "Invalid player"},
                        to = sid
                    )
                return
            
            #--------------- Team Check -------------
            cursor.execute(
                "SELECT team_id, name, purse FROM teams WHERE team_id = %s",
                (team_id)
            )
            team = cursor.fetchone()

            if not team:
                await sio.emit("bid_rejected", 
                        {"error": "Team not found"},
                        to = sid
                    )
                return
            
            if float(team["purse"]) < bid_amount:
                await sio.emit("bid_rejected",
                        {"error": "Insufficient purse"},
                        to = sid
                    )
                return
            
            #-------------- Current Highest Bid ---------------
            cursor.execute("""
                    SELECT MAX(bid_amount) AS highest_bid
                    FROM live_bids
                    WHERE player_id = %s
                """, (active_player,))
            
            row = cursor.fetchone()
            highest_bid = float(row["highest_bid"]) if row and row["highest_bid"] else 0

            required = highest_bid + MIN_INCREAMENT

            if bid_amount < required:
                await sio.emit("bid_rejected", 
                    {"error": f"Minimum bid ₹{required}"},
                    to = sid
                )
                return
            
            #--------------- Insert Live Bid --------------
            cursor.execute("""
                    INSERT INTO live_bids (player_id, team_id, bid_amount, bid_time)
                    VALUES(%s, %s, %s, NOW())
                    ON DUPLICATE KEY UPDATE
                           bid_amount = VALUES(bid_amount),
                           bid_time= NOW()
                """,(
                    active_player,
                    team_id,
                    bid_amount
                ))
            
            #--------------- Bid history ----------------
            cursor.execute("""
                INSERT INTO bids (player_id, team_id, bid_amount, bid_time)
                VALUES (%s, %s, %s, NOW())
            """,(
                active_player,
                team_id,
                bid_amount
            ))

            conn.commit()

            print(f"💰 Bid accepted: Team {team_id} ➡ ₹{bid_amount}")

            #--------------- ACK TO BIDDER ----------------
            await sio.emit("bid_accepted",{
                "team_id": team_id,
                "bid_amount": bid_amount
            }, to = sid)

            #-------------- Broadcast -------------
            await sio.emit("bid_placed", {
                "team_id": team_id,
                "team_name": team["name"],
                "bid_amount": bid_amount
            })

        except Exception as e:
            conn.rollback()

            print("⚠ place_bid error: ", e)

            await sio.emit("bid_rejected",
                {"error": str(e)},
                to = sid
            )

        finally:
            cursor.close()
            conn.close()