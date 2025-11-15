import eventlet
# ---- eventlet must be first ----

# ---- now normal imports ----
import threading
import os
import io
import csv
import uuid
from datetime import datetime, timedelta, timezone
import mysql.connector
import pandas as pd
import bcrypt
from flask import Flask, jsonify, request, session, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_cors import CORS, cross_origin
from werkzeug.utils import secure_filename
from decimal import Decimal



def safe_json(obj):
    """Recursively convert Decimals to float for JSON serialization."""
    if isinstance(obj, Decimal):
        return float(obj)
    elif isinstance(obj, dict):
        return {k: safe_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [safe_json(i) for i in obj]
    return obj

# Global variable to track pause/resume status
auction_status = {"paused": False}

# ---- Flask setup ----
base_dir = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER_PLAYERS = os.path.join(base_dir, 'uploads', 'players')
UPLOAD_FOLDER_TEAMS = os.path.join(base_dir, 'uploads', 'teams')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}
os.makedirs(UPLOAD_FOLDER_PLAYERS, exist_ok=True)
os.makedirs(UPLOAD_FOLDER_TEAMS, exist_ok=True)

app = Flask(__name__)
app.secret_key = "jpl_secret_here"
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_PERMANENT= True,
    PERMANENT_SESSION_LIFETIME= timedelta(hours=2),
    SESSION_TYPE="filesystem"
)

# ---- CORS setup ----
CORS(app, supports_credentials=True, origins=["http://localhost:3000"])

# ---- Socket setup ----
socketio = SocketIO(
    app,
    cors_allowed_origins=["http://localhost:3000"],
    async_mode="eventlet",
    manage_session=False
)


# ---- Auction timer globals ----
auction_timer = {
    "end_time": None,
    "active": False,
    "paused": False,
    "remaining_seconds": 0
}
timer_thread = None
thread_lock = threading.Lock()

from datetime import datetime, timezone

def ensure_aware_utc(dt):
    """
    Convert datetime or ISO string to timezone-aware datetime in UTC.
    Returns None if dt falsy.
    """
    if not dt:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except Exception:
            try:
                dt = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None
    # If it's a naive datetime, treat it as UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------- TIMEZONE SAFE UTILITY ----------------
def seconds_remaining(expires_at):
    """Safely calculate seconds remaining between expires_at and current UTC time."""
    expires_at = ensure_aware_utc(expires_at)
    if not expires_at:
        return 0
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except Exception:
            expires_at = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S")
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return max(0, int((expires_at - now).total_seconds()))


def background_timer(player_id, expires_at, mode, session_id):
    """
    Timer loop for a single player auction.
    - emits timer_update every second while running
    - respects paused flag in DB
    - when time expires, decides SOLD/UNSOLD using live_bids
    - emits auction_ended safely (Decimal -> float)
    - in random mode, starts next player automatically
    """
    from datetime import datetime, timezone, timedelta

    with app.app_context():
        print(f"⏱️ Timer started for player {player_id}, mode={mode}, expires_at={expires_at}")
        try:
            # Normalize expires_at to timezone-aware datetime
            if isinstance(expires_at, str):
                try:
                    expires_at = datetime.fromisoformat(expires_at)
                except Exception:
                    expires_at = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S")
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)

            while True:
                # Refresh auction row to check pause/cancel
                conn = get_db_connection()
                cursor = conn.cursor(dictionary=True)
                try:
                    cursor.execute("""
                        SELECT player_id, expires_at, paused, paused_remaining, session_id, mode
                        FROM current_auction
                        WHERE player_id=%s
                        LIMIT 1
                    """, (player_id,))
                    row = cursor.fetchone()
                finally:
                    cursor.close()
                    conn.close()

                if not row:
                    print(f"⚠️ current_auction row missing for player {player_id} — stopping timer")
                    break

                # normalize DB expires
                db_expires = row.get("expires_at") or expires_at
                if isinstance(db_expires, str):
                    try:
                        db_expires = datetime.fromisoformat(db_expires)
                    except Exception:
                        db_expires = datetime.strptime(db_expires, "%Y-%m-%d %H:%M:%S")
                if db_expires.tzinfo is None:
                    db_expires = db_expires.replace(tzinfo=timezone.utc)

                # If paused, emit paused event and wait
                if row.get("paused"):
                    rem = int(row.get("paused_remaining") or 0)
                    socketio.emit("auction_paused", safe_json({
                        "paused": True,
                        "remaining_seconds": rem
                    }), to=None)
                    socketio.sleep(1)
                    continue

                # compute remaining
                remaining = seconds_remaining(db_expires)
                if remaining > 0:
                    socketio.emit("timer_update", safe_json({
                        "remaining_seconds": int(remaining),
                        "server_time": datetime.now(timezone.utc).isoformat()
                    }), to=None)
                    socketio.sleep(1)
                    continue

                # Time expired -> finalize auction (SOLD / UNSOLD)
                conn = get_db_connection()
                cursor = conn.cursor(dictionary=True)
                try:
                    # Lock row to avoid races
                    cursor.execute("SELECT * FROM current_auction WHERE player_id=%s FOR UPDATE", (player_id,))
                    active = cursor.fetchone()
                    if not active:
                        conn.rollback()
                        print(f"⚠️ No active auction for player {player_id} at expiry")
                        break

                    if active.get("paused"):
                        # If paused while acquiring lock, skip
                        conn.commit()
                        continue

                    # Get top live bid
                    cursor.execute("""
                        SELECT b.team_id, b.bid_amount, t.name AS team_name
                        FROM live_bids b
                        JOIN teams t ON b.team_id = t.team_id
                        WHERE b.player_id = %s
                        ORDER BY b.bid_amount DESC, b.bid_time ASC
                        LIMIT 1
                    """, (player_id,))
                    top_bid = cursor.fetchone()

                    if top_bid:
                        # SOLD path
                        sold_price = float(top_bid["bid_amount"])
                        team_id = top_bid["team_id"]

                        # Deduct purse
                        cursor.execute("UPDATE teams SET purse = purse - %s WHERE team_id = %s", (sold_price, team_id))

                        # Insert sold record (using sold_on column)
                        cursor.execute("""
                            INSERT INTO sold_players (player_id, team_id, sold_price, session_id, sold_on)
                            VALUES (%s, %s, %s, %s, NOW())
                        """, (player_id, team_id, sold_price, session_id))

                        # cleanup current auction + live bids
                        cursor.execute("DELETE FROM current_auction WHERE player_id=%s", (player_id,))
                        cursor.execute("DELETE FROM live_bids WHERE player_id=%s", (player_id,))
                        conn.commit()

                        # fetch player info for payload
                        cursor.execute("SELECT id, name, category, type, image_path, base_price FROM players WHERE id=%s", (player_id,))
                        player_info = cursor.fetchone()
                        player_info = safe_json(player_info) if player_info else {"id": player_id, "name": "Unknown"}

                        socketio.emit("auction_ended", safe_json({
                            "status": "sold",
                            "player": player_info,
                            "team": {"team_id": team_id, "team_name": top_bid.get("team_name"), "bid_amount": float(top_bid["bid_amount"])},
                            "sold_price": sold_price,
                            "message": f"Player sold to {top_bid.get('team_name')} for ₹{sold_price}"
                        }), to=None)

                        print(f"✅ Player {player_id} SOLD to {top_bid.get('team_name')} for ₹{sold_price}")

                    else:
                        # UNSOLD path
                        cursor.execute("DELETE FROM current_auction WHERE player_id=%s", (player_id,))
                        cursor.execute("INSERT INTO unsold_players (player_id, reason, session_id, added_on) VALUES (%s, %s, %s, NOW())",
                                       (player_id, "No bids received", session_id))
                        cursor.execute("DELETE FROM live_bids WHERE player_id=%s", (player_id,))
                        conn.commit()

                        cursor.execute("SELECT id, name, category, type, image_path, base_price FROM players WHERE id=%s", (player_id,))
                        player_info = cursor.fetchone()
                        player_info = safe_json(player_info) if player_info else {"id": player_id, "name": "Unknown"}

                        socketio.emit("auction_ended", safe_json({
                            "status": "unsold",
                            "player": player_info,
                            "message": "No bids received – player marked unsold"
                        }), to=None)

                        print(f"🗑️ Player {player_id} marked UNSOLD")

                    # If random mode, pick next player and start it
                    if mode == "random":
                        cursor.execute("""
                            SELECT id FROM players
                            WHERE id NOT IN (
                                SELECT player_id FROM sold_players
                                UNION
                                SELECT player_id FROM unsold_players
                            )
                            ORDER BY RAND() LIMIT 1
                        """)
                        next_row = cursor.fetchone()
                        if next_row:
                            next_id = next_row["id"]
                            duration = 120
                            start_time = datetime.now(timezone.utc)
                            next_expires = start_time + timedelta(seconds=duration)

                            # create next current_auction
                            cursor.execute("""
                                INSERT INTO current_auction (player_id, start_time, expires_at, auction_duration, mode, session_id)
                                VALUES (%s, %s, %s, %s, %s, %s)
                            """, (next_id, start_time, next_expires, duration, mode, session_id))
                            conn.commit()

                            print(f"🆕 Random mode: starting next player {next_id}")
                            socketio.start_background_task(background_timer, next_id, next_expires, mode, session_id)

                            # emit auction_started
                            cursor.execute("SELECT name FROM players WHERE id=%s", (next_id,))
                            name_row = cursor.fetchone()
                            socketio.emit("auction_started", safe_json({
                                "player_id": next_id,
                                "player_name": name_row.get("name") if name_row else None,
                                "mode": mode,
                                "duration": duration,
                                "expires_at": next_expires.isoformat()
                            }), to=None)
                except Exception as e:
                    conn.rollback()
                    print(f"❌ Error in background_timer for player {player_id}: {e}")
                    break
                finally:
                    cursor.close()
                    conn.close()

                break  # end while loop after handling expiry

        except Exception as e:
            print(f"❌ Fatal background_timer error for player {player_id}: {e}")
        finally:
            print(f"🏁 Timer thread ended for player {player_id}")


