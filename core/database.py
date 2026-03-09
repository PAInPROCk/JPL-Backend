import pymysql


def get_db_connection():
    try:
        print("🔌 Attempting DB Connection...")

        conn = pymysql.connect(
            host="127.0.0.1",
            user="root",
            password="",
            database="jpl",
            port=3306,
            connect_timeout=5,
            cursorclass=pymysql.cursors.DictCursor
        )
        print("DB Connection established")
        return conn
    
    except Exception as e:
        print("❌Database Connection Error:", e)
        return None