import asyncio
from datetime import datetime, timezone, timedelta
import pymysql

from core.database import get_db_connection
from sockets.socket_manager import sio

async def background_timer(player_id, expires_at, mode, session_id):

    print(f"⏰ Timer started for player {player_id}")

    while True:

        conn = get_db_connection()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        try:
            cursor.execute(
                "SELECT paused, paused_remaining, expires_at FROM current_auction WHERE player_id=%s",
                (player_id,)
            )

            state = cursor.fetchone()
            print("PAUSE STATE:", state["paused"])

        finally:
            cursor.close()
            conn.close()

        if not state:
            print("⚠ Auction row missing — stopping timer")
            return

        # ---------------- PAUSED ----------------
        if state["paused"]:
            await asyncio.sleep(1)
            continue

        # ---------------- NORMAL TIMER ----------------
        now = datetime.now(timezone.utc)

        db_expires = state["expires_at"]

        if isinstance(db_expires, str):
            db_expires = datetime.fromisoformat(db_expires)

        if db_expires.tzinfo is None:
            db_expires = db_expires.replace(tzinfo=timezone.utc)

        remaining = max(0, int((db_expires - now).total_seconds()))

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

        cursor.execute(
            "SELECT * FROM current_auction WHERE player_id=%s FOR UPDATE",
            (player_id,)
        )

        auction = cursor.fetchone()

        if not auction:
            return

        # ---------------- HIGHEST BID ----------------
        cursor.execute("""
        SELECT b.team_id, b.bid_amount, t.name AS team_name
        FROM live_bids b
        JOIN teams t ON b.team_id = t.team_id
        WHERE b.player_id = %s
        ORDER BY b.bid_amount DESC
        LIMIT 1
        """, (player_id,))

        top_bid = cursor.fetchone()

        if top_bid:

            cursor.execute("""
            UPDATE teams
            SET purse = purse - %s
            WHERE team_id = %s
            """, (top_bid["bid_amount"], top_bid["team_id"]))

            cursor.execute("""
            INSERT INTO sold_players
            (player_id, team_id, sold_price, sold_time)
            VALUES (%s,%s,%s,NOW())
            """, (
                player_id,
                top_bid["team_id"],
                top_bid["bid_amount"]
            ))

            await sio.emit("auction_ended", {
                "status": "sold",
                "player_id": player_id,
                "team_id": top_bid["team_id"],
                "team_name": top_bid["team_name"],
                "bid_amount": float(top_bid["bid_amount"])
            })

            await sio.emit("next_player_loading", {
                "delay": 10
            })
        else:

            cursor.execute("""
            INSERT INTO unsold_players
            (player_id, reason, added_on)
            VALUES (%s,%s,NOW())
            """, (player_id, "No Bids"))

            await sio.emit("auction_ended", {
                "status": "unsold",
                "player_id": player_id
            })

            await sio.emit("next_player_loading", {
                "delay": 10
            })

        cursor.execute("DELETE FROM current_auction WHERE player_id=%s", (player_id,))
        cursor.execute("DELETE FROM live_bids WHERE player_id=%s", (player_id,))

        conn.commit()

    finally:
        cursor.close()
        conn.close()

    # ---------------- DELAY BEFORE NEXT PLAYER ----------------
    print("⏳ Waiting 10 seconds before next player")
    await asyncio.sleep(10)

    conn = get_db_connection()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    try:

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

        next_player = cursor.fetchone()

        if not next_player:
            print("🏁 Auction finished")
            await sio.emit("auction_finished", {})
            return

        start_time = datetime.now(timezone.utc)
        duration = 120
        expires_at = start_time + timedelta(seconds=duration)

        cursor.execute("""
        INSERT INTO current_auction
        (player_id,start_time,expires_at,auction_duration,mode)
        VALUES (%s,%s,%s,%s,%s)
        """, (
            next_player["id"],
            start_time,
            expires_at,
            duration,
            mode
        ))

        conn.commit()

        await sio.emit("auction_started", {
            "player_id": next_player["id"],
            "player_name": next_player["name"],
            "mode": mode,
            "duration": duration,
            "expires_at": expires_at.isoformat()
        })

        asyncio.create_task(
            background_timer(
                next_player["id"],
                expires_at,
                mode,
                session_id
            )
        )

        print(f"🚀 Next auction started for {next_player['name']}")

    finally:
        cursor.close()
        conn.close()