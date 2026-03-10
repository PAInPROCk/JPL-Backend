from pydantic import BaseModel
from typing import Optional

class StartAuctionRequest(BaseModel):
    mode: str = "manual"
    player_id: Optional[int] = None
    duration: int = 120