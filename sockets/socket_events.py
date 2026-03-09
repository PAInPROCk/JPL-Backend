from sockets.socket_manager import sio

def register_socket_events():
    @sio.event
    async def connect(sid, eviron):
        print("✅ Socket Connected:", sid)

    @sio.event
    async def disconnect(sid):
        print("❌ Socket Disconnected:", sid)