def start_next_auction_internal(mode="random", session_id="default", delay_seconds=2):
    """
    Picks the next player, inserts them into current_auction,
    starts the timer, and emits auction_started.

    Runs safely inside a background task.
    """
    from datetime import datetime, timezone, timedelta
    import eventlet

    try:
        # allow sold payload to reach frontend first
        eventlet.sleep(delay_seconds)

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 🎯 Pick next available player
        cursor.execute("""
            SELECT id, name FROM players
            WHERE id NOT IN (
                SELECT player_id FROM sold_players
                UNION
                SELECT player_id FROM unsold_players
            )
            ORDER BY RAND() LIMIT 1
        """)
        next_p = cursor.fetchone()

        if not next_p:
            print("🏁 No next player available — auction finished.")
            cursor.close()
            conn.close()
            return False

        next_player_id = next_p["id"]
        next_player_name = next_p["name"]

        # Timer setup
        duration = 180
        start_time = datetime.now(timezone.utc)
        expires_at = start_time + timedelta(seconds=duration)

        # Insert into current auction
        cursor.execute("""
            INSERT INTO current_auction (player_id, start_time, expires_at, auction_duration, mode, session_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (next_player_id, start_time, expires_at, duration, mode, session_id))
        conn.commit()

        # Clear ANY leftover bids
        cursor.execute("DELETE FROM live_bids WHERE player_id = %s", (next_player_id,))
        conn.commit()

        # Start timer thread
        socketio.start_background_task(background_timer,
                                       next_player_id,
                                       expires_at,
                                       mode,
                                       session_id)

        # Emit auction started
        socketio.emit("auction_started", safe_json({
            "player_id": next_player_id,
            "player_name": next_player_name,
            "mode": mode,
            "duration": duration,
            "expires_at": expires_at.isoformat()
        }), to=None)

        print(f"🚀 Next auction started for {next_player_name} (ID {next_player_id})")

        cursor.close()
        conn.close()
        return True

    except Exception as e:
        print("❌ start_next_auction_internal error:", e)
        try:
            cursor.close()
            conn.close()
        except:
            pass
        return False


@app.route('/mark-sold', methods=['POST'])
def mark_sold():
    if 'user' not in session or session['user'].get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.get_json() or {}
    player_id = data.get('player_id')
    session_id = data.get('session_id', session.get('session_id', 'default'))

    if not player_id:
        return jsonify({'error': 'player_id required'}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Get highest live bid
        cursor.execute("""
            SELECT b.team_id, b.bid_amount, t.name AS team_name
            FROM live_bids b
            JOIN teams t ON b.team_id = t.team_id
            WHERE b.player_id = %s
            ORDER BY b.bid_amount DESC, b.bid_time ASC
            LIMIT 1
        """, (player_id,))
        top = cursor.fetchone()

        if not top:
            return jsonify({"error": "No live bids for this player"}), 404

        sold_price = float(top["bid_amount"])
        team_id = top["team_id"]
        team_name = top["team_name"]

        # Deduct purse
        cursor.execute("UPDATE teams SET purse = purse - %s WHERE team_id = %s",
                       (sold_price, team_id))

        # Insert into sold_players
        cursor.execute("""
            INSERT INTO sold_players (player_id, team_id, sold_price, session_id, sold_on)
            VALUES (%s, %s, %s, %s, NOW())
        """, (player_id, team_id, sold_price, session_id))

        # Remove from auction tables
        cursor.execute("DELETE FROM current_auction WHERE player_id = %s", (player_id,))
        cursor.execute("DELETE FROM live_bids WHERE player_id = %s", (player_id,))
        conn.commit()

        # Fetch player info for frontend
        cursor.execute("""
            SELECT id, name, category, type, image_path, base_price
            FROM players WHERE id = %s
        """, (player_id,))
        player_info = cursor.fetchone()
        player_info = safe_json(player_info) if player_info else {"id": player_id}

        # Emit sold event
        payload = safe_json({
            "status": "sold",
            "player": player_info,
            "team": {
                "team_id": team_id,
                "team_name": team_name,
                "bid_amount": sold_price
            },
            "sold_price": sold_price,
            "message": f"Player sold to {team_name} for ₹{sold_price}"
        })

        socketio.emit("auction_ended", payload, to=None)
        print(f"✅ Player {player_info.get('name')} manually SOLD to {team_name} for ₹{sold_price}")

        # ⭐ START NEXT AUCTION AUTOMATICALLY ⭐
        mode = "random"  # or fetch from current_auction before deleting it
        socketio.start_background_task(start_next_auction_internal, mode, session_id, 2)
        print("⏭️ mark_sold → scheduled next auction")

        return jsonify({"success": True, "message": "Player marked as SOLD"}), 200

    except Exception as e:
        conn.rollback()
        print("❌ Error in mark_sold:", e)
        return jsonify({"error": str(e)}), 500

    finally:
        cursor.close()
        conn.close()

@app.route('/pause-auction', methods=['POST'])
def pause_auction():
    if 'user' not in session or session['user'].get('role') != 'admin':
        return jsonify({'error': 'Forbidden'}), 403

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 🟢 Get currently active auction
        cursor.execute("SELECT player_id, expires_at, paused FROM current_auction LIMIT 1")
        auction = cursor.fetchone()
        if not auction:
            return jsonify({'error': 'No active auction found'}), 400

        if auction.get("paused"):
            return jsonify({'error': 'Auction is already paused'}), 400

        # 🕒 Calculate remaining time
        expires_at = ensure_aware_utc(auction['expires_at']) 
        if isinstance(expires_at, str):
            try:
                expires_at = datetime.fromisoformat(expires_at)
            except Exception:
                expires_at = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S")
        
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        remaining = seconds_remaining(expires_at)

        # 🔒 Update DB with paused state
        cursor.execute("""
            UPDATE current_auction
            SET paused = 1, paused_remaining = %s
            WHERE player_id = %s
        """, (remaining, auction["player_id"]))
        conn.commit()

        print(f"⏸️ Auction paused for player {auction['player_id']} with {remaining}s remaining")

        # 🔊 Notify all connected clients
        socketio.emit("auction_paused", {
            "paused": True,
            "remaining_seconds": remaining
        }, to=None)

        return jsonify({
            "message": "Auction paused successfully",
            "player_id": auction["player_id"],
            "remaining": remaining
        }), 200

    except Exception as e:
        print(f"❌ Error while pausing auction: {e}")
        conn.rollback()
        return jsonify({'error': 'Failed to pause auction'}), 500

    finally:
        cursor.close()
        conn.close()


@app.route('/resume-auction', methods=['POST'])
def resume_auction():
    if 'user' not in session or session['user'].get('role') != 'admin':
        return jsonify({'error': 'Forbidden'}), 403

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 🟢 Fetch paused auction data
        cursor.execute("""
            SELECT player_id, paused_remaining, mode, session_id
            FROM current_auction
            WHERE paused = 1
            LIMIT 1
        """)
        auction = cursor.fetchone()

        if not auction:
            return jsonify({'error': 'No paused auction found'}), 400

        remaining = auction["paused_remaining"] or 0
        if remaining <= 0:
            return jsonify({'error': 'Cannot resume. Auction time already ended.'}), 400

        # 🕒 Calculate new expiry time
        new_end_time = datetime.now(timezone.utc) + timedelta(seconds=remaining)

        # 🔄 Update DB to unpause auction
        cursor.execute("""
            UPDATE current_auction
            SET paused = 0,
                paused_remaining = NULL,
                expires_at = %s
            WHERE player_id = %s
        """, (new_end_time, auction["player_id"]))
        conn.commit()

        print(f"▶️ Auction resumed for player {auction['player_id']} — {remaining}s remaining")

        # 🧵 Start new background timer with required args
        player_id = auction["player_id"]
        mode = auction["mode"]
        session_id = auction["session_id"]

        socketio.start_background_task(background_timer, player_id, new_end_time, mode, session_id)

        # 🔊 Notify all clients immediately
        socketio.emit("auction_resumed", {
            "paused": False,
            "remaining_seconds": remaining,
            "expires_at": new_end_time.isoformat()
        }, to=None)

        return jsonify({
            "message": "Auction resumed successfully",
            "player_id": player_id,
            "remaining": remaining
        }), 200

    except Exception as e:
        print(f"❌ Error while resuming auction: {e}")
        conn.rollback()
        return jsonify({'error': 'Failed to resume auction'}), 500

    finally:
        cursor.close()
        conn.close()


def fetch_current_auction():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT ca.id AS current_id, p.*
        FROM current_auction ca
        JOIN players p ON ca.player_id = p.id
        ORDER BY ca.id DESC LIMIT 1
    """)
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


from datetime import datetime, timezone

def broadcast_auction_update():
    """
    Build a stable 'auction_update' payload and emit it to all clients.
    Uses live_bids as the source of truth for current bid.
    Converts Decimal -> float via safe_json() before emit.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM current_auction LIMIT 1")
        auction = cursor.fetchone()

        if not auction:
            socketio.emit("auction_cleared", safe_json({"message": "No active auction"}), to=None)
            return

        player_id = auction["player_id"]

        # player info
        cursor.execute("""
            SELECT id, name, category, type, image_path, base_price
            FROM players WHERE id = %s
        """, (player_id,))
        player = cursor.fetchone()
        player_safe = safe_json(player) if player else {"id": player_id, "name": "Unknown"}

        expires_at = ensure_aware_utc(auction.get("expires_at"))
        paused = bool(auction.get("paused"))
        paused_remaining = int(auction.get("paused_remaining") or 0)
        time_left = paused_remaining if paused else seconds_remaining(expires_at)

        # highest live bid (if any)
        cursor.execute("""
            SELECT b.team_id, b.bid_amount, t.name AS team_name
            FROM live_bids b
            JOIN teams t ON b.team_id = t.team_id
            WHERE b.player_id = %s
            ORDER BY b.bid_amount DESC, b.bid_time ASC
            LIMIT 1
        """, (player_id,))
        highest = cursor.fetchone()
        highest_safe = safe_json(highest) if highest else None

        current_bid = float(highest["bid_amount"]) if highest else float(player_safe.get("base_price") or 0)

        payload = {
            "player": player_safe,
            "paused": paused,
            "time_left": int(time_left),
            "highest_bid": highest_safe,
            "currentBid": float(current_bid),
            "server_time": datetime.now(timezone.utc).isoformat()
        }

        print(f"📢 Broadcasting auction_update: player={player_id} currentBid={current_bid} time_left={time_left} paused={paused}")
        socketio.emit("auction_update", safe_json(payload), to=None)

    except Exception as e:
        print(f"❌ broadcast_auction_update error: {e}")
    finally:
        cursor.close()
        conn.close()

@socketio.on("connect")
def on_connect():
    # Flask sessions are available only if same-site cookie sends. If not, require token from client.
    user = session.get("user")
    # Accept the connection, optionally check session
    if not user:
        # allow read-only connections optionally, or disconnect:
        # return False # to reject
        pass
    emit("connected", {"msg": "Connected to JPL socket", "user": user})

@socketio.on("join_auction")
def join_auction(data):
    if "user" not in session:
        emit("error", {"error": "Unauthorized"})
        return
    user = session["user"]
    team_id = user.get("team_id")
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT name, purse FROM teams WHERE team_id = %s", (team_id,))
    team = cursor.fetchone()
    conn.close()
    team_name = team["name"] if team else "Unknown"
    purse = team["purse"] if team else 0
    print(f"✅ Team {team_name} (ID: {team_id}) joined auction with purse ₹{purse}")
    emit("team_joined", safe_json({
        "team_id": team_id,
        "team_name": team_name,
        "purse": purse
    }))



@socketio.on("place_bid")
def handle_place_bid(data):
    """
    Socket handler: validates pause, team, purse, increments; writes to live_bids + history.
    Replies using emit to request.sid on rejections.
    """
    # Auth check: ensure session user present and role team
    user = session.get('user')
    if not user:
        return emit("bid_rejected", {"error": "Unauthorized"}, to=request.sid)
    if user.get("role") != "team":
        return emit("bid_rejected", {"error": "Only teams can place bids"}, to=request.sid)

    team_id = data.get("team_id")
    player_id = data.get("player_id")
    bid_amount = data.get("bid_amount")

    try:
        bid_amount = float(bid_amount)
    except Exception:
        return emit("bid_rejected", {"error": "Invalid bid amount"}, to=request.sid)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # check current auction and paused flag
        cursor.execute("SELECT * FROM current_auction LIMIT 1")
        auction = cursor.fetchone()
        if not auction:
            return emit("bid_rejected", {"error": "No active auction"}, to=request.sid)
        if auction.get("paused"):
            print("⛔ Bid REJECTED – auction is paused")
            return emit("bid_rejected", {"error": "Auction is paused"}, to=request.sid)

        # ensure client is bidding on active player
        active_player = auction["player_id"]
        if str(player_id) != str(active_player):
            return emit("bid_rejected", {"error": "Invalid player for bidding"}, to=request.sid)

        # team validation and purse (purse column used as live wallet)
        cursor.execute("SELECT team_id, name, purse FROM teams WHERE team_id = %s", (team_id,))
        team = cursor.fetchone()
        if not team:
            return emit("bid_rejected", {"error": "Team not found"}, to=request.sid)
        if float(team["purse"]) < bid_amount:
            return emit("bid_rejected", {"error": "Insufficient purse"}, to=request.sid)

        # highest live bid
        cursor.execute("SELECT MAX(bid_amount) AS highest_bid FROM live_bids WHERE player_id = %s", (active_player,))
        row = cursor.fetchone()
        highest_bid = float(row["highest_bid"]) if row and row["highest_bid"] is not None else 0.0

        MIN_INCREMENT = 500
        required = max(highest_bid + MIN_INCREMENT, float(0 if auction.get("base_price") is None else auction.get("base_price")))

        if bid_amount < required:
            return emit("bid_rejected", {"error": f"Minimum required bid is ₹{required}"}, to=request.sid)

        # Upsert into live_bids so each team has at most one live row per player
        cursor.execute("""
            INSERT INTO live_bids (player_id, team_id, bid_amount, bid_time)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
            ON DUPLICATE KEY UPDATE
              bid_amount = VALUES(bid_amount),
              bid_time = CURRENT_TIMESTAMP
        """, (active_player, team_id, bid_amount))

        # Append to historical bids table for audit/history
        cursor.execute("""
            INSERT INTO bids (player_id, team_id, bid_amount, bid_time)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
        """, (active_player, team_id, bid_amount))

        conn.commit()

        print(f"💰 Live bid accepted: Team {team_id} → ₹{bid_amount}")

        # notify bidder (ack) and broadcast to all
        emit("bid_accepted", {"message": "Bid accepted", "team_id": team_id, "bid_amount": float(bid_amount)}, to=request.sid)

        socketio.emit("bid_placed", {
            "team_id": team_id,
            "team_name": team["name"],
            "bid_amount": float(bid_amount)
        }, to=None)

        # update auction board
        broadcast_auction_update()

    except Exception as e:
        conn.rollback()
        print("⚠ place_bid error:", e)
        emit("bid_rejected", {"error": str(e)}, to=request.sid)
    finally:
        cursor.close()
        conn.close()

@socketio.on("start_auction_socket")
def handle_start_auction(data):
    # only admin allowed
    if "user" not in session or session['user'].get('role') != 'admin':
        emit("error", {"error": "Forbidden"})
        return
    player_id = data.get("player_id")
    duration = data.get("duration",600)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM current_auction")
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=duration)

        cursor.execute("INSERT INTO current_auction (player_id, expires_at) VALUES (%s, %s)", (player_id, expires_at))
        conn.commit()
        broadcast_auction_update()

        socketio.start_background_task(auto_end_auction, player_id, expires_at)
        emit("auction_started", {"player_id": player_id, "expires_at": expires_at.isoformat}, to=None)
    finally:
        cursor.close(); conn.close()

def auto_end_auction(player_id, expires_at):
    while datetime.now(timezone.utc) < expires_at:
        socketio.sleep(1)
    with app.app_context():
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM current_auction WHERE player_id=%s", (player_id,))
        active = cursor.fetchone()
        if active:
            broadcast_auction_update()
            socketio.emit("auction_ended", {"player_id": player_id}, to=None)
        cursor.close()
        conn.close()

@socketio.on("end_auction_socket")
def handle_end_auction(data):

    # admin only, or allow system logic
    if "user" not in session or session['user'].get('role') != 'admin':
        emit("error", {"error": "Forbidden"})
        return
    # re-use your end-auction logic (decide winning bid, update sold_players)
    # After concluding:
    broadcast_auction_update()
    emit("auction_ended", {"message": "Auction ended"}, to=None)

@app.route('/cancel-auction', methods=['POST'])
def cancel_auction():
    """Cancel the current auction manually (admin only). Marks player as UNSOLD and notifies clients."""
    if 'user' not in session or session['user'].get('role') != 'admin':
        return jsonify({'error': 'Forbidden'}), 403

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM current_auction LIMIT 1")
        auction = cursor.fetchone()
        if not auction:
            return jsonify({"error": "No active auction"}), 400

        player_id = auction['player_id']
        session_id = auction.get('session_id', session.get('session_id', 'default'))

        # fetch player info
        cursor.execute("SELECT id, name, category, type, image_path, base_price FROM players WHERE id = %s", (player_id,))
        player_info = cursor.fetchone()
        player_info = safe_json(player_info) if player_info else {"id": player_id, "name": "Unknown"}

        # insert unsold record
        cursor.execute("""
            INSERT INTO unsold_players (player_id, reason, session_id, added_on)
            VALUES (%s, %s, %s, NOW())
        """, (player_id, "Auction manually cancelled", session_id))

        # cleanup current auction & live bids
        cursor.execute("DELETE FROM current_auction WHERE player_id = %s", (player_id,))
        cursor.execute("DELETE FROM live_bids WHERE player_id = %s", (player_id,))
        conn.commit()

        socketio.emit("auction_ended", safe_json({
            "status": "unsold",
            "player": player_info,
            "message": "Auction cancelled — player marked unsold manually"
        }), to=None)

        print(f"🛑 Auction cancelled manually for player {player_info.get('name')} (ID {player_id})")
        return jsonify({"message": f"Auction cancelled for {player_info.get('name')}", "player": player_info}), 200

    except Exception as e:
        conn.rollback()
        print(f"❌ Error in cancel_auction: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()



def allowed_file(filename):
    return '.' in filename and \
        filename.rsplit('.',1)[1].lower() in ALLOWED_EXTENSIONS

# ✅ Database connection
def get_db_connection():
    return mysql.connector.connect(
        host="localhost",
        user="root",
        password="",   # XAMPP default (empty) – change if you set a password
        database="jpl"
    )

# ✅ Home route
@app.route('/')
def home():
    return "Welcome to JPL Backend!"

# ✅ Login
@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
    user = cursor.fetchone()

    cursor.close()
    conn.close()

    print("DEBUG: user from DB xampp",user)

    if user and bcrypt.checkpw(password.encode('utf-8'), user['password'].encode('utf-8')):
        print("DEBUG: password match success")
        session['user'] = {
            'id': user['id'],
            'email': user['email'],
            'role': user['role'],
            'team_id': user.get('team_id')
        }

        return jsonify({
            "message": "Login successful",
            "user": session['user'],
            "role": user['role'],
            "authenticated" : True
        }), 200
    else:
        print("DEBUG: password match FAIL ❌")
        return jsonify({"error": "Invalid email or password"}), 401

# ✅ Check authentication
@app.route('/check-auth', methods=['GET'])
def check_auth():
    print("DEBUG SESSION:", session)
    user = session.get('user')
    if not user:
        return jsonify({"authenticated": False}), 401

    role = user.get('role')

    # Extend response if team
    extra = {}
    if role == 'team':
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            # ✅ Use team_id column instead of id
            cursor.execute("SELECT team_id AS id, name, purse FROM teams WHERE team_id = %s", (user.get('team_id') or user.get('id'),))
            team_data = cursor.fetchone()
            if team_data:
                extra["team_id"] = team_data["id"]
                extra["team_name"] = team_data["name"]
                extra["purse"] = team_data["purse"]
        finally:
            cursor.close()
            conn.close()

    print("SESSION USER:", session.get('user'))
    return jsonify({
        "authenticated": True,
        "user": {**user, **extra},
        "role": role
    }), 200

# ✅ Logout
@app.route('/logout', methods=['POST'])
def logout():
    if 'user' in session:
        session.clear()
        return jsonify({"message": "Logged out successfully"}), 200
    else:
        return jsonify({"error": "No user logged in"}), 400

# ✅ Register
@app.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    email = data.get('email', '').strip()
    password = data.get('password', '')
    role = data.get('role', 'user')

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
    if cursor.fetchone():
        cursor.close()
        conn.close()
        return jsonify({"error": "Email already registered"}), 400

    hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    cursor.execute(
        "INSERT INTO users (email, password, role) VALUES (%s, %s, %s)",
        (email, hashed_password, role)
    )
    conn.commit()
    new_user_id = cursor.lastrowid

    cursor.close()
    conn.close()

    session['user'] = {
        'id': new_user_id,
        'email': email,
        'role': role
    }

    return jsonify({
        "message": "User registered successfully!",
        "user": session['user']
    }), 201


# ✅ Get all teams
@app.route('/teams')
def get_teams():

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM teams")
    teams = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify(teams)

@app.route('/team/<int:team_id>', methods=['GET', 'PUT'])
def manage_team(team_id):
    # ✅ Authentication check
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # -------------------------------
        # 🔹 GET Request → Fetch Team + Sold Players
        # -------------------------------
        if request.method == 'GET':
            cursor.execute("SELECT * FROM teams WHERE id = %s", (team_id,))
            team = cursor.fetchone()

            if not team:
                return jsonify({"error": "Team not found"}), 404

            # fetch sold players for this team
            cursor.execute("""
                SELECT p.*, sp.sold_price, sp.sold_time
                FROM sold_players sp
                JOIN players p ON sp.player_id = p.id
                WHERE sp.team_id = %s
            """, (team_id,))
            sold_players = cursor.fetchall()

            return jsonify({
                "team": team,
                "sold_players": sold_players
            }), 200

        # -------------------------------
        # 🔹 PUT Request → Update Team (Admin Only)
        # -------------------------------
        elif request.method == 'PUT':
            if session.get('role') != 'admin':
                return jsonify({"error": "Forbidden"}), 403

            data = request.json
            name = data.get("name")
            owner = data.get("owner")
            budget = data.get("budget")

            cursor.execute("""
                UPDATE teams SET name = %s, owner = %s, budget = %s
                WHERE id = %s
            """, (name, owner, budget, team_id))
            conn.commit()

            return jsonify({"message": "Team updated successfully"}), 200

    except mysql.connector.Error as err:
        return jsonify({"error": str(err)}), 500

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route('/add-team', methods=['POST'])
def add_team():
    # ✅ Authentication check
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    # ✅ Role check (only admin can add teams)
    if session['user']['role'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403

    name = request.form.get('teamName')
    captain = request.form.get('captain')
    team_rank = request.form.get('teamRank')
    total_budget = request.form.get('totalBudget')
    season_budget = request.form.get('seasonBudget')
    players_bought = request.form.get('playersBought')
    mobile = request.form.get('mobile')
    email = request.form.get('emailId')

    
    image_file = request.files.get('image')
    image_path = None
    if image_file and '.' in image_file.filename:
        ext = image_file.filename.rsplit('.', 1)[1].lower()
        if ext in ALLOWED_EXTENSIONS:
            filename = f"{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join(UPLOAD_FOLDER_TEAMS, filename)
            image_file.save(filepath)
            image_path = f'uploads/teams/{filename}'

    # ✅ Validation check
    if not name:
        return jsonify({"error": "Team name is required"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "INSERT INTO teams (name, captain, mobile_No, email_Id, Team_Rank, Total_Budget, Season_Budget, Players_Bought, image_path) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (name, captain, mobile, email, team_rank, total_budget, season_budget, players_bought, image_path)
        )
        conn.commit()
        return jsonify({"message": "Team added successfully!"}), 201

    except mysql.connector.IntegrityError:
        return jsonify({"error": "Team name already exists"}), 400

    except mysql.connector.Error as err:
        return jsonify({"error": str(err)}), 400

    finally:
        cursor.close()
        conn.close()

@app.route('/upload-teams', methods=['POST'])
def upload_teams():
    # ✅ Authentication check
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    # ✅ Role check (only admins can upload)
    if session['user']['role'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403


    # ✅ File presence check
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']

    # ✅ File type check
    if not file.filename.endswith('.csv'):
        return jsonify({'error': 'Only CSV files are allowed'}), 400

    stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
    csv_input = csv.reader(stream)

    conn = get_db_connection()
    cursor = conn.cursor()

    inserted, skipped = 0, 0
    for row in csv_input:
        # ✅ Row structure validation
        if len(row) < 1 or not row[0].strip():
            skipped += 1
            continue

        name = row[0].strip()
        owner = row[1].strip() if len(row) > 1 and row[1] else None
        budget = float(row[2]) if len(row) > 2 and row[2] else 0

        try:
            cursor.execute(
                "INSERT INTO teams (name, owner, budget) VALUES (%s, %s, %s)",
                (name, owner, budget)
            )
            inserted += 1
        except mysql.connector.IntegrityError:
            skipped += 1  # duplicate team names
        except mysql.connector.Error:
            skipped += 1  # other DB errors

    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({
        "message": f"Upload complete. Inserted: {inserted}, Skipped: {skipped}"
    }), 201

@app.route('/upload-player-image', methods=['POST'])
def upload_player_image():
    if 'user' not in session or session['user']['role'] != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401

    file = request.files.get('image')
    if not file or file.filename == '':
        return jsonify({'error': 'No file provided'}), 400

    if not allowed_file(file.filename):
        return jsonify({'error': 'Invalid file type'}), 400

    ext = file.filename.rsplit('.', 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(UPLOAD_FOLDER_PLAYERS, filename)
    file.save(filepath)

    return jsonify({'image_path': f'uploads/players/{filename}'}), 201

# --- Save uploaded team logo ---
@app.route('/upload-team-image', methods=['POST'])
def upload_team_image():
    if 'user' not in session or session['user']['role'] != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401

    file = request.files.get('image')
    if not file or file.filename == '':
        return jsonify({'error': 'No file provided'}), 400

    if not allowed_file(file.filename):
        return jsonify({'error': 'Invalid file type'}), 400

    ext = file.filename.rsplit('.', 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(UPLOAD_FOLDER_TEAMS, filename)
    file.save(filepath)

    return jsonify({'image_path': f'uploads/teams/{filename}'}), 201

# --- Serve player images ---
@app.route('/uploads/players/<filename>')
def serve_player_image(filename):
    return send_from_directory(UPLOAD_FOLDER_PLAYERS, filename)

# --- Serve team images ---
@app.route('/uploads/teams/<filename>')
def serve_team_image(filename):
    return send_from_directory(UPLOAD_FOLDER_TEAMS, filename)

# ✅ Get all bids
@app.route('/bids')
def get_bids():
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT 
            bids.id,
            bids.bid_amount,
            bids.bid_time,
            players.name AS player_name,
            teams.name AS team_name
        FROM live_bids
        JOIN players ON bids.player_id = players.id
        JOIN teams ON bids.team_id = teams.id
        ORDER BY bids.bid_time DESC
    """)
    bids = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify(bids)

