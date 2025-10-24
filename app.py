# ---- eventlet must be first ----
import eventlet
eventlet.monkey_patch()

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
    SESSION_COOKIE_HTTPONLY=True
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


def background_timer():
    global auction_timer
    while auction_timer["active"]:
        if auction_timer["end_time"] and not auction_timer["paused"]:
            now = datetime.now(timezone.utc)
            remaining = (auction_timer["end_time"] - now).total_seconds()

            if remaining <= 0:
                remaining = 0
                auction_timer["active"] = False
                auction_timer["remaining_seconds"] = 0
                socketio.emit("timer_update", 0, to=None)

                # ✅ Move player to unsold table safely
                try:
                    conn = get_db_connection()
                    cursor = conn.cursor(dictionary=True)

                    cursor.execute("SELECT player_id FROM current_auction LIMIT 1")
                    current = cursor.fetchone()

                    if current and "player_id" in current:
                        player_id = current["player_id"]

                        cursor.execute("""
                            INSERT INTO unsold_players (player_id, reason, auction_round, added_on)
                            VALUES (%s, %s, %s, NOW())
                        """, (player_id, "No bids placed", 1))
                        conn.commit()

                        # ✅ Remove player from current_auction table after moving to unsold
                        cursor.execute("DELETE FROM current_auction WHERE player_id = %s", (player_id,))
                        conn.commit()
                        print(f"🗑️ Player {player_id} removed from current_auction after being marked UNSOLD")

                        
                        cursor.execute("""
                            SELECT p.id, p.name, p.image_path, p.base_price
                            FROM players p
                            WHERE p.id = %s
                        """, (player_id,))
                        player_data = cursor.fetchone()

                        socketio.emit("auction_ended", safe_json({
                            "status": "unsold",
                            "message": "No bids were placed. Player moved to Unsold list.",
                            "player": player_data
                        }), to=None)
                        print(f"✅ Player {player_id} moved to unsold_players table.")

                    else:
                        print("⚠️ No player found in current_auction — skipping insert.")

                    cursor.close()
                    conn.close()

                except Exception as e:
                    print(f"❌ Timer thread error: {e}")

                break

            auction_timer["remaining_seconds"] = int(remaining)
            socketio.emit("timer_update", int(remaining), to=None)

        socketio.sleep(1)



@app.route('/pause-auction', methods=['POST'])
def pause_auction():
    if 'user' not in session or session['user'].get('role') != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    if not auction_timer["active"]:
        return jsonify({'error': 'No active auction'}), 400

    # Freeze remaining time
    now = datetime.now(timezone.utc)
    if auction_timer["end_time"]:
        auction_timer["remaining_seconds"] = max(0, int((auction_timer["end_time"] - now).total_seconds()))
    auction_timer["paused"] = True

    socketio.emit("auction_paused", {"remaining": auction_timer["remaining_seconds"]}, to=None)
    return jsonify({"message": "Auction paused", "remaining": auction_timer["remaining_seconds"]}), 200


@app.route('/resume-auction', methods=['POST'])
def resume_auction():
    if 'user' not in session or session['user'].get('role') != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    if not auction_timer["paused"]:
        return jsonify({'error': 'Auction is not paused'}), 400

    # Reset end_time based on remaining_seconds
    auction_timer["end_time"] = datetime.now(timezone.utc) + timedelta(seconds=auction_timer["remaining_seconds"])
    auction_timer["paused"] = False

    socketio.emit("auction_resumed", {"remaining": auction_timer["remaining_seconds"]}, to=None)
    return jsonify({"message": "Auction resumed"}), 200


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

def broadcast_auction_update():
    """Send updated highest bid and auction player to all connected clients"""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM current_auction LIMIT 1")
    auction = cursor.fetchone()
    if not auction:
        # auction cleared
        socketio.emit("auction_cleared", {"message": "No active auction"})
        cursor.close(); conn.close(); return

    player_id = auction['player_id']
    # Get players info
    cursor.execute("SELECT * FROM players WHERE id = %s", (player_id,))
    player = cursor.fetchone()

    # Get highest bid
    cursor.execute("""SELECT b.team_id, b.bid_amount, t.name AS team_name
                      FROM bids b JOIN teams t ON b.team_id = t.id
                      WHERE b.player_id = %s ORDER BY b.bid_amount DESC, b.bid_time ASC LIMIT 1""",
                   (player_id,))
    highest = cursor.fetchone()
    cursor.close()
    conn.close()

    payload = {"player": player, "highest_bid": highest}
    socketio.emit("auction_update", payload)

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

