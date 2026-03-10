import asyncio
from datetime import datetime, timezone
import pymysql

from core.database import get_db_connection
from sockets.socket_manager import sio

async def background_timer(player_id, expires_at, mode, session_id):
    print(f"⏰ Timer started for player {player_id}")

    while True:
        now = datetime.now(timezone.utc)
        remaining = int((expires_at - now).total_seconds())

        if remaining <= 0:
            break

        await sio.emit("timer_update", {
            "remaining_seconds": remaining,
            "server_time": now.isoformat()
        })

        await asyncio.sleep(1)

    print("⏰ Timer expired")

    conn = get_db_connection()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    try:
        #Lock Auction row
        cursor.execute(
            "SELECT * FROM current_auction WHERE player_id=%s FOR UPDATE",
            (player_id,)
        )
        auction = cursor.fetchone()

        if not auction:
            return
        
        # Top Bid
        cursor.execute("""
                SELECT b.team_id, b.bid_amount, t.name AS team_name
                FROM live_bids b
                JOIN teams t ON b.team_id = t.team_id
                WHERE b.player_id = %s
                ORDER BY b.bid_amount DESC
                LIMIT 1
            """, (player_id))
        
        top_bid = cursor.fetchone()

        if top_bid:
            #Deduct purse
            cursor.execute("""
                UPDATE teams
                SET purse = purse - %s
                WHERE team_id = %s
            """, (top_bid["bid_amount", top_bid["team_id"]]))

            cursor.execute("""
                INSERT INTO sold_players
                (player_id, team_id, sold_price, sold_time)
                VALUES (%s, %s, %s, NOW())
            """,(
                player_id,
                top_bid["team_id"],
                top_bid["bid_amount"]
            ))

            cursor.execute(
                "DELETE FROM current_auction WHERE player_id = %s",
                (player_id,)
            )

            cursor.execute(
                "DELETE FROM live_bids WHERE player_id = %s",
                (player_id,)
            )

            conn.commit()

            await sio.emit("auction_ended", {
                "status": "sold",
                "player_id": player_id,
                "team_id": top_bid["team_id"],
                "team_name": top_bid["team_name"],
                "bid_amount": float(top_bid["bid_amount"])
            })
        else:

            cursor.execute(
                "DELETE FROM current_auction WHERE player_id = %s",
                (player_id,)
            )

            cursor.execute("""
                INSERT INTO unsold_players
                (player_id, reason, added_on)
                VALUES (%s, %s, NOW())
            """,(
                player_id,
                "No Bids"
            ))

            conn.commit()

            await sio.emit("auction_ended",{
                "status": "unsold",
                "player_id": player_id
            })
    finally:
        cursor.close()
        conn.close()

    print(f"🏁 Timer finished for player {player_id}")
