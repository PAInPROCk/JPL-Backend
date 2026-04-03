from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import socketio
from sockets.socket_manager import sio
from sockets.socket_events import register_socket_events
from core.database import get_db_connection
from fastapi.concurrency import run_in_threadpool
from auth.auth_routes import router as auth_router
from routers.players import router as players_router
from routers.teams import router as teams_router
from routers.auction_routes import router as auction_router
import socket
# from core.utils import get_local_ip

#Create FastAPI app
app =  FastAPI()
app.include_router(auth_router)
app.include_router(players_router)
app.include_router(teams_router)
app.include_router(auction_router)



#CORS Setup for frontend and Backend connectivity

# FRONTEND_PORT = 3000
# # local_ip = get_local_ip()

# origins = [
#     f"http://localhost:{FRONTEND_PORT}",
#     f"http://127.0.0.1:{FRONTEND_PORT}",
#     # f"http://{local_ip}:{FRONTEND_PORT}",
#     "*"
# ]

# print("🌐 Allowed CORS Origins:", origins)
# print(f"🚀 Server running on: http://{local_ip}:5000")

app.add_middleware(
    CORSMiddleware,
    allow_origins= ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

#Register socket events
register_socket_events()

#Combine FastAPI + Soket.IO
socket_app = socketio.ASGIApp(sio, other_asgi_app=app)

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

@app.get("/")
async def root():
    return{"Message":"JPL Backend Running"}

@app.get("/db-test")
async def db_test():
    conn = await run_in_threadpool(get_db_connection)

    if conn is None:
        return{"DB" : "Connection failed"}

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1")

        result = cursor.fetchone()
        return{
            "db": "connected",
            "result": result
        }
    except Exception as e:
        print("DB test error:",e)
        return{"error": str(e)}
    finally:
        cursor.close()
        conn.close()