@socketio.on('join_auction')
def handle_join_auction(data):
    team_id = data.get('team_id')
    team_name = data.get('team_name')
    purse = data.get('purse')
    
    sid = request.sid
    print(f"✅ Team {team_name} (ID: {team_id}) joined auction with purse ₹{purse}")
    join_room("auction_room")

    emit("joined_auction", {"message": f"{team_name} joined successfully"}, room="auction_room")

@socketio.on("place_bid")
def handle_place_bid(data):
    """
    data expected: {team_id, bid_amount}
    Use session to find user/team, validate, insert DB, broadcast
    """
    # auth check
    if "user" not in session:
        emit("error", {"error": "Unauthorized"})
        return

    user = session['user']
    # Basic validation
    team_id = data.get("team_id")
    bid_amount = data.get("bid_amount")
    # add more validation (numeric, positive)
    try:
        bid_amount = float(bid_amount)
    except:
        emit("error", {"error": "Invalid bid amount"})
        return

    # Insert or update bid (use existing place-bid logic but via socket)
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # Check current auction
        cursor.execute("SELECT * FROM current_auction LIMIT 1")
        auction = cursor.fetchone()
        if not auction:
            emit("error", {"error": "No active auction"})
            return
        player_id = auction['player_id']

        # Check team budget
        cursor.execute("SELECT id, name, budget FROM teams WHERE id = %s", (team_id,))
        team = cursor.fetchone()
        if not team:
            emit("error", {"error": "Team not found"})
            return
        if team['budget'] < bid_amount:
            emit("error", {"error": "Insufficient budget"})
            return

        # Get highest bid
        cursor.execute("SELECT MAX(bid_amount) AS highest_bid FROM bids WHERE player_id = %s", (player_id,))
        result = cursor.fetchone()
        highest_bid = result['highest_bid'] or 0
        MIN_INCREMENT = 10
        min_required = max(0, highest_bid + MIN_INCREMENT)

        if bid_amount < min_required:
            emit("error", {"error": f"Minimum required bid is {min_required}"})
            return

        # Insert or update
        cursor.execute("""
            INSERT INTO bids (player_id, team_id, bid_amount)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE bid_amount = VALUES(bid_amount), bid_time = CURRENT_TIMESTAMP
        """, (player_id, team_id, bid_amount))
        conn.commit()

        # Broadcast updated auction state
        broadcast_auction_update()
        emit("bid_placed", {"status": "ok", "team_id": team_id, "bid_amount": bid_amount}, to=None)
    except Exception as e:
        conn.rollback()
        emit("error", {"error": str(e)})
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

