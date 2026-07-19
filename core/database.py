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

# Global pool instance
_db_pool = None

def get_pool():
    global _db_pool
    if _db_pool is None:
        try:
            print("[DB] Initializing database connection pool...")
            from psycopg2.pool import ThreadedConnectionPool
            
            host = os.getenv("SUPABASE_DB_HOST") or os.getenv("POSTGRES_HOST") or os.getenv("MYSQLHOST", "127.0.0.1")
            port = os.getenv("SUPABASE_DB_PORT") or os.getenv("POSTGRES_PORT") or os.getenv("MYSQLPORT", "5432")
            user = os.getenv("SUPABASE_DB_USER") or os.getenv("POSTGRES_USER") or os.getenv("MYSQLUSER", "postgres")
            password = os.getenv("SUPABASE_DB_PASSWORD") or os.getenv("POSTGRES_PASSWORD") or os.getenv("MYSQLPASSWORD", "")
            database = os.getenv("SUPABASE_DB_NAME") or os.getenv("POSTGRES_DATABASE") or os.getenv("MYSQLDATABASE", "postgres")
            
            _db_pool = ThreadedConnectionPool(
                minconn=2,
                maxconn=20,
                host=host,
                port=int(port),
                user=user,
                password=password,
                database=database,
                connect_timeout=5
            )
            print("[DB] Connection pool initialized successfully with minconn=2, maxconn=20.")
        except Exception as e:
            print("[DB Error] Failed to initialize connection pool:", e)
            raise e
    return _db_pool

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
        # Instead of closing the actual database TCP connection,
        # return it back to the global ThreadedConnectionPool!
        if _db_pool is not None and self._conn is not None:
            try:
                _db_pool.putconn(self._conn)
            except Exception as e:
                print("[DB Error] Failed to release connection back to pool:", e)
        else:
            if self._conn is not None:
                self._conn.close()

    def begin(self):
        pass

def get_db_connection():
    try:
        pool = get_pool()
        conn = pool.getconn()
        return PostgresConnectionWrapper(conn)
    except Exception as e:
        print("[DB Error] Database Connection Error:", e)
        return None