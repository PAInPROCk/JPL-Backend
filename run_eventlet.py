# run_eventlet.py
import os
import sys

# ✅ Must monkey patch BEFORE ANYTHING ELSE
print("🧩 Applying eventlet.monkey_patch() early...")
import eventlet
eventlet.monkey_patch(os=True, select=True, socket=True, thread=True, time=True)
print("✅ Monkey patch applied successfully")

# ✅ Delay import of app until after patch
print("🚀 Importing app safely after patch...")
from app import app, socketio

if __name__ == "__main__":
    print("🚀 Starting JPL backend with Eventlet...")
    socketio.run(app, host="0.0.0.0", port=5000)