def broadcast_auction_update():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM current_auction LIMIT 1")
    auction = cursor.fetchone()
    if not auction:
        socketio.emit("auction_cleared", {"message": "No Active auction"})
        cursor.close()
        conn.close()
        return
    
    player_id = auction["player_id"]
    expires_at = auction["expires_at"]

    cursor.execute("SELECT * FROM players WHERE id=%s", (player_id,))
    player = cursor.fetchone()

    cursor.execute("""SELECT b.team_id, b.bid_amount, t.name AS team_name
                   FROM bids b JOIN teams t ON b.team_id = t.id
                   WHERE b.player_id=%s ORDER BY b.bid_amount DESC LIMIT 1""", (player_id,))
    highest = cursor.fetchone()
    cursor.close()
    conn.close()

    time_left = None
    if expires_at:
        expires_dt = expires_at if isinstance(expires_at, datetime) else datetime.fromisoformat(str(expires_at))
        time_left = max(0, int((expires_dt - datetime.now(timezone.utc)).total_seconds()))

    
    payload = {
        "player": player,
        "highest_bid": highest,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "time_left": time_left
    }
    socketio.emit("auction_update", payload)

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

        # ❌ Stop timer
        global auction_timer
        auction_timer["active"] = False
        auction_timer["paused"] = False
        auction_timer["end_time"] = None
        auction_timer["remaining_seconds"] = 0

        # ➕ Optionally log player as unsold
        cursor.execute("INSERT INTO unsold_players (player_id) VALUES (%s)", (player_id,))
        cursor.execute("DELETE FROM current_auction WHERE player_id = %s", (player_id,))
        conn.commit()

        # 🔔 Notify all clients
        socketio.emit("auction_cancelled", {
            "message": "Auction cancelled",
            "player_id": player_id
        }, to=None)

        return jsonify({"message": "Auction cancelled", "player_id": player_id}), 200

    except Exception as e:
        conn.rollback()
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
        FROM bids
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
            "SELECT id FROM bids WHERE player_id = %s AND team_id = %s",
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
            SELECT MAX(bid_amount) AS highest_bid FROM bids WHERE player_id = %s
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
    global auction_timer, timer_thread

    # 🔐 Auth + Role check
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized', 'status': 'error'}), 401
    if session['user'].get('role') != 'admin':
        return jsonify({'error': 'Forbidden', 'status': 'error'}), 403

    data = request.json or {}
    mode = data.get('mode', 'manual')  # Default: manual
    player_id = data.get('player_id')  # Used only for manual mode

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # 🎯 Determine which player to start auction for based on mode
        if mode == "manual":
            if not player_id:
                return jsonify({'error': 'player_id is required for manual mode'}), 400
            cursor.execute("SELECT * FROM players WHERE id = %s", (player_id,))
            player = cursor.fetchone()

        elif mode == "random":
            cursor.execute("""
                SELECT * FROM players
                WHERE id NOT IN (SELECT player_id FROM sold_players)
                ORDER BY RAND() LIMIT 1
            """)
            player = cursor.fetchone()

        elif mode == "unsold":
            cursor.execute("""
                SELECT * FROM players
                WHERE id NOT IN (SELECT player_id FROM sold_players)
                AND id IN (SELECT player_id FROM auction_history WHERE status = 'unsold')
                ORDER BY RAND() LIMIT 1
            """)
            player = cursor.fetchone()

        elif mode == "custom":
            cursor.execute("""
                SELECT * FROM players
                WHERE category = 'All-Rounder'
                AND id NOT IN (SELECT player_id FROM sold_players)
                ORDER BY RAND() LIMIT 1
            """)
            player = cursor.fetchone()

        else:
            return jsonify({'error': f'Invalid mode: {mode}'}), 400

        if not player:
            return jsonify({'error': 'No eligible player found for this mode'}), 404

        player_id = player['id']

        # 🧹 Clear any existing auction
        cursor.execute("DELETE FROM current_auction")

        # ⏱️ Auction timing setup
        auction_duration = 600  # 10 minutes
        start_time = datetime.now(timezone.utc)
        expires_at = start_time + timedelta(seconds=auction_duration)

        # 💾 Save auction state in DB
        cursor.execute("""
            INSERT INTO current_auction (player_id, start_time, expires_at, auction_duration)
            VALUES (%s, %s, %s, %s)
        """, (player_id, start_time, expires_at, auction_duration))
        conn.commit()

        # 🧠 Update in-memory timer (for live countdown)
        auction_timer.update({
            "end_time": expires_at,
            "active": True,
            "paused": False,
            "remaining_seconds": auction_duration
        })

        # 🧵 Safely start background timer task
        def run_timer():
            try:
                background_timer()
            except Exception as e:
                print("❌ Timer thread error:", e)

        with thread_lock:
            if not timer_thread or not getattr(timer_thread, "_running", False):
                timer_thread = socketio.start_background_task(run_timer)
                timer_thread._running = True

        # 📡 Notify all connected clients
        socketio.emit("auction_started", {
            "player_id": player_id,
            "player_name": player['name'],
            "mode": mode,
            "duration": auction_duration
        }, to=None)

        return jsonify({
            "message": f"Auction started for {player['name']} in {mode} mode",
            "player_id": player_id,
            "player_name": player['name'],
            "mode": mode,
            "start_time": start_time.isoformat(),
            "expires_at": expires_at.isoformat(),
            "duration": auction_duration,
            "status": "auction_started"
        }), 201

    except Exception as e:
        print("❌ Error in /start-auction:", str(e))
        conn.rollback()
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
    conn = get_db_connection
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("Select start_time, duration FROM current_auction LIMIT 1")
        auction = cursor.fetchone()
        if not auction:
            return jsonify({"active": False, "timeLeft": 0}), 200
        
        start_time = auction["start_time"]
        duration = auction["duration"]

        # Calculate remaining time
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        remaining = (auction['expires_at'] - elapsed).total_seconds()

        return jsonify({
            "active": remaining > 0,
            "timeLeft": remaining
        }), 200
    finally:
        cursor.close()
        conn.close()

