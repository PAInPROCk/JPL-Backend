from flask import Flask, jsonify
import mysql.connector

app = Flask(__name__)


# Connect to MySQL DB from XAMPP
db = mysql.connector.connect(
    host="localhost",
    user="root",
    password="",         # Leave empty for XAMPP unless you set a password
    database="jpl"
)

cursor = db.cursor(dictionary=True)

@app.route('/')
def home():
    return "Welcome to JPL Backend!"

@app.route('/players')
def get_players():
    cursor.execute("SELECT * FROM players")
    players = cursor.fetchall()
    return jsonify(players)

if __name__ == '__main__':
    app.run(debug=True)
