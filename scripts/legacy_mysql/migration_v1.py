import pymysql
import os
from dotenv import load_dotenv

# Load .env from the same directory
load_dotenv()

def migrate():
    # Use hardcoded values from database.py as primary, or .env if available
    # Given database.py is currently hardcoded, we'll try to follow its pattern but allow overrides
    
    # Prioritize local 127.0.0.1 as per database.py
    host = "127.0.0.1"
    port = 3306
    user = "root"
    password = ""
    database = "jpl"

    # Fallback to .env if local connection fails (optional logic can be added)
    if os.getenv("MYSQLHOST") and os.getenv("MYSQLHOST") != "127.0.0.1":
         print("ℹ️ External DB found in .env, but prioritizing local 127.0.0.1...")

    print(f"🚀 Starting Migration on {host}:{port}/{database}...")

    try:
        conn = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            cursorclass=pymysql.cursors.DictCursor
        )
        cursor = conn.cursor()

        # 1. Update teams table budget columns
        print("📊 Updating 'teams' table columns to DECIMAL...")
        cursor.execute("ALTER TABLE teams MODIFY Season_Budget DECIMAL(15,2)")
        cursor.execute("ALTER TABLE teams MODIFY Total_Budget DECIMAL(15,2)")

        # 2. Add Unique constraints to prevent duplicate sales/bids
        print("🔑 Adding UNIQUE constraints to 'sold_players' and 'live_bids'...")
        
        # Check if index already exists to avoid errors on re-run
        cursor.execute("SHOW INDEX FROM sold_players WHERE Column_name = 'player_id' AND Non_unique = 0")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE sold_players ADD UNIQUE (player_id)")
            print("✅ Unique constraint added to sold_players(player_id)")
        else:
            print("ℹ️ Unique constraint already exists on sold_players(player_id)")

        cursor.execute("SHOW INDEX FROM live_bids WHERE Column_name = 'player_id' AND Non_unique = 0")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE live_bids ADD UNIQUE (player_id)")
            print("✅ Unique constraint added to live_bids(player_id)")
        else:
            print("ℹ️ Unique constraint already exists on live_bids(player_id)")

        conn.commit()
        print("🎉 Migration completed successfully!")

    except Exception as e:
        print(f"❌ Migration failed: {e}")
    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    migrate()
