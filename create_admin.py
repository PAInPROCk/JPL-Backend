import pymysql
import bcrypt
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Step 1: Connect to your database
db = pymysql.connect(
    host=os.getenv("MYSQLHOST", "127.0.0.1"),
    user=os.getenv("MYSQLUSER", "root"),
    password=os.getenv("MYSQLPASSWORD", ""),
    database=os.getenv("MYSQLDATABASE", "jpl"),
    port=int(os.getenv("MYSQLPORT", 3306)),
    connect_timeout=5,
    cursorclass=pymysql.cursors.DictCursor
)
cursor = db.cursor()

# Step 2: Admin credentials
name = "JPL Admin 3"
email = "admin3@example.com"       # change if you want
plain_password = "12345"           # change if you want
role = "admin"
team_id = None 

# Step 3: Hash the password
hashed_password = bcrypt.hashpw(plain_password.encode('utf-8'), bcrypt.gensalt())

# Step 4: Insert into table
try:
    cursor.execute(
        "INSERT INTO users (name, email, password, role, team_id) VALUES (%s, %s, %s, %s, %s)",
        (name, email, hashed_password.decode('utf-8'), role, team_id)
    )
    db.commit()
    if role == "admin":
        print(f"✅ Admin user created: {email}")
    else:
        print(f"Team User Created Successfully: {email}")
        
except pymysql.connector.Error as err:
    print(f"❌ Error: {err}")

cursor.close()
db.close()
