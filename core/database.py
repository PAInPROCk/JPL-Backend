import pymysql
import os

def get_db_connection():
    try:
        print("🔌 Attempting DB Connection...")

        conn = pymysql.connect(
            host=os.getenv("MYSQLHOST"),
            user=os.getenv("MYSQLUSER"),
            password=os.getenv("MYSQLPASSWORD"),
            database=os.getenv("MYSQLDATABASE"),
            port=int(os.getenv("MYSQLPORT")),
            connect_timeout=5,
            cursorclass=pymysql.cursors.DictCursor
        )
        print("DB Connection established")
        return conn
    
    except Exception as e:
        print("❌Database Connection Error:", e)
        return None