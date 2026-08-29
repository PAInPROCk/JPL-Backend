# 📦 Offline / Local MySQL Fallback Tools

This directory preserves the legacy MySQL schema and maintenance scripts for the JPL / AUCTRA platform.

---

## 📌 Purpose

The primary production architecture of JPL uses **Supabase PostgreSQL** (`postgresql_schema.sql`) and **Supabase Auth** (`create_supabase_admin.py`).

In the event of an emergency, severe WAN internet disruption, or offline deployment requirement where cloud Supabase cannot be reached during an auction event, these legacy assets allow you to spin up a local MySQL instance (`127.0.0.1:3306`) as a disaster recovery fallback.

---

## 📂 Included Assets

| File | Description |
| :--- | :--- |
| **`jpl_schema.sql`** | Complete MySQL relational schema including `teams`, `players`, `bids`, `users`, and `current_auction` tables. |
| **`create_admin.py`** | Seeds an initial administrator account into a local MySQL database with bcrypt-hashed credentials. |
| **`generate_hash.py`** | Utility to generate bcrypt password hashes for manual database insertions. |
| **`migration_v1.py`** | Schema migration runner for local MySQL instances. |

---

## 🚀 How to Use for Local Offline Fallback

1. Start your local MySQL server (e.g. via XAMPP, Docker, or native service on `127.0.0.1:3306`).
2. Import the schema into MySQL:
   ```bash
   mysql -u root -p jpl < scripts/legacy_mysql/jpl_schema.sql
   ```
3. Seed the local admin account:
   ```bash
   python scripts/legacy_mysql/create_admin.py
   ```
4. Configure local MySQL connection details in `.env`:
   ```env
   MYSQLHOST=127.0.0.1
   MYSQLPORT=3306
   MYSQLUSER=root
   MYSQLPASSWORD=your_password
   MYSQLDATABASE=jpl
   ```
