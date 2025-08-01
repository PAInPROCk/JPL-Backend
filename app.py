from flask import Flask, jsonify, request
import mysql.connector
import pandas as pd
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Allow all origins for now

# ✅ Connect to MySQL database (XAMPP default config)
db = mysql.connector.connect(
    host="localhost",
    user="root",
    password="",        # Leave blank for XAMPP
    database="jpl"
)

cursor = db.cursor(dictionary=True)

# ✅ Home route
@app.route('/')
def home():
    return "Welcome to JPL Backend!"

# ✅ Get all players
@app.route('/players')
def get_players():
    cursor.execute("SELECT * FROM players")
    players = cursor.fetchall()
    return jsonify(players)

# ✅ Get all teams
@app.route('/teams')
def get_teams():
    cursor.execute("SELECT * FROM teams")
    teams = cursor.fetchall()
    return jsonify(teams)

# ✅ Get all bids with player and team names
@app.route('/bids')
def get_bids():
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
    return jsonify(bids)

# ✅ Add a new bid
@app.route('/add-bid', methods=['POST'])
def add_bid():
    data = request.get_json()
    player_id = data.get('player_id')
    team_id = data.get('team_id')
    bid_amount = data.get('bid_amount')

    if not player_id or not team_id or not bid_amount:
        return jsonify({"error": "Missing data"}), 400

    try:
        cursor.execute(
            "INSERT INTO bids (player_id, team_id, bid_amount) VALUES (%s, %s, %s)",
            (player_id, team_id, bid_amount)
        )
        db.commit()
        return jsonify({"message": "Bid added successfully"}), 201
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500

# ✅ Add a new player
@app.route('/add-player', methods=['POST'])
def add_player():
    data = request.get_json()
    
    name = data.get('name')
    nickname = data.get('nickname')
    age = data.get('age')
    category = data.get('category')
    type_ = data.get('type')
    base_price = data.get('base_price')
    total_runs = data.get('total_runs')
    highest_runs = data.get('highest_runs')
    wickets_taken = data.get('wickets_taken')
    times_out = data.get('times_out')
    teams_played = data.get('teams_played')
    image_path = data.get('image_path')

    if not name or not base_price or not category or not type_:
        return jsonify({"error": "Missing required player data"}), 400

    try:
        cursor.execute("""
            INSERT INTO players 
            (name, nickname, age, category, type, base_price, total_runs, highest_runs, 
             wickets_taken, times_out, teams_played, image_path)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            name, nickname, age, category, type_, base_price,
            total_runs, highest_runs, wickets_taken, times_out, teams_played, image_path
        ))
        db.commit()
        return jsonify({"message": "Player added successfully"}), 201
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500

# ✅ Upload players via CSV and update if exists
@app.route('/upload-players', methods=['POST'])
def upload_players():
    if 'file' not in request.files:
        return jsonify({"error": "CSV file is missing"}), 400

    file = request.files['file']

    try:
        df = pd.read_csv(file)

        for _, row in df.iterrows():
            # Check if player already exists by name
            cursor.execute("SELECT id FROM players WHERE name = %s", (row['name'],))
            existing = cursor.fetchone()

            if existing:
                # UPDATE existing player
                cursor.execute("""
                    UPDATE players
                    SET nickname=%s, age=%s, category=%s, type=%s, base_price=%s,
                        total_runs=%s, highest_runs=%s, wickets_taken=%s,
                        times_out=%s, teams_played=%s, image_path=%s
                    WHERE name=%s
                """, (
                    row['nickname'], row['age'], row['category'], row['type'],
                    row['base_price'], row['total_runs'], row['highest_runs'],
                    row['wickets_taken'], row['times_out'], row['teams_played'],
                    row['image_path'], row['name']
                ))
            else:
                # INSERT new player
                cursor.execute("""
                    INSERT INTO players
                    (name, nickname, age, category, type, base_price, total_runs,
                     highest_runs, wickets_taken, times_out, teams_played, image_path)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    row['name'], row['nickname'], row['age'], row['category'], row['type'],
                    row['base_price'], row['total_runs'], row['highest_runs'],
                    row['wickets_taken'], row['times_out'], row['teams_played'], row['image_path']
                ))

        db.commit()
        return jsonify({"message": "Players uploaded and updated successfully"}), 201

    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500

# ✅ Get current auction player
@app.route('/current-auction')
def get_current_auction():
    cursor = db.cursor(dictionary=True)
    cursor.execute("""
        SELECT players.* FROM current_auction
        JOIN players ON current_auction.player_id = players.id
        ORDER BY current_auction.id DESC LIMIT 1
    """)
    player = cursor.fetchone()
    cursor.close()
    return jsonify(player)

# ✅ Set next auction player (admin trigger)
@app.route('/next-auction', methods=['POST'])
def next_auction():
    data = request.get_json()
    player_id = data.get('player_id')

    if not player_id:
        return jsonify({"error": "Missing player_id"}), 400

    try:
        cursor = db.cursor()
        cursor.execute("INSERT INTO current_auction (player_id) VALUES (%s)", (player_id,))
        db.commit()
        cursor.close()
        return jsonify({"message": "Auction moved to next player"}), 200
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500

# ✅ Run the Flask app
if __name__ == '__main__':
    app.run(debug=True)
