from flask import Flask, jsonify, request, session
import mysql.connector
import pandas as pd
from flask_cors import CORS
import bcrypt
import io
import csv

app = Flask(__name__)
app.secret_key = "jpl_secret_here"  # ⚠️ Replace with a strong, random secret key
CORS(app, supports_credentials=True)  # Enable cookies for session auth

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

    print("DEBUG: user from DB",user)

    if user and bcrypt.checkpw(password.encode('utf-8'), user['password'].encode('utf-8')):
        print("DEBUG: password match success")
        session['user'] = {
            'id': user['id'],
            'email': user['email'],
            'role': user['role']
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
    user = session.get('user')
    if 'user' in session:
        return jsonify({
            "authenticated": True,
            "user": session['user'],
            "role": user['role'] 
        }), 200
    else:
        return jsonify({"authenticated": False}), 401

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

@app.route('/add-team', methods=['POST'])
def add_team():
    # ✅ Authentication check
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    # ✅ Role check (only admin can add teams)
    if session['user']['role'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403

    data = request.json
    name = data.get('name')
    owner = data.get('owner')
    budget = data.get('budget', 0)

    # ✅ Validation check
    if not name:
        return jsonify({"error": "Team name is required"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "INSERT INTO teams (name, owner, budget) VALUES (%s, %s, %s)",
            (name, owner, budget)
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

# ✅ Add a new player
@app.route('/add-player', methods=['POST'])
def add_player():
    # ✅ Authentication check
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    # ✅ Role check (only admin can add players)
    if session['user']['role'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403

    data = request.json

    # ✅ Extract fields
    name = data.get('name')
    nickname = data.get('nickname')
    age = data.get('age')
    category = data.get('category')   # e.g. Batsman, Bowler
    type_ = data.get('type')          # e.g. Right-hand, Left-hand
    base_price = data.get('base_price')
    total_runs = data.get('total_runs', 0)
    highest_runs = data.get('highest_runs', 0)
    wickets_taken = data.get('wickets_taken', 0)
    times_out = data.get('times_out', 0)
    teams_played = data.get('teams_played')
    image_path = data.get('image_path')
    jersey_number = data.get('jersey_number')

    # ✅ Validation
    if not name or not base_price:
        return jsonify({"error": "Player name and base price are required"}), 400
    
    if jersey_number is not None and not str(jersey_number).isdigit():
        return jsonify({"error": "Jersey number must be numeric"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            INSERT INTO players 
            (name, nickname, age, category, type, base_price, total_runs, highest_runs, 
             wickets_taken, times_out, teams_played, image_path, jersey_number) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            name, nickname, age, category, type_, base_price, 
            total_runs, highest_runs, wickets_taken, times_out, 
            teams_played, image_path, jersey_number
        ))
        conn.commit()

        return jsonify({"message": "Player added successfully!"}), 201

    except mysql.connector.IntegrityError:
        return jsonify({"error": "Player with same name or jersey number already exists"}), 400

    except mysql.connector.Error as err:
        return jsonify({"error": str(err)}), 400

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
    if not file.filename.endswith('.csv'):
        return jsonify({'error': 'Only CSV files are allowed'}), 400

    try:
        # ✅ Use pandas for flexible column handling
        df = pd.read_csv(file)

        # ✅ Minimum required columns
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
                base_price = float(row['base_price']) if pd.notna(row['base_price']) else None

                if not name or base_price is None:
                    skipped += 1
                    continue

                # Optional fields
                nickname = row.get('nickname')
                age = int(row['age']) if 'age' in row and pd.notna(row['age']) else None
                category = row.get('category')
                type_ = row.get('type')
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
                            teams_played=%s, image_path=%s, jersey_number=%s
                        WHERE name=%s
                    """, (
                        base_price, nickname, age, category, type_,
                        total_runs, highest_runs, wickets_taken, times_out,
                        teams_played, image_path, jersey_number, name
                    ))
                    updated += 1
                else:
                    # ✅ Insert new
                    cursor.execute("""
                        INSERT INTO players 
                        (name, base_price, nickname, age, category, type, total_runs, highest_runs, wickets_taken, times_out, teams_played, image_path, jersey_number)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        name, base_price, nickname, age, category, type_,
                        total_runs, highest_runs, wickets_taken, times_out,
                        teams_played, image_path, jersey_number
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
    
@app.route('/start-auction', methods=['POST'])
def start_auction():
    # 🔐 Auth + Role check
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized', 'status': 'error'}), 401
    if session['user'].get('role') != 'admin':
        return jsonify({'error': 'Forbidden', 'status': 'error'}), 403

    data = request.json
    player_id = data.get('player_id')
    if not player_id:
        return jsonify({'error': 'player_id is required', 'status': 'error'}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # ✅ Check if player exists
        cursor.execute("SELECT id, name FROM players WHERE id = %s", (player_id,))
        player = cursor.fetchone()
        if not player:
            return jsonify({'error': 'Player not found', 'status': 'error'}), 404

        # ✅ Check if player already sold
        cursor.execute("SELECT id FROM sold_players WHERE player_id = %s", (player_id,))
        sold = cursor.fetchone()
        if sold:
            return jsonify({'error': 'Player already sold', 'status': 'error'}), 400

        # ✅ Clear any existing auction
        cursor.execute("DELETE FROM current_auction")

        # ✅ Insert new auction
        cursor.execute("INSERT INTO current_auction (player_id) VALUES (%s)", (player_id,))
        conn.commit()

        return jsonify({
            "message": f"Auction started for {player['name']}",
            "player_id": player_id,
            "player_name": player['name'],
            "status": "auction_started"
        }), 201

    except mysql.connector.Error as err:
        conn.rollback()
        return jsonify({'error': str(err), 'status': 'error'}), 500

    finally:
        cursor.close()
        conn.close()

# ✅ Get current auction player
@app.route('/current-auction')
def get_current_auction():
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT players.* FROM current_auction
        JOIN players ON current_auction.player_id = players.id
        ORDER BY current_auction.id DESC LIMIT 1
    """)
    player = cursor.fetchone()
    cursor.close()
    conn.close()
    return jsonify(player)

# ✅ Move auction to next player
@app.route('/next-auction', methods=['POST'])
def next_auction():
    # 🔐 Auth check
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    # 🔐 Role check (only admin can move to next auction)
    if session['user'].get('role') != 'admin':
        return jsonify({"error": "Forbidden"}), 403

    data = request.get_json()
    player_id = data.get('player_id')
    if not player_id:
        return jsonify({"error": "Missing player_id"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # ✅ Clear any existing auction
        cursor.execute("DELETE FROM current_auction")

        # ✅ Insert next player
        cursor.execute("INSERT INTO current_auction (player_id) VALUES (%s)", (player_id,))
        conn.commit()

        return jsonify({
            "message": "Auction moved to next player",
            "player_id": player_id,
            "status": "auction_moved"
        }), 200

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        cursor.close()
        conn.close()

    
@app.route('/end-auction', methods=['POST'])
def end_auction():
    # 🔐 Authentication + Role check (Option 2 style)
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
        # 1️⃣ Get current auction
        cursor.execute("SELECT * FROM current_auction LIMIT 1")
        auction = cursor.fetchone()

        if not auction:
            if force_clear:
                cursor.execute("DELETE FROM current_auction")
                conn.commit()
                return jsonify({
                    "message": "Auction table cleared (force_clear used)",
                    "status": "cleared"
                }), 200
            return jsonify({'error': 'No active auction', 'status': 'error'}), 400

        player_id = auction['player_id']

        # 2️⃣ Force clear option
        if force_clear:
            cursor.execute("DELETE FROM current_auction")
            conn.commit()
            return jsonify({
                "message": "Auction forcefully cleared",
                "player_id": player_id,
                "status": "cleared"
            }), 200

        # 3️⃣ Admin direct sale
        if team_id and sold_price:
            # Validate team
            cursor.execute("SELECT id, name FROM teams WHERE id = %s", (team_id,))
            team = cursor.fetchone()
            if not team:
                return jsonify({'error': 'Team not found', 'status': 'error'}), 404

            # Insert into sold_players
            cursor.execute("""
                INSERT INTO sold_players (player_id, team_id, sold_price)
                VALUES (%s, %s, %s)
            """, (player_id, team_id, sold_price))

            # Deduct budget
            cursor.execute("UPDATE teams SET budget = budget - %s WHERE id = %s", (sold_price, team_id))

            # Clear auction
            cursor.execute("DELETE FROM current_auction")
            conn.commit()

            return jsonify({
                "message": f"Player sold directly to {team['name']} for {sold_price}",
                "player_id": player_id,
                "team_id": team['id'],
                "team_name": team['name'],
                "sold_price": float(sold_price),
                "status": "sold"
            }), 201

        # 4️⃣ Bidding flow
        cursor.execute("""
            SELECT b.team_id, b.bid_amount, t.name AS team_name
            FROM bids b
            JOIN teams t ON b.team_id = t.id
            WHERE b.player_id = %s
            ORDER BY b.bid_amount DESC, b.bid_time ASC
            LIMIT 1
        """, (player_id,))
        highest_bid = cursor.fetchone()

        if not highest_bid:
            # No bids → auction failed
            cursor.execute("DELETE FROM current_auction")
            conn.commit()
            return jsonify({
                "message": "Auction ended with no bids",
                "player_id": player_id,
                "status": "unsold"
            }), 200

        # Process winning bid
        team_id = highest_bid['team_id']
        sold_price = highest_bid['bid_amount']

        cursor.execute("""
            INSERT INTO sold_players (player_id, team_id, sold_price)
            VALUES (%s, %s, %s)
        """, (player_id, team_id, sold_price))

        cursor.execute("UPDATE teams SET budget = budget - %s WHERE id = %s", (sold_price, team_id))

        # Clear auction + bids
        cursor.execute("DELETE FROM current_auction")
        cursor.execute("DELETE FROM bids WHERE player_id = %s", (player_id,))
        conn.commit()

        return jsonify({
            "message": f"Player sold via auction to {highest_bid['team_name']} for {sold_price}",
            "player_id": player_id,
            "team_id": team_id,
            "team_name": highest_bid['team_name'],
            "sold_price": float(sold_price),
            "status": "sold"
        }), 200

    except mysql.connector.IntegrityError:
        conn.rollback()
        return jsonify({'error': 'This player is already sold', 'status': 'error'}), 400

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
    app.run(debug=True)
