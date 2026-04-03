import asyncio
import time
from auction_state import auction_state
from socket_manager import sio
from team_state import team_wallets

MIN_INCREMENT = 500
DEFAULT_TIME = 20
timer_task = None
bid_lock = asyncio.Lock()

async def emit_update():

    for sid, _ in sio.manager.get_participants("/", None):

        session = await sio.get_session(sid)

        if not session:
            continue

        user = session.get("user")
        if not user:
            continue

        team_id = user.get("team_id")

        payload = {
            **auction_state,
            "teamBalance": team_wallets.get(team_id, {}).get("purse", 0),
            "canBid": calculate_can_bid(team_id)
        }

        await sio.emit("auction_update", payload, to=sid)

def calculate_can_bid(team_id):

    if auction_state["paused"]:
        return False

    if auction_state["status"] != "auction_active":
        return False

    if not team_id:
        return False

    # Example: prevent same team bidding twice
    last_bid = auction_state["highest_bid"]
    if last_bid and last_bid["team_id"] == team_id:
        return False

    return True

async def timer_loop():

    print("⏱ Timer loop started")

    while auction_state["status"] == "auction_active":

        await asyncio.sleep(1)

        if auction_state["paused"]:
            continue

        auction_state["time_left"] = max(0, auction_state["time_left"] - 1)

        await sio.emit("timer_update", {
            "time_left": auction_state["time_left"]
        })

        if auction_state["time_left"] <= 0:
            await end_auction()
            break


async def start_auction(player):

    global timer_task

    if auction_state["status"] == "auction_active":
        print("⚠️ Auction already running")
        return
    
    auction_state["status"] = "auction_active"
    auction_state["player"] = player
    auction_state["highest_bid"] = None
    auction_state["currentBid"] = player["base_price"]
    auction_state["time_left"] = DEFAULT_TIME
    auction_state["paused"] = False
    auction_state["history"] = []

    # Kill old timer if exists
    if timer_task and not timer_task.done():
        timer_task.cancel()

    timer_task = asyncio.create_task(timer_loop())

    print("🚀 Auction started")

    await emit_update()


async def engine_place_bid(team, bid_amount):

    async with bid_lock:   # 🔥 CRITICAL

        if auction_state["paused"]:
            return {"error": "Auction is paused"}
        
        team_id = team["id"]

        if team_id not in team_wallets:
            return {"error": "Invalid team"}

        team_purse = team_wallets[team_id]["purse"]

        if bid_amount > team_purse:
            return {"error": "Insufficient purse balance"}

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

        print(f"💰 VALID BID → {team['name']} ₹{bid_amount}")

        await emit_update()

        return {"success": True}


async def pause_auction():
    auction_state["paused"] = True
    print("⏸ Auction paused")
    await emit_update()


async def resume_auction():
    auction_state["paused"] = False
    print("▶ Auction resumed")
    await emit_update()


async def end_auction():

    print("🏁 Auction ending")

    winning_team_id = None
    sold_price = 0

    if auction_state["highest_bid"]:

        winning_team_id = auction_state["highest_bid"]["team_id"]
        sold_price = auction_state["highest_bid"]["bid_amount"]

        # 💰 Deduct purse safely
        if winning_team_id in team_wallets:

            current_purse = team_wallets[winning_team_id]["purse"]

            # Prevent negative purse (extra safety)
            if current_purse >= sold_price:
                team_wallets[winning_team_id]["purse"] -= sold_price
            else:
                print("⚠ Purse deduction skipped (insufficient balance)")

        result = {
            "status": "sold",
            "player": auction_state["player"],
            "team": auction_state["highest_bid"],
            "sold_price": sold_price
        }

    else:
        result = {
            "status": "unsold",
            "player": auction_state["player"]
        }

    # 📢 Notify everyone auction ended
    await sio.emit("auction_ended", result)

    # 🔁 Reset state safely
    auction_state.update({
        "status": "idle",
        "player": None,
        "highest_bid": None,
        "currentBid": 0,
        "time_left": 0,
        "history": [],
        "paused": False
    })

    print("✅ Auction reset complete")

    # 🔄 Emit fresh update so teams get updated purse
    await emit_update()