# ✅ Add a new bid
@app.route('/add-bid', methods=['POST'])
def add_bid():
    # ✅ Authentication check
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    # ✅ Role check (only admin can add bids)
    if session['user']['role'] != 'admin':
        return jsonify({"error": "Forbidden"}), 403

    data = request.get_json()
    player_id = data.get('player_id')
    team_id = data.get('team_id')
    bid_amount = data.get('bid_amount')

    # ✅ Validation check
    if not player_id or not team_id or not bid_amount:
        return jsonify({"error": "Missing data"}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # ✅ Check if player is in current auction
        cursor.execute("SELECT player_id FROM current_auction WHERE player_id = %s", (player_id,))
        if not cursor.fetchone():
            return jsonify({"error": "Player is not in current auction"}), 400

        # ✅ Check team budget
        cursor.execute("SELECT budget FROM teams WHERE id = %s", (team_id,))
        team = cursor.fetchone()
        if not team:
            return jsonify({"error": "Team not found"}), 404
        if team['budget'] < bid_amount:
            return jsonify({"error": "Insufficient budget"}), 400

        # ✅ Duplicate bid check
        cursor.execute(
            "SELECT id FROM live_bids WHERE player_id = %s AND team_id = %s",
            (player_id, team_id)
        )
        if cursor.fetchone():
            return jsonify({"error": "Duplicate bid detected"}), 400

        # ✅ Insert new bid
        cursor.execute(
            "INSERT INTO bids (player_id, team_id, bid_amount) VALUES (%s, %s, %s)",
            (player_id, team_id, bid_amount)
        )
        conn.commit()
        return jsonify({"message": "Bid added successfully"}), 201

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        cursor.close()
        conn.close()

    
@app.route('/place-bid', methods=['POST'])
def place_bid():
    # 🔐 Auth + Role check
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized', 'status': 'error'}), 401
    if session['user']['role'] != 'team':
        return jsonify({'error': 'Only teams can place bids', 'status': 'error'}), 403

    data = request.json
    team_id = data.get('team_id')
    bid_amount = data.get('bid_amount')
    if not team_id or not bid_amount:
        return jsonify({'error': 'team_id and bid_amount are required', 'status': 'error'}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # ✅ Active auction check
        cursor.execute("SELECT * FROM current_auction LIMIT 1")
        auction = cursor.fetchone()
        if not auction:
            return jsonify({'error': 'No active auction', 'status': 'error'}), 400
        player_id = auction['player_id']

        # ✅ Player not sold
        cursor.execute("SELECT id, name, base_price FROM players WHERE id = %s", (player_id,))
        player = cursor.fetchone()
        if not player:
            return jsonify({'error': 'Player not found', 'status': 'error'}), 404
        cursor.execute("SELECT id FROM sold_players WHERE player_id = %s", (player_id,))
        if cursor.fetchone():
            return jsonify({'error': 'Player already sold', 'status': 'error'}), 400

        # ✅ Validate team & budget
        cursor.execute("SELECT id, name, budget FROM teams WHERE id = %s", (team_id,))
        team = cursor.fetchone()
        if not team:
            return jsonify({'error': 'Team not found', 'status': 'error'}), 404
        if team['budget'] < bid_amount:
            return jsonify({'error': 'Insufficient budget', 'status': 'error'}), 400

        # ✅ Highest bid check
        cursor.execute("""
            SELECT MAX(bid_amount) AS highest_bid FROM live_bids WHERE player_id = %s
        """, (player_id,))
        result = cursor.fetchone()
        highest_bid = result['highest_bid'] if result and result['highest_bid'] else 0

        # ✅ Minimum increment
        MIN_INCREMENT = 10
        min_required = max(player['base_price'], highest_bid + MIN_INCREMENT)
        if bid_amount < min_required:
            return jsonify({'error': f'Bid must be at least {min_required}', 'status': 'error'}), 400

        # ✅ Insert/Update bid
        cursor.execute("""
            INSERT INTO bids (player_id, team_id, bid_amount)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE bid_amount = VALUES(bid_amount), bid_time = CURRENT_TIMESTAMP
        """, (player_id, team_id, bid_amount))
        conn.commit()

        return jsonify({
            "message": f"Bid placed successfully by {team['name']} for {player['name']}",
            "player_id": player_id,
            "player_name": player['name'],
            "team_id": team_id,
            "team_name": team['name'],
            "bid_amount": float(bid_amount),
            "highest_bid_before": float(highest_bid),
            "highest_bid_now": float(bid_amount),
            "status": "bid_placed"
        }), 201

    except mysql.connector.Error as err:
        conn.rollback()
        return jsonify({'error': str(err), 'status': 'error'}), 500

    finally:
        cursor.close()
        conn.close()

# ✅ Get all players
@app.route('/players')
def get_players():

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM players")
    players = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify(players)

@app.route('/players-with-teams')
def get_players_with_teams():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT 
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
            GROUP_CONCAT(t.name SEPARATOR ', ') AS teams_played
        FROM players p
        LEFT JOIN player_teams pt ON p.id = pt.player_id
        LEFT JOIN teams t ON pt.team_id = t.team_id   -- ✅ FIXED HERE
        GROUP BY p.id
        ORDER BY p.name ASC
    """)

    players = cursor.fetchall()
    cursor.close()
    conn.close()

    return jsonify(players)

@app.route('/add-player', methods=['POST'])
def add_player():
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    if session['user']['role'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    print("📥 FORM DATA RECEIVED:", request.form.to_dict())

    # Extract form data
    first_name = request.form.get('playerName', '').strip()
    father_name = request.form.get('fatherName', '').strip()
    sur_name = request.form.get('surName', '').strip()
    nickname = request.form.get('nickName')
    age = request.form.get('age')
    category = request.form.get('category')
    type_ = request.form.get('style')
    base_price = request.form.get('basePrice')
    total_runs = request.form.get('totalRuns', 0)
    highest_runs = request.form.get('highestRuns', 0)
    wickets_taken = request.form.get('wickets', 0)
    times_out = request.form.get('outs', 0)
    jersey_number = request.form.get('jerseyNo')
    mobile_no = request.form.get('mobile')
    email = request.form.get('emailId')
    gender = request.form.get('gender')
    team_ids = request.form.get('teams[]') or request.form.getlist('teams') or []  # new form field
    if not team_ids:
        single_team = request.form.get('teams[]') or request.form.get('teams')
        if single_team:
            team_ids = [single_team]
    teams_played = len(team_ids)

    # ✅ Combine names safely
    full_name = " ".join(
        part for part in [first_name, father_name, sur_name] if part
    ).strip()

    # ✅ Teams (IDs from frontend)
    team_ids = request.form.getlist('teams[]')

    # ✅ Handle image
    image_file = request.files.get('image')
    image_path = None
    if image_file and '.' in image_file.filename:
        ext = image_file.filename.rsplit('.', 1)[1].lower()
        if ext in ALLOWED_EXTENSIONS:
            filename = f"{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join(UPLOAD_FOLDER_PLAYERS, filename)
            image_file.save(filepath)
            image_path = f'uploads/players/{filename}'

    if not full_name:
        return jsonify({"error": "Player full name is required"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # ✅ Insert into players table
        cursor.execute("""
            INSERT INTO players 
            (name, nickname, age, category, type, base_price, total_runs, highest_runs, 
             wickets_taken, times_out, image_path, jersey, mobile_No, email_Id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            full_name, nickname, age, category, type_, base_price, total_runs, highest_runs,
            wickets_taken, times_out, image_path, jersey_number, mobile_no, email
        ))

        player_id = cursor.lastrowid

        # ✅ Insert player-team relationships
        for team_id in team_ids:
            cursor.execute(
                "INSERT INTO player_teams (player_id, team_id) VALUES (%s, %s)",
                (player_id, team_id)
            )

        conn.commit()
        return jsonify({"message": "Player added successfully!", "player_id": player_id}), 201

    except mysql.connector.IntegrityError:
        conn.rollback()
        return jsonify({"error": "Player with same name or jersey number exists"}), 400
    except mysql.connector.Error as err:
        conn.rollback()
        return jsonify({"error": str(err)}), 500
    finally:
        cursor.close()
        conn.close()



# ✅ Upload players via CSV
@app.route('/upload-players', methods=['POST'])
def upload_players():
    # ✅ Authentication check
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    # ✅ Role check (only admins can upload)
    if session['user']['role'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403

    # ✅ File presence check
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']

    # ✅ File type check
    if not (file.filename.endswith('.csv') or file.filename.endswith('.xlsx') or file.filename.endswith('.xls')):
        return jsonify({'error': 'Only CSV or Excel files are allowed'}), 400

    try:
        # ✅ Use pandas for flexible column handling
        filename = file.filename.lower()
        if filename.endswith('.csv'):
            df = pd.read_csv(file)
        elif filename.endswith('.xlsx') or filename.endswith('.xls'):
            df = pd.read_excel(file)
        else:
            return jsonify({'error': 'Only CSV or Excel files are allowed'}), 400


        # ✅ Minimum required columns
        df.columns = df.columns.str.strip().str.lower()
        required_cols = {'name', 'base_price'}
        if not required_cols.issubset(df.columns):
            return jsonify({"error": f"CSV must include at least {required_cols}"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        inserted, updated, skipped = 0, 0, 0

        for _, row in df.iterrows():
            try:
                # Extract fields safely
                name = str(row['name']).strip() if pd.notna(row['name']) else None
                base_price = float(row['base_price']).strip() if pd.notna(row['base_price']) else None

                if not name or base_price is None:
                    skipped += 1
                    continue

                # image_file = row.get('image')
                # image_path = None
                # if image_file and '.' in image_file.filename:
                #     ext = image_file.filename.rsplit('.', 1)[1].lower()
                # if ext in ALLOWED_EXTENSIONS:
                #     filename = f"{uuid.uuid4().hex}.{ext}"
                #     filepath = os.path.join(UPLOAD_FOLDER_PLAYERS, filename)
                #     image_file.save(filepath)
                #     image_path = f'uploads/players/{filename}'

                # Optional fields
                nickname = row.get('nickname')
                age = int(row['age']) if 'age' in row and pd.notna(row['age']) else None
                category = row.get('category')
                type = row.get('type')
                mobile_no = row.get('mobile_no')
                email_Id = row.get('email')
                total_runs = int(row['total_runs']) if 'total_runs' in row and pd.notna(row['total_runs']) else 0
                highest_runs = int(row['highest_runs']) if 'highest_runs' in row and pd.notna(row['highest_runs']) else 0
                wickets_taken = int(row['wickets_taken']) if 'wickets_taken' in row and pd.notna(row['wickets_taken']) else 0
                times_out = int(row['times_out']) if 'times_out' in row and pd.notna(row['times_out']) else 0
                teams_played = row.get('teams_played')
                image_path = row.get('image_path')
                jersey_number = int(row['jersey_number']) if 'jersey_number' in row and pd.notna(row['jersey_number']) else None

                # Check if player exists
                cursor.execute("SELECT id FROM players WHERE name = %s", (name,))
                existing = cursor.fetchone()

                if existing:
                    # ✅ Update existing
                    cursor.execute("""
                        UPDATE players
                        SET base_price=%s, nickname=%s, age=%s, category=%s, type=%s,
                            total_runs=%s, highest_runs=%s, wickets_taken=%s, times_out=%s,
                            teams_played=%s, image_path=%s, jersey=%s, name=%s, mobile_no=%s, email_Id=%s
                        WHERE name=%s
                    """, (
                        base_price, nickname, age, category, type,
                        total_runs, highest_runs, wickets_taken, times_out,
                        teams_played, image_path, jersey_number, name, mobile_no, email_Id
                    ))
                    updated += 1
                else:
                    # ✅ Insert new
                    cursor.execute("""
                        INSERT INTO players 
                        (name, nickname, age, category, type, jersey, mobile_no, email_Id, base_price, total_runs, highest_runs, wickets_taken, times_out, teams_played, image_path)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        name, nickname, age, category, type, jersey_number, mobile_no, email_Id,
                        base_price, total_runs, highest_runs, wickets_taken, times_out,
                        teams_played, image_path
                    ))
                    inserted += 1

            except Exception:
                skipped += 1  # bad row, skip

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({
            "message": f"Upload complete. Inserted: {inserted}, Updated: {updated}, Skipped: {skipped}"
        }), 201

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
@app.route('/player/<int:player_id>', methods=['GET'])
def get_player(player_id):
    # ✅ Authentication check
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    conn = None
    cursor = None
    try:
        conn = get_db_connection()   # ✅ Use helper for DB connection
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM players WHERE id = %s", (player_id,))
        player = cursor.fetchone()

        if not player:
            return jsonify({"error": "Player not found"}), 404

        return jsonify(player), 200

    except mysql.connector.Error as err:
        return jsonify({"error": str(err)}), 500

    finally:
        # ✅ Ensure resources are always closed
        if cursor:
            cursor.close()
        if conn:
            conn.close()


@app.route('/start-auction', methods=['POST'])
def start_auction():
    # auth & role
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized', 'status': 'error'}), 401
    if session['user'].get('role') != 'admin':
        return jsonify({'error': 'Forbidden', 'status': 'error'}), 403

    data = request.json or {}
    mode = data.get('mode', 'manual')
    player_id = data.get('player_id')

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # pick player based on mode
        if mode == "manual":
            if not player_id:
                return jsonify({'error': 'player_id is required for manual mode'}), 400
            cursor.execute("SELECT * FROM players WHERE id = %s", (player_id,))
            player = cursor.fetchone()
        elif mode == "random":
            cursor.execute("""
                SELECT * FROM players
                WHERE id NOT IN (
                    SELECT player_id FROM sold_players
                    UNION
                    SELECT player_id FROM unsold_players
                )
                ORDER BY RAND() LIMIT 1
            """)
            player = cursor.fetchone()
        elif mode == "unsold":
            cursor.execute("""
                SELECT p.* FROM players p
                JOIN unsold_players u ON p.id = u.player_id
                WHERE p.id NOT IN (SELECT player_id FROM sold_players)
                ORDER BY u.id ASC LIMIT 1
            """)
            player = cursor.fetchone()
        else:
            return jsonify({'error': f'Invalid mode: {mode}'}), 400

        if not player:
            return jsonify({'error': 'No eligible player found for this mode'}), 404

        player_id = player['id']

        # clear any existing auction rows and live bids (clean start)
        cursor.execute("DELETE FROM current_auction")
        cursor.execute("DELETE FROM live_bids")
        conn.commit()

        # setup auction timings
        auction_duration = int(data.get('duration', 120))
        start_time = datetime.now(timezone.utc)
        expires_at = start_time + timedelta(seconds=auction_duration)
        session_id = session.get('session_id', 'default')

        cursor.execute("""
            INSERT INTO current_auction (player_id, start_time, expires_at, auction_duration, mode, session_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (player_id, start_time, expires_at, auction_duration, mode, session_id))
        conn.commit()

        # emit initial timer + auction_started
        socketio.emit("timer_update", safe_json({
            "remaining_seconds": auction_duration,
            "server_time": datetime.now(timezone.utc).isoformat()
        }), to=None)

        socketio.emit("auction_started", safe_json({
            "player_id": player_id,
            "player_name": player.get('name'),
            "mode": mode,
            "duration": auction_duration,
            "expires_at": expires_at.isoformat()
        }), to=None)

        # start background timer
        socketio.start_background_task(background_timer, player_id, expires_at, mode, session_id)

        print(f"🚀 Auction started for {player.get('name')} (mode={mode})")
        return jsonify({
            "message": f"Auction started for {player.get('name')} in {mode} mode",
            "player_id": player_id,
            "player_name": player.get('name'),
            "mode": mode,
            "start_time": start_time.isoformat(),
            "expires_at": expires_at.isoformat(),
            "duration": auction_duration,
            "status": "auction_started"
        }), 201

    except Exception as e:
        conn.rollback()
        print("❌ Error in /start-auction:", e)
        return jsonify({'error': str(e), 'status': 'error'}), 500
    finally:
        cursor.close()
        conn.close()

@app.route("/auction-status")
def auction_status():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM current_auction LIMIT 1")
    auction = cursor.fetchone()
    cursor.close()
    conn.close()

    if auction:
        return jsonify({"active": True, "player": auction})
    else:
        return jsonify({"active": False})

@app.route('/auction-time', methods=['GET'])
def get_timer():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("SELECT start_time, expires_at FROM current_auction LIMIT 1")
        auction = cursor.fetchone()
        if not auction:
            return jsonify({"active": False, "timeLeft": 0}), 200

        start_time = ensure_aware_utc(auction.get("start_time"))
        expires_at = ensure_aware_utc(auction.get("expires_at"))

        if isinstance(start_time, str):
            try:
                start_time = datetime.fromisoformat(start_time)
            except Exception:
                start_time = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)

        remaining = seconds_remaining(expires_at)
        elapsed= 0
        if start_time:
            elapsed = max(0, int((datetime.now(timezone.utc) - start_time).total_seconds()))

        return jsonify({
            "active": remaining > 0,
            "timeLeft": remaining,
            "elapsed": int(elapsed)
        }), 200
    finally:
        cursor.close()
        conn.close()

# ✅ Get current auction player
@app.route('/current-auction', methods=['GET'])
def get_current_auction():
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    user = session['user']
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Fetch current auction + player details
        cursor.execute("""
            SELECT 
                ca.player_id,
                ca.start_time,
                ca.expires_at,
                ca.auction_duration,
                ca.paused,
                ca.paused_remaining,
                p.name, p.image_path, p.jersey,
                p.category, p.type, p.base_price
            FROM current_auction ca
            JOIN players p ON ca.player_id = p.id
            LIMIT 1
        """)
        auction = cursor.fetchone()

        if not auction:
            return jsonify({
                "status": "no_active_auction",
                "message": "No auction currently active"
            }), 200

        # base price safe
        base_price = auction["base_price"] or 0
        if isinstance(base_price, Decimal):
            base_price = float(base_price)

        # Remaining time
        expires_at = ensure_aware_utc(auction["expires_at"])
        paused = bool(auction.get("paused"))
        paused_remaining = int(auction.get("paused_remaining") or 0)

        remaining = paused_remaining if paused else seconds_remaining(expires_at)

        # ✨ MOST IMPORTANT FIX — USE live_bids
        cursor.execute("""
            SELECT b.team_id, t.name AS team_name, b.bid_amount
            FROM live_bids b
            JOIN teams t ON b.team_id = t.team_id
            WHERE b.player_id = %s
            ORDER BY b.bid_amount DESC, b.bid_time ASC
            LIMIT 1
        """, (auction['player_id'],))
        top_bid = cursor.fetchone()

        # FIX currentBid
        if top_bid:
            current_bid = float(top_bid["bid_amount"])
        else:
            current_bid = float(base_price)

        # Team Balance
        team_balance = 0
        if user.get("role") == "team":
            cursor.execute("SELECT purse FROM teams WHERE team_id = %s", (user["team_id"],))
            row = cursor.fetchone()
            if row:
                team_balance = float(row["purse"])

        # Next bid buttons
        next_steps = [
            current_bid + 500,
            current_bid + 1000,
            current_bid + 1500
        ]

        return jsonify({
            "status": "auction_active",
            "player": {
                "id": auction["player_id"],
                "name": auction["name"],
                "jersey": auction["jersey"],
                "category": auction["category"],
                "type": auction["type"],
                "image_path": auction["image_path"],
                "base_price": base_price
            },
            "currentBid": current_bid,
            "highest_bid": safe_json(top_bid) if top_bid else None,
            "remaining_seconds": remaining,
            "auction_duration": auction["auction_duration"],
            "teamBalance": team_balance,
            "nextSteps": next_steps,
            "paused": paused,
            "canBid": user.get("role") == "team",
            "history": []
        }), 200

    except Exception as e:
        print("❌ Error in /current-auction:", str(e))
        return jsonify({"error": str(e)}), 500

    finally:
        cursor.close()
        conn.close()
    

@app.route('/next-auction', methods=['POST'])
def next_auction():
    # 🔐 Auth check
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    if session['user'].get('role') != 'admin':
        return jsonify({"error": "Forbidden"}), 403

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # 1️⃣ Get current auction player
        cursor.execute("SELECT player_id, session_id, mode FROM current_auction LIMIT 1")
        current = cursor.fetchone()

        if not current:
            return jsonify({"error": "No active auction found"}), 400

        player_id = current["player_id"]
        session_id = current.get("session_id", "default")
        mode = current.get("mode", "random")

        # 2️⃣ Get highest live bid (if exists)
        cursor.execute("""
            SELECT b.team_id, b.bid_amount, t.name AS team_name
            FROM live_bids b
            JOIN teams t ON b.team_id = t.team_id
            WHERE b.player_id = %s
            ORDER BY b.bid_amount DESC, b.bid_time ASC
            LIMIT 1
        """, (player_id,))
        raw_top_bid = cursor.fetchone()

        top_bid = safe_json(raw_top_bid) if raw_top_bid else None

        # 3️⃣ Fetch player info for sold.js
        cursor.execute("""
            SELECT id, name, category, type, image_path, base_price
            FROM players WHERE id = %s
        """, (player_id,))
        raw_player = cursor.fetchone()
        player_info = safe_json(raw_player) if raw_player else {
            "id": player_id,
            "name": "Unknown",
            "image_path": None
        }

        # 4️⃣ SOLD OR UNSOLD LOGIC
        if top_bid:
            sold_price = float(top_bid["bid_amount"])
            team_id = top_bid["team_id"]

            # Save sold record
            cursor.execute("""
                INSERT INTO sold_players (player_id, team_id, sold_price, session_id, sold_on)
                VALUES (%s, %s, %s, %s, NOW())
            """, (player_id, team_id, sold_price, session_id))

            sold_status = "sold"
            sold_msg = f"Player sold to {top_bid['team_name']} for ₹{sold_price}"

            print(f"✅ Player {player_info['name']} SOLD to {top_bid['team_name']}")

        else:
            cursor.execute("""
                INSERT INTO unsold_players (player_id, reason, session_id, added_on)
                VALUES (%s, %s, %s, NOW())
            """, (player_id, "No bids received", session_id))

            sold_status = "unsold"
            sold_msg = "No bids received – player marked unsold"

            sold_price = None
            team_id = None

            print(f"🗑 Player {player_info['name']} marked UNSOLD")

        # 5️⃣ Clear current auction + all live bids
        cursor.execute("DELETE FROM current_auction WHERE player_id = %s", (player_id,))
        cursor.execute("DELETE FROM live_bids WHERE player_id = %s", (player_id,))
        conn.commit()

        # 6️⃣ EMIT auction_ended TO FRONTEND (NO MORE CRASHING)
        payload = {
            "status": sold_status,
            "player": player_info,
            "team": {
                "team_id": team_id,
                "team_name": top_bid["team_name"],
                "bid_amount": sold_price,
            } if top_bid else None,
            "sold_price": sold_price,
            "message": sold_msg
        }

        socketio.emit("auction_ended", safe_json(payload), to=None)
        print(f"📢 auction_ended emitted → {sold_status}")

        # 7️⃣ WAIT BEFORE NEXT PLAYER
        print("⏳ Waiting 10 seconds before next player...")
        eventlet.sleep(10)

        # 8️⃣ PICK NEXT PLAYER
        cursor.execute("""
            SELECT id, name FROM players
            WHERE id NOT IN (
                SELECT player_id FROM sold_players
                UNION
                SELECT player_id FROM unsold_players
            )
            ORDER BY RAND() LIMIT 1
        """)
        next_player = cursor.fetchone()

        if not next_player:
            print("🏁 All players completed.")
            return jsonify({"message": "All players completed"}), 200

        next_player_id = next_player["id"]

        # 9️⃣ CREATE NEXT AUCTION SLOT
        duration = 180
        start_time = datetime.now(timezone.utc)
        expires_at = start_time + timedelta(seconds=duration)

        cursor.execute("""
            INSERT INTO current_auction (player_id, start_time, expires_at, auction_duration, mode, session_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            next_player_id,
            start_time,
            expires_at,
            duration,
            mode,
            session_id
        ))
        conn.commit()

        # 🔟 Start timer for the next player
        socketio.start_background_task(background_timer,
                                       next_player_id,
                                       expires_at,
                                       mode,
                                       session_id)

        # 1️⃣1️⃣ Notify frontend auction started
        socketio.emit("auction_started", safe_json({
            "player_id": next_player_id,
            "player_name": next_player["name"],
            "mode": mode,
            "duration": duration,
            "expires_at": expires_at.isoformat()
        }), to=None)

        print(f"🚀 Next player started → {next_player['name']}")

        return jsonify({
            "status": "auction_moved",
            "message": f"Moved to next player ({next_player['name']})"
        }), 200

    except Exception as e:
        conn.rollback()
        print("❌ Error in next_auction:", e)
        return jsonify({"error": str(e)}), 500

    finally:
        cursor.close()
        conn.close()

    
@app.route('/end-auction', methods=['POST'])
def end_auction():
    # 🔐 Authentication + Role Check
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized', 'status': 'error'}), 401
    if session['user'].get('role') != 'admin':
        return jsonify({'error': 'Forbidden', 'status': 'error'}), 403

    data = request.json or {}
    force_clear = data.get('force_clear', False)
    team_id = data.get('team_id')
    sold_price = data.get('sold_price')

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # 🔍 Get current auction player
        cursor.execute("SELECT * FROM current_auction LIMIT 1")
        auction = cursor.fetchone()
        if not auction:
            if force_clear:
                cursor.execute("DELETE FROM current_auction")
                conn.commit()
                return jsonify({"message": "Auction forcefully cleared", "status": "cleared"}), 200
            return jsonify({'error': 'No active auction', 'status': 'error'}), 400

        player_id = auction['player_id']

        # 🧾 Get player details
        cursor.execute("SELECT id, name, base_price, image_path FROM players WHERE id = %s", (player_id,))
        player = cursor.fetchone()

        if not player:
            return jsonify({'error': 'Player not found', 'status': 'error'}), 404

        status = "unsold"
        team_name = None
        message = ""
        timestamp = datetime.now(timezone.utc)

        # ⚙️ Case 1: Admin forces auction clear
        if force_clear:
            cursor.execute("DELETE FROM current_auction")
            conn.commit()

            # Log into auction history
            cursor.execute("""
                INSERT INTO auction_history (player_id, team_id, sold_price, status, ended_at)
                VALUES (%s, NULL, NULL, %s, %s)
            """, (player_id, "force_clear", timestamp))
            conn.commit()

            socketio.emit("auction_ended", {
                "message": "Auction forcefully cleared",
                "player_id": player_id,
                "status": "unsold"
            }, to=None)
            return jsonify({"message": "Auction cleared", "player": player, "status": "unsold"}), 200

        # ⚙️ Case 2: Admin directly assigns a team
        if team_id and sold_price:
            cursor.execute("SELECT id, name FROM teams WHERE id = %s", (team_id,))
            team = cursor.fetchone()
            if not team:
                return jsonify({'error': 'Team not found', 'status': 'error'}), 404

            cursor.execute("""
                INSERT INTO sold_players (player_id, team_id, sold_price)
                VALUES (%s, %s, %s)
            """, (player_id, team_id, sold_price))
            cursor.execute("UPDATE teams SET budget = budget - %s WHERE id = %s", (sold_price, team_id))

            status, team_name, message = "sold", team['name'], f"Player sold directly to {team['name']} for ₹{sold_price}"

        else:
            # ⚙️ Case 3: Normal bidding flow
            cursor.execute("""
                SELECT b.team_id, b.bid_amount, t.name AS team_name
                FROM live_bids b
                JOIN teams t ON b.team_id = t.team_id
                WHERE b.player_id = %s
                ORDER BY b.bid_amount DESC, b.bid_time ASC
                LIMIT 1
            """, (player_id,))
            highest = cursor.fetchone()

            if highest:
                team_id, sold_price, team_name = highest['team_id'], highest['bid_amount'], highest['team_name']
                cursor.execute("""
                    INSERT INTO sold_players (player_id, team_id, sold_price)
                    VALUES (%s, %s, %s)
                """, (player_id, team_id, sold_price))
                cursor.execute("UPDATE teams SET budget = budget - %s WHERE id = %s", (sold_price, team_id))
                message, status = f"Player sold via auction to {team_name} for ₹{sold_price}", "sold"
            else:
                message = "Auction ended with no bids"
                status = "unsold"

        # 🧹 Clear active auction + bids
        cursor.execute("DELETE FROM current_auction WHERE player_id = %s", (player_id,))
        cursor.execute("DELETE FROM live_bids WHERE player_id = %s", (player_id,))

        # 🧾 Insert record into auction_history
        cursor.execute("""
            INSERT INTO auction_history (player_id, team_id, sold_price, status, ended_at)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            player_id,
            team_id if status == "sold" else None,
            sold_price if status == "sold" else None,
            status,
            timestamp
        ))
        conn.commit()

        # 🧨 Stop any ongoing timer
        global auction_timer
        auction_timer.update({"active": False, "paused": False, "end_time": None, "remaining_seconds": 0})

        # 📢 Broadcast result
        socketio.emit("auction_ended", {
            "message": message,
            "player_id": player_id,
            "team_id": team_id if status == "sold" else None,
            "team_name": team_name,
            "sold_price": float(sold_price) if status == "sold" else None,
            "status": status
        }, to=None)

        return jsonify({
            "message": message,
            "player_id": player_id,
            "team_id": team_id if status == "sold" else None,
            "team_name": team_name,
            "sold_price": float(sold_price) if status == "sold" else None,
            "status": status
        }), 200

    except mysql.connector.Error as err:
        conn.rollback()
        return jsonify({'error': str(err), 'status': 'error'}), 500

    finally:
        cursor.close()
        conn.close()

# Reset Auction
@app.route('/reset-auction', methods=['POST'])
def reset_auction():
    """Admin-only: Reset current auction and all related bids safely."""
    
    # 🔐 Authentication & Role check (Option 2)
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized', 'status': 'error'}), 401
    if session['user'].get('role') != 'admin':
        return jsonify({'error': 'Forbidden', 'status': 'error'}), 403

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # 1️⃣ Check if a player is currently in auction
        cursor.execute("SELECT player_id FROM current_auction LIMIT 1")
        row = cursor.fetchone()
        if row:
            player_id = row['player_id']
            # Clear bids for the current player only
            cursor.execute("DELETE FROM live_bids WHERE player_id = %s", (player_id,))
        
        # 2️⃣ Clear current auction
        cursor.execute("DELETE FROM current_auction")
        conn.commit()

        return jsonify({
            "message": "Auction reset successfully",
            "player_cleared": row['player_id'] if row else None,
            "status": "reset"
        }), 200

    except mysql.connector.Error as err:
        conn.rollback()
        return jsonify({'error': str(err), 'status': 'error'}), 500

    finally:
        cursor.close()
        conn.close()

# Sold 
@app.route('/sold-players', methods=['GET'])
def sold_players():
    """List sold players with their teams and prices (with pagination)."""

    # ✅ Authentication check
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    # ✅ Pagination
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 10))
    offset = (page - 1) * limit

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Total count for pagination
        cursor.execute("SELECT COUNT(*) AS cnt FROM sold_players")
        total = cursor.fetchone()['cnt']

        # Fetch paginated sold players
        cursor.execute(
            """
            SELECT sp.id, sp.player_id, p.name AS player_name, 
                   sp.team_id, t.name AS team_name,
                   sp.sold_price, sp.sold_time
            FROM sold_players sp
            JOIN players p ON sp.player_id = p.id
            JOIN teams t ON sp.team_id = t.team_id
            ORDER BY sp.sold_time DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset)
        )
        rows = cursor.fetchall()

        return jsonify({
            "data": rows,
            "meta": {
                "page": page,
                "limit": limit,
                "total": total,
                "pages": (total + limit - 1) // limit
            }
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        cursor.close()
        conn.close()

# ✅ Run app
if __name__ == '__main__':
    print("🚀 Starting JPL backend with Eventlet...")
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, use_reloader=False)
