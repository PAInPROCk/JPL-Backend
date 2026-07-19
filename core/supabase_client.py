import os
from dotenv import load_dotenv
from supabase import create_client, Client

# Load environment variables
load_dotenv()

supabase_url = os.getenv("SUPABASE_URL")
supabase_anon_key = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_KEY")
supabase_service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not supabase_url:
    raise RuntimeError("SUPABASE_URL is missing from environment")

_client = None
_admin_client = None

def get_supabase_client() -> Client:
    """Returns a client authenticated with the public anon key (cached)."""
    global _client
    if _client is None:
        if not supabase_anon_key:
            raise RuntimeError("SUPABASE_ANON_KEY or SUPABASE_KEY is missing from environment")
        if supabase_anon_key.startswith("sb_"):
            # If it uses the publishable one, try fallback
            raise RuntimeError("SUPABASE_KEY appears to be a publishable key. Use the Supabase anon public JWT key instead.")
        _client = create_client(supabase_url, supabase_anon_key)
    return _client

def get_supabase_admin_client() -> Client:
    """Returns a client authenticated with the service role key (cached)."""
    global _admin_client
    if _admin_client is None:
        if not supabase_service_role_key:
            raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is missing from environment")
        _admin_client = create_client(supabase_url, supabase_service_role_key)
    return _admin_client