# ✅ Get current auction player
@app.route('/current-auction', methods=['GET'])
def get_current_auction():
    # ✅ Step 1: Session check
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    user = session['user']
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # ✅ Step 2: Get current auction + player details
        cursor.execute("""
            SELECT 
                ca.player_id, 
                ca.start_time, 
                ca.expires_at, 
                ca.auction_duration,
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

        # ✅ Step 3: Calculate remaining time
        now = datetime.now(timezone.utc)
        expires_at = auction.get('expires_at')
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        remaining = max(0, int((expires_at - now).total_seconds())) if expires_at else 0

        # ✅ Step 4: Get highest bid if exists
        cursor.execute("""
            SELECT b.team_id, t.name AS team_name, b.bid_amount
            FROM bids b
            JOIN teams t ON b.team_id = t.team_id
            WHERE b.player_id = %s
            ORDER BY b.bid_amount DESC
            LIMIT 1
        """, (auction['player_id'],))
        top_bid = cursor.fetchone()

        # ✅ Step 5: Safely handle base price & current bid
        base_price = auction.get('base_price') or 0
        if isinstance(base_price, Decimal):
            base_price = float(base_price)

        current_bid = top_bid['bid_amount'] if top_bid else base_price
        if isinstance(current_bid, Decimal):
            current_bid = float(current_bid)

        # ✅ Step 6: Initialize team balance for both admin/team users
        team_balance = 0.0
        if user.get('role') == 'team':
            cursor.execute("SELECT purse FROM teams WHERE team_id = %s", (user.get('team_id'),))
            team = cursor.fetchone()
            if team and team.get('purse') is not None:
                team_balance = float(team['purse'])

        # ✅ Step 7: Define next bid increments safely
        next_steps = [current_bid + 500, current_bid + 1000, current_bid + 1500]

        # ✅ Step 8: Build the final JSON response
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
            "remaining_seconds": remaining,
            "auction_duration": auction["auction_duration"],
            "teamBalance": team_balance,
            "nextSteps": next_steps,
            "canBid": user.get("role") == "team",
            "history": []  # you can add bid history later if needed
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

    data = request.get_json() or {}
    mode = data.get('mode', 'manual')
    player_id = data.get('player_id')  # optional for manual mode

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # ✅ Determine next player based on mode
        if mode == "manual":
            if not player_id:
                return jsonify({'error': 'player_id required for manual mode'}), 400
            cursor.execute("SELECT * FROM players WHERE id = %s", (player_id,))
            next_player = cursor.fetchone()

        elif mode == "random":
            cursor.execute("""
                SELECT * FROM players
                WHERE id NOT IN (SELECT player_id FROM sold_players)
                ORDER BY RAND() LIMIT 1
            """)
            next_player = cursor.fetchone()

        elif mode == "unsold":
            cursor.execute("""
                SELECT * FROM players
                WHERE id NOT IN (SELECT player_id FROM sold_players)
                AND id IN (SELECT player_id FROM auction_history WHERE status = 'unsold')
                ORDER BY RAND() LIMIT 1
            """)
            next_player = cursor.fetchone()

        elif mode == "custom":
            # Example: load custom-filtered player
            cursor.execute("""
                SELECT * FROM players
                WHERE category = 'All-Rounder' AND id NOT IN (SELECT player_id FROM sold_players)
                ORDER BY RAND() LIMIT 1
            """)
            next_player = cursor.fetchone()

        else:
            return jsonify({'error': f'Invalid mode: {mode}'}), 400

        if not next_player:
            return jsonify({"error": "No available player found for next auction"}), 404

        next_player_id = next_player["id"]

        # ✅ Clear existing auction before inserting next
        cursor.execute("DELETE FROM current_auction")

        auction_duration = 600  # 10 min
        start_time = datetime.now(timezone.utc)
        expires_at = start_time + timedelta(seconds=auction_duration)

        # ✅ Insert new auction
        cursor.execute("""
            INSERT INTO current_auction (player_id, start_time, expires_at, auction_duration)
            VALUES (%s, %s, %s, %s)
        """, (next_player_id, start_time, expires_at, auction_duration))
        conn.commit()

        # 🧵 Restart background timer
        global timer_thread
        with thread_lock:
            if not timer_thread or not getattr(timer_thread, 'running', lambda: False)():
                timer_thread = socketio.start_background_task(background_timer)

        # 📢 Notify all connected clients
        socketio.emit("auction_started", {
            "player_id": next_player_id,
            "player_name": next_player["name"],
            "mode": mode,
            "duration": auction_duration
        }, to=None)

        return jsonify({
            "message": f"Moved to next player ({next_player['name']}) in {mode} mode",
            "player_id": next_player_id,
            "player_name": next_player["name"],
            "mode": mode,
            "status": "auction_moved"
        }), 200

    except mysql.connector.Error as err:
        conn.rollback()
        return jsonify({"error": str(err), "status": "error"}), 500

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
                FROM bids b
                JOIN teams t ON b.team_id = t.id
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
        cursor.execute("DELETE FROM bids WHERE player_id = %s", (player_id,))

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
            cursor.execute("DELETE FROM bids WHERE player_id = %s", (player_id,))
        
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
            JOIN teams t ON sp.team_id = t.id
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
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
