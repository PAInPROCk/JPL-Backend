
CREATE DATABASE IF NOT EXISTS jpl;
USE jpl;

-- Drop tables if exist for a clean setup
DROP TABLE IF EXISTS bids;
DROP TABLE IF EXISTS players;
DROP TABLE IF EXISTS teams;

-- Teams table
CREATE TABLE teams (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    budget DECIMAL(10,2) DEFAULT 20000.00
);

-- Players table
CREATE TABLE players (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    nickname VARCHAR(255),
    age INT,
    category VARCHAR(100),
    type VARCHAR(100),
    base_price DECIMAL(10,2),
    total_runs INT,
    highest_runs INT,
    wickets_taken INT,
    times_out INT,
    teams_played TEXT,
    image_path VARCHAR(255)
);

-- Bids table
CREATE TABLE bids (
    id INT AUTO_INCREMENT PRIMARY KEY,
    player_id INT,
    team_id INT,
    bid_amount DECIMAL(10,2),
    bid_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (player_id) REFERENCES players(id),
    FOREIGN KEY (team_id) REFERENCES teams(id)
);

-- Insert sample teams
INSERT INTO teams (name, budget) VALUES
('JPL Warriors', 25000.00),
('JPL Titans', 30000.00),
('JPL Challengers', 20000.00),
('JPL Strikers', 15000.00);

-- Insert sample players
INSERT INTO players (name, nickname, age, category, type, base_price, total_runs, highest_runs, wickets_taken, times_out, teams_played, image_path) VALUES
('Prathamesh Bhadwalkar', 'PB', 19, 'Batsman', 'Right Handed', 5000, 1200, 90, 5, 20, 'JPL Warriors', '/assets/images/player1.png'),
('Amit Sharma', 'Ami', 22, 'Bowler', 'Left Arm Spinner', 4000, 500, 45, 40, 15, 'JPL Titans', '/assets/images/player2.png'),
('Rahul Verma', 'RV', 21, 'All-Rounder', 'Right Handed', 7000, 1500, 100, 20, 25, 'JPL Challengers', '/assets/images/player3.png'),
('Suresh Iyer', 'SI', 23, 'Batsman', 'Right Handed', 6000, 1800, 110, 2, 30, 'JPL Strikers', '/assets/images/player4.png'),
('Karan Patel', 'KP', 20, 'Bowler', 'Right Arm Fast', 3500, 300, 30, 50, 10, 'JPL Titans', '/assets/images/player5.png'),
('Neha Kulkarni', 'NK', 18, 'Batsman', 'Left Handed', 3000, 800, 60, 0, 18, 'JPL Challengers', '/assets/images/player6.png'),
('Vikas Yadav', 'Vicky', 25, 'All-Rounder', 'Right Handed', 8000, 2200, 130, 30, 28, 'JPL Warriors', '/assets/images/player7.png'),
('Deepak Joshi', 'DJ', 24, 'Bowler', 'Left Arm Fast', 4500, 400, 40, 45, 12, 'JPL Strikers', '/assets/images/player8.png'),
('Anjali Mehta', 'Anju', 19, 'Batsman', 'Right Handed', 5500, 1000, 70, 3, 20, 'JPL Warriors', '/assets/images/player9.png'),
('Sanjay Rao', 'SR', 26, 'All-Rounder', 'Left Handed', 9000, 2500, 150, 35, 32, 'JPL Titans', '/assets/images/player10.png');
