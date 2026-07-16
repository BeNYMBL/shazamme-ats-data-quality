# Shazamme ATS Data Quality Tool

A lightweight local tool for debugging and auditing data quality in the Shazamme-to-Bullhorn integration pipeline. Find duplicate candidates in Bullhorn and verify whether they were created by Shazamme.

## Features

- **Advertiser Management** — Load validated Bullhorn advertisers from the Shazamme MSSQL database and store them locally in PostgreSQL
- **Bullhorn Token Refresh** — Automated 3-step OAuth flow to refresh Bullhorn session tokens
- **Duplicate Detection** — Find duplicate candidates added on a given day, matched by email, name, or either
- **Shazamme Verification** — Cross-references each duplicate against the Shazamme database to confirm if it was posted by Shazamme or is a false alarm
- **CSV Export** — Download results with duplicate set, candidate details, and Shazamme verification status

## Prerequisites

- Python 3.10+
- Access to the Shazamme MSSQL database (read-only)
- Access to the PostgreSQL RDS instance

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/BeNYMBL/shazamme-ats-data-quality.git
cd shazamme-ats-data-quality

# 2. Install dependencies
pip install psycopg2-binary pymssql python-dotenv

# 3. Create a .env file in the project root with your database credentials
```

### .env file format

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

Contact your team lead for the actual credentials. The `.env` file is git-ignored and must not be committed.

### Run database migrations

```bash
python migrations/migrate.py
```

Check migration status at any time:

```bash
python migrations/migrate.py --status
```

## Running the App

```bash
python bullhorn_dupe_finder.py            # default port 8000
python bullhorn_dupe_finder.py 9001       # custom port
```

Open **http://localhost:8000** in your browser.

## How to Use

1. **Add an advertiser** — Use the dropdown at the top to select a Bullhorn advertiser from Shazamme and click "Add"
2. **Select an advertiser** — Click on an advertiser in the left panel to view its details
3. **Refresh token** — Click "Refresh Token" to get a fresh Bullhorn session token (also happens automatically before each search)
4. **Find duplicates** — Pick a date, match mode, and click "Find Duplicates"
5. **Review results** — Each duplicate shows whether it exists in the Shazamme database:
   - **Yes** — Candidate was posted by Shazamme (real duplicate)
   - **No (false alarm)** — Candidate was not found in Shazamme
6. **Export** — Click "Export CSV" to download the results

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
