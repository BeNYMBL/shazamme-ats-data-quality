# Shazamme ATS Data Quality Tool

A lightweight local tool for debugging and auditing data quality in the Shazamme-to-Bullhorn integration pipeline. Find duplicate candidates in Bullhorn and verify whether they were created by Shazamme.

## Features

- **Advertiser Management** — Load validated Bullhorn advertisers from the Shazamme MSSQL database and store them locally in PostgreSQL
- **Bullhorn Token Refresh** — Automated 3-step OAuth flow to refresh Bullhorn session tokens
- **Duplicate Detection** — Find duplicate candidates added on a given day, matched by email, name, or either
- **Run All Report** — One-click duplicate scan across all advertisers with a real-time progress bar
- **Shazamme Verification** — Cross-references each duplicate against the Shazamme database to confirm if it was posted by Shazamme or is a false alarm
- **Candidate Detail** — Click any candidate to view full details (contact, location, professional info, source, status) from Bullhorn
- **CSV Export** — Download results with duplicate set, candidate details, and Shazamme verification status

## Prerequisites

- Python 3.10+
- Access to the Shazamme MSSQL database (read-only)
- Access to the PostgreSQL RDS instance (`Shazamme_DataQuality`)

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/BeNYMBL/shazamme-ats-data-quality.git
cd shazamme-ats-data-quality

# 2. Install dependencies
pip install psycopg2-binary pymssql python-dotenv

# 3. Create a .env file in the project root with your database credentials (see below)

# 4. Run database migrations
python migrations/migrate.py

# 5. Start the app
python bullhorn_dupe_finder.py
```

Open **http://localhost:8000** in your browser.

### .env file format

Create a `.env` file in the project root. Contact your team lead for the actual credentials.

```env
# PostgreSQL — Shazamme_DataQuality (read-write)
POSTGRES_HOST=
POSTGRES_PORT=5432
POSTGRES_DATABASE=Shazamme_DataQuality
POSTGRES_USERNAME=
POSTGRES_PASSWORD=

# MSSQL — Shazamme Production (read-only)
MSSQL_HOST=
MSSQL_PORT=1433
MSSQL_DB=
MSSQL_USER=
MSSQL_PASSWORD=
```

The `.env` file is git-ignored and must not be committed.

### Database migrations

Apply all pending migrations before first use:

```bash
python migrations/migrate.py
```

Check migration status at any time:

```bash
python migrations/migrate.py --status
```

## How to Use

### Step 1: Add Advertisers

Use the dropdown at the top of the page to select a Bullhorn advertiser from the Shazamme database and click **"Add"**. This copies the advertiser's credentials into the local PostgreSQL database. You can add as many advertisers as you want to monitor. To remove an advertiser, hover over its name in the left panel and click the **×** button.

### Step 2: Find Duplicates (Single Advertiser)

1. Click an advertiser in the **left panel** to view its details
2. Click **"Refresh Token"** to get a fresh Bullhorn session token (also happens automatically before each search)
3. Pick a **date**, **match mode** (email, name, or either), and click **"Find Duplicates"**
4. Review results — each duplicate shows whether it exists in the Shazamme database:
   - **Yes** — Candidate was posted by Shazamme (real duplicate)
   - **No (false alarm)** — Candidate was not found in Shazamme
5. Click **"Export CSV"** to download the results

After running a search, a **"View Report"** button appears so you can re-view the results without re-fetching. This persists across page refreshes.

### Step 3: Run All Report (All Advertisers)

1. Select a **date** and **match mode** in the **"Run All Report"** bar at the top
2. Click **"Run All Report"** — the app switches to a full-width report view
3. Watch the **progress bar** and **live feed** as each advertiser is processed (token refresh → fetch → detect → verify)
4. Once complete, review:
   - **Summary stats** — total checked, advertisers with duplicates, clean, failed
   - **Advertiser table** — click any row to expand and see duplicate sets
   - **Candidate details** — click any candidate row to open a slide-out panel with full Bullhorn details
5. Click **"Export CSV"** to download the full report across all advertisers
6. Click **"← Back to Advertisers"** to return to the main view

The report persists — after going back, click **"View Last Report"** to re-open it without re-running.

### Step 4: Investigate Candidates

In any duplicate results view, click on a **candidate row** to open the **slide-out detail panel** showing:
- Contact info (email, phone, mobile)
- Location (address)
- Professional details (occupation, employment preference, education)
- Source and ownership (source, owner, date added, last modified)
- Status

This fetches the latest data directly from Bullhorn on demand.

## Running the App

```bash
python bullhorn_dupe_finder.py            # default port 8000
python bullhorn_dupe_finder.py 9001       # custom port
```

Open **http://localhost:PORT** in your browser. Press `Ctrl+C` to stop.

## Project Structure

```
├── bullhorn_dupe_finder.py              # main app (server + UI + logic)
├── DESIGN.md                            # detailed design document
├── README.md                            # this file
├── .env                                 # database credentials (git-ignored)
├── .gitignore
└── migrations/
    ├── migrate.py                       # migration runner
    ├── 001_create_advertiser_table.sql
    ├── 002_add_bullhorn_api_columns.sql
    ├── 003_add_company_column.sql
    └── 004_create_candidate_table.sql
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `ModuleNotFoundError: No module named 'psycopg2'` | Run `pip install psycopg2-binary pymssql python-dotenv` |
| MSSQL connection error | Check your `.env` credentials and network access to the MSSQL host |
| PostgreSQL connection error | Check your `.env` credentials and network access to the PostgreSQL host |
| "Token refresh failed" | The advertiser's Bullhorn API credentials may be invalid or expired — check with the team lead |
| Port already in use | Use a different port: `python bullhorn_dupe_finder.py 9001` |
| Advertiser shows "Failed" in Run All Report | That advertiser's credentials are likely invalid — it will be skipped and the report continues |
