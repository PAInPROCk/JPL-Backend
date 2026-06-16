import sys
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Setup Mock PyMySQL for backward compatibility in other files
try:
    import psycopg2
    import psycopg2.extras

    class MockPyMySQL:
        class cursors:
            DictCursor = psycopg2.extras.RealDictCursor
        
        IntegrityError = psycopg2.IntegrityError
        Error = psycopg2.Error
        DataError = psycopg2.DataError
        DatabaseError = psycopg2.DatabaseError
        InterfaceError = psycopg2.InterfaceError
        InternalError = psycopg2.InternalError
        NotSupportedError = psycopg2.NotSupportedError
        OperationalError = psycopg2.OperationalError
        ProgrammingError = psycopg2.ProgrammingError

    sys.modules['pymysql'] = MockPyMySQL
    sys.modules['pymysql.cursors'] = MockPyMySQL.cursors
except ImportError:
    pass

class PostgresConnectionWrapper:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self, cursor=None, cursorclass=None):
        from psycopg2.extras import RealDictCursor
        return self._conn.cursor(cursor_factory=RealDictCursor)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def begin(self):
        pass

def get_db_connection():
    try:
        print("🔌 Attempting DB Connection...")
        import psycopg2

        # Support Supabase / standard PostgreSQL env variables, or fallback to local defaults
        host = os.getenv("SUPABASE_DB_HOST") or os.getenv("POSTGRES_HOST") or os.getenv("MYSQLHOST", "127.0.0.1")
        port = os.getenv("SUPABASE_DB_PORT") or os.getenv("POSTGRES_PORT") or os.getenv("MYSQLPORT", "5432")
        user = os.getenv("SUPABASE_DB_USER") or os.getenv("POSTGRES_USER") or os.getenv("MYSQLUSER", "postgres")
        password = os.getenv("SUPABASE_DB_PASSWORD") or os.getenv("POSTGRES_PASSWORD") or os.getenv("MYSQLPASSWORD", "")
        database = os.getenv("SUPABASE_DB_NAME") or os.getenv("POSTGRES_DATABASE") or os.getenv("MYSQLDATABASE", "postgres")

        conn = psycopg2.connect(
            host=host,
            port=int(port),
            user=user,
            password=password,
            database=database,
            connect_timeout=5
        )
        print("DB Connection established")
        return PostgresConnectionWrapper(conn)
    
    except Exception as e:
        print("❌Database Connection Error:", e)
        return None