-- Enable UUID extension (usually enabled by default in Supabase)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Drop tables in correct order if they exist
DROP TABLE IF EXISTS unsold_players;
DROP TABLE IF EXISTS sold_players;
DROP TABLE IF EXISTS live_bids;
DROP TABLE IF EXISTS bids;
DROP TABLE IF EXISTS current_auction;
DROP TABLE IF EXISTS player_teams;
DROP TABLE IF EXISTS captains;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS players;
DROP TABLE IF EXISTS teams;

-- Create Teams table
CREATE TABLE teams (
    team_id SERIAL PRIMARY KEY,
    name VARCHAR(255) UNIQUE NOT NULL,
    captain VARCHAR(255),
    mobile_No VARCHAR(20),
    email_Id VARCHAR(255),
    Team_Rank INT DEFAULT 0,
    Total_Budget DECIMAL(15,2) DEFAULT 0.00,
    Season_Budget DECIMAL(15,2) DEFAULT 0.00,
    purse DECIMAL(15,2) DEFAULT 0.00 CHECK (purse >= 0),
    Players_Bought INT DEFAULT 0,
    image_path VARCHAR(255)
);

-- Create Players table
CREATE TABLE players (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) UNIQUE NOT NULL,
    nickname VARCHAR(255),
    age INT,
    gender VARCHAR(20),
    category VARCHAR(100),
    type VARCHAR(100),
    jersey INT UNIQUE,
    mobile_No VARCHAR(20),
    email_Id VARCHAR(255),
    base_price DECIMAL(15,2) DEFAULT 0.00,
    total_runs INT DEFAULT 0,
    highest_runs INT DEFAULT 0,
    wickets_taken INT DEFAULT 0,
    times_out INT DEFAULT 0,
    teams_played TEXT,
    image_path VARCHAR(255)
);

-- Create Captains table
CREATE TABLE captains (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    team_id INT UNIQUE REFERENCES teams(team_id) ON DELETE SET NULL,
    image_path VARCHAR(255)
);

-- Create Player Teams mapping (history or squads)
CREATE TABLE player_teams (
    player_id INT REFERENCES players(id) ON DELETE CASCADE,
    team_id INT REFERENCES teams(team_id) ON DELETE CASCADE,
    PRIMARY KEY (player_id, team_id)
);

-- Create Users table (linked to Supabase Auth)
CREATE TABLE users (
    id UUID PRIMARY KEY, -- Will store Supabase Auth user UUID
    name VARCHAR(255),
    email VARCHAR(255) UNIQUE NOT NULL,
    role VARCHAR(50) DEFAULT 'team', -- 'admin' or 'team'
    team_id INT REFERENCES teams(team_id) ON DELETE SET NULL
);

-- Create Current Auction table (Active player being bid on)
CREATE TABLE current_auction (
    player_id INT PRIMARY KEY REFERENCES players(id) ON DELETE CASCADE,
    start_time TIMESTAMP WITH TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    auction_duration INT DEFAULT 120,
    mode VARCHAR(50) DEFAULT 'manual',
    paused BOOLEAN DEFAULT FALSE,
    paused_remaining INT DEFAULT 0
);

-- Create Live Bids table (Tracks high bid per player in real-time)
CREATE TABLE live_bids (
    player_id INT PRIMARY KEY REFERENCES players(id) ON DELETE CASCADE,
    team_id INT REFERENCES teams(team_id) ON DELETE CASCADE,
    bid_amount DECIMAL(15,2) NOT NULL,
    bid_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Create Bids table (Full bidding history log)
CREATE TABLE bids (
    id SERIAL PRIMARY KEY,
    player_id INT REFERENCES players(id) ON DELETE CASCADE,
    team_id INT REFERENCES teams(team_id) ON DELETE CASCADE,
    bid_amount DECIMAL(15,2) NOT NULL,
    bid_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Create Sold Players table
CREATE TABLE sold_players (
    player_id INT PRIMARY KEY REFERENCES players(id) ON DELETE CASCADE,
    team_id INT REFERENCES teams(team_id) ON DELETE CASCADE,
    sold_price DECIMAL(15,2) NOT NULL,
    session_id VARCHAR(100) DEFAULT 'default',
    sold_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Create Unsold Players table
CREATE TABLE unsold_players (
    id SERIAL PRIMARY KEY,
    player_id INT REFERENCES players(id) ON DELETE CASCADE,
    reason TEXT,
    added_on TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ==========================================
-- INDEXES FOR FOREIGN KEYS (DB-02 Optimization)
-- ==========================================
CREATE INDEX IF NOT EXISTS idx_bids_player_id ON bids(player_id);
CREATE INDEX IF NOT EXISTS idx_bids_team_id ON bids(team_id);
CREATE INDEX IF NOT EXISTS idx_sold_players_team_id ON sold_players(team_id);
CREATE INDEX IF NOT EXISTS idx_player_teams_team_id ON player_teams(team_id);
CREATE INDEX IF NOT EXISTS idx_live_bids_team_id ON live_bids(team_id);
CREATE INDEX IF NOT EXISTS idx_users_team_id ON users(team_id);

-- ==========================================
-- SUPABASE AUTH USER SYNCHRONIZATION TRIGGER
-- ==========================================

-- Trigger function to automatically sync new auth users to public.users
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger AS $$
BEGIN
  INSERT INTO public.users (id, name, email, role, team_id)
  VALUES (
    new.id,
    COALESCE(new.raw_user_meta_data->>'name', ''),
    new.email,
    COALESCE(new.raw_app_meta_data->>'role', 'team'),
    (new.raw_user_meta_data->>'team_id')::integer
  );
  RETURN new;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Trigger execution
DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE PROCEDURE public.handle_new_user();
