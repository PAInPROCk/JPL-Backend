import mysql.connector
import bcrypt

# Step 1: Connect to your database
db = mysql.connector.connect(
    host="localhost",        # change if needed
    user="root",             # change if needed
    password="",             # change if needed
    database="jpl"           # your DB name
)
cursor = db.cursor()

# Step 2: Admin credentials
email = "team@example.com"       # change if you want
plain_password = "12345"           # change if you want
role = "team"

# Step 3: Hash the password
hashed_password = bcrypt.hashpw(plain_password.encode('utf-8'), bcrypt.gensalt())

# Step 4: Insert into table
try:
    cursor.execute(
        "INSERT INTO users (email, password, role) VALUES (%s, %s, %s)",
        (email, hashed_password.decode('utf-8'), role)
    )
    db.commit()
    print(f"✅ Admin user created: {email}")
except mysql.connector.Error as err:
    print(f"❌ Error: {err}")

cursor.close()
db.close()
