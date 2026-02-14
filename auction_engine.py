import asyncio
import time
from auction_state import auction_state
from socket_manager import sio

MIN_INCREMENT = 500
DEFAULT_TIME = 20


async def emit_update():
    """Single source of truth emitter"""
    await sio.emit("auction_update", auction_state)


async def timer_loop():
    """Single authoritative timer"""
    while auction_state["status"] == "auction_active":

        if not auction_state["paused"]:
            auction_state["time_left"] = max(0, auction_state["time_left"] - 1)

            if auction_state["time_left"] <= 0:
                await end_auction()
                break

            await emit_update()

        await asyncio.sleep(1)


async def start_auction(player):
    auction_state["status"] = "auction_active"
    auction_state["player"] = player
    auction_state["highest_bid"] = None
    auction_state["currentBid"] = player["base_price"]
    auction_state["time_left"] = DEFAULT_TIME
    auction_state["paused"] = False
    auction_state["history"] = []

    asyncio.create_task(timer_loop())
    await emit_update()


async def place_bid(team, bid_amount):

    if auction_state["paused"]:
        return {"error": "Auction is paused"}

    current = auction_state["currentBid"]

    if bid_amount <= current:
        return {"error": "Bid must be higher than current bid"}

    if bid_amount < current + MIN_INCREMENT:
        return {"error": f"Minimum increment is ₹{MIN_INCREMENT}"}

    bid_data = {
        "team_id": team["id"],
        "team_name": team["name"],
        "bid_amount": bid_amount,
        "bid_time": time.strftime("%H:%M:%S")
    }

    auction_state["highest_bid"] = bid_data
    auction_state["currentBid"] = bid_amount
    auction_state["time_left"] = DEFAULT_TIME   # 🔥 RESET TIMER
    auction_state["history"].append(bid_data)

    await emit_update()

    return {"success": True}    


async def pause_auction():
    auction_state["paused"] = True
    auction_state["status"] = "paused"
    await emit_update()


async def resume_auction():
    auction_state["paused"] = False
    auction_state["status"] = "auction_active"
    await emit_update()


async def end_auction():
    if auction_state["highest_bid"]:
        result = {
            "status": "sold",
            "player": auction_state["player"],
            "team": auction_state["highest_bid"]
        }
    else:
        result = {
            "status": "unsold",
            "player": auction_state["player"]
        }

    await sio.emit("auction_ended", result)

    auction_state["status"] = "idle"
    auction_state["player"] = None
    auction_state["highest_bid"] = None
    auction_state["currentBid"] = 0
    auction_state["time_left"] = 0
    auction_state["history"] = []
