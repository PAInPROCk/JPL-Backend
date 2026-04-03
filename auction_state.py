import asyncio
import time

auction_state = {
    "status": "idle",          # idle | auction_active | paused
    "player": None,
    "highest_bid": None,
    "currentBid": 0,
    "time_left": 0,
    "paused": False,
    "history": []
}

TIMER_TASK = None
