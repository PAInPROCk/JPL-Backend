import os
import sys
from dotenv import load_dotenv

# Load .env file
load_dotenv()

# We need the service role key to perform admin auth actions bypassing confirmation
supabase_url = os.getenv("SUPABASE_URL")
supabase_service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not supabase_url or not supabase_service_key:
    print("❌ Error: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in your .env file.")
    print("Please add them to your .env to run this script.")
    sys.exit(1)

try:
    from supabase import create_client, Client
except ImportError:
    print("❌ Error: supabase-py package not found.")
    sys.exit(1)

supabase: Client = create_client(supabase_url, supabase_service_key)

def create_admin(email, password, name):
    print(f"🚀 Creating Supabase Admin User: {email}...")
    try:
        # Create user via Supabase Auth Admin API
        user = supabase.auth.admin.create_user({
            "email": email,
            "password": password,
            "email_confirm": True, # Auto-confirm email
            "user_metadata": {
                "name": name,
                "role": "admin"
            },
            "app_metadata": {
                "role": "admin"
            }
        })
        
        user_id = user.user.id
        print(f"✅ Supabase Auth user created with UUID: {user_id}")
        print("🎉 Admin user sync trigger will automatically copy this user to public.users table.")
        
    except Exception as e:
        print(f"❌ Error creating admin user: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python create_supabase_admin.py <email> <password> <name>")
        print("Example: python create_supabase_admin.py admin@example.com mysecretpassword 'JPL Admin'")
    else:
        email = sys.argv[1]
        password = sys.argv[2]
        name = sys.argv[3]
        create_admin(email, password, name)
