# Shazamme Data Quality Tool — Design Document

**Created:** 2026-07-16
**Status:** v2 — Multi-database (MSSQL read-only + PostgreSQL)

---

## Purpose

A lightweight local tool for ops/support staff to debug and audit data quality in the Shazamme-to-Bullhorn integration pipeline. The app connects to the Shazamme MSSQL database (read-only) to load advertiser configurations, stores working copies in a PostgreSQL database (`Shazamme_DataQuality`), and uses the Bullhorn REST API to find duplicate candidate records.

---

## Architecture

```
                                    ┌─────────────────────────┐
                                    │  Shazamme MSSQL (prod)  │
                                    │  READ-ONLY              │
                                    │  dbo.Advertiser         │
                                    │  dbo.ExternalSystem     │
                                    └────────▲────────────────┘
                                             │ pymssql
                                             │ (fetch advertisers)
Browser (localhost)          Python HTTP Server
┌──────────────────┐        ┌──────────────────────┐
│                  │        │                      │         ┌──────────────────────┐
│  Top panel:      │  POST  │  ThreadingHTTP       │ psycopg2│  PostgreSQL (RDS)    │
│  Add Advertiser  │───────▶│  Server (8000)       │◄───────▶│  Shazamme_DataQuality│
│  dropdown        │        │                      │         │  Advertiser table    │
│                  │        │  - /api/advertisers/* │         └──────────────────────┘
│  Left panel:     │◀──────│  - /api/find          │
│  Advertiser list │  JSON  │                      │         ┌──────────────────────┐
│                  │        │                      │  HTTP   │  Bullhorn REST API   │
│  Right panel:    │        │                      │────────▶│  /search/Candidate   │
│  Advertiser      │        │                      │◀────────│                      │
│  details         │        └──────────────────────┘         └──────────────────────┘
│                  │
│  Bottom panel:   │
│  Duplicate       │
│  results         │
└──────────────────┘
```

### Key constraints

- **MSSQL is read-only.** The app never writes to the Shazamme production database.
- **PostgreSQL is the working database.** All advertiser data used by the app is copied into `Shazamme_DataQuality.Advertiser`.
- **Bullhorn REST API** is called using credentials stored in the PostgreSQL Advertiser record (RestURL, SessionToken, etc.).

---

## Databases

### MSSQL — Shazamme Production (read-only)

| Detail | Value |
|--------|-------|
| Host | `shazamme-db-prod.c2co6ncidqsp.us-east-1.rds.amazonaws.com` |
| Port | 1433 |
| Database | `Shazamme` |
| Driver | `pymssql` |

**Tables used (read-only):**

**`dbo.Advertiser`** — source of advertiser/client configuration. ~188 validated Bullhorn advertisers.

**`dbo.ExternalSystem`** — lookup table for integration type. Bullhorn's `ExternalSystemID` = `7fe50749-e1b2-4cc7-970f-a6b4de0066e4`.

**Filter criteria for dropdown:**
```sql
SELECT a.AdvertiserID, a.Company, a.BullhornClientID, a.BullhornClientSecret,
       a.BullhornAPIUsername, a.BullhornAPIPassword, a.BullhornSessionToken,
       a.BullhornCorpToken, a.BullhornSwimlane, a.BullhornRestURL
FROM dbo.Advertiser a
INNER JOIN dbo.ExternalSystem es ON a.ExternalSystemID = es.ExternalSystemID
WHERE a.IsValidated = 1
  AND es.ExternalSystem = 'Bullhorn'
ORDER BY a.Company
```

### PostgreSQL — Shazamme_DataQuality (read-write)

| Detail | Value |
|--------|-------|
| Host | `vysta-process-db.c2co6ncidqsp.us-east-1.rds.amazonaws.com` |
| Port | 5432 |
| Database | `Shazamme_DataQuality` |
| Driver | `psycopg2` |

**`Advertiser` table:**

| Column | Type | Source (MSSQL) |
|--------|------|----------------|
| `Id` | SERIAL (PK) | auto-generated |
| `AdvertiserID` | UUID, UNIQUE, NOT NULL | `dbo.Advertiser.AdvertiserID` |
| `Company` | VARCHAR(100) | `dbo.Advertiser.Company` |
| `BullhornClientID` | VARCHAR(255) | `dbo.Advertiser.BullhornClientID` |
| `BullhornClientSecret` | VARCHAR(255) | `dbo.Advertiser.BullhornClientSecret` |
| `BullhornAPIUsername` | VARCHAR(255) | `dbo.Advertiser.BullhornAPIUsername` |
| `BullhornAPIPassword` | VARCHAR(255) | `dbo.Advertiser.BullhornAPIPassword` |
| `BullhornSessionToken` | VARCHAR(500) | `dbo.Advertiser.BullhornSessionToken` |
| `BullhornCorpToken` | VARCHAR(255) | `dbo.Advertiser.BullhornCorpToken` |
| `BullhornSwimlane` | VARCHAR(255) | `dbo.Advertiser.BullhornSwimlane` |
| `BullhornRestURL` | VARCHAR(500) | `dbo.Advertiser.BullhornRestURL` |

**`Candidate` table** — stores duplicate candidates found via Bullhorn API, with Shazamme verification:

| Column | Type | Description |
|--------|------|-------------|
| `Id` | SERIAL (PK) | auto-generated |
| `AdvertiserId` | INT, NOT NULL, FK → Advertiser.Id | Links to the advertiser context |
| `BullhornCandidateID` | VARCHAR(50), NOT NULL | Bullhorn's candidate ID |
| `CandidateName` | VARCHAR(200) | Full name from Bullhorn |
| `Email` | VARCHAR(250) | Can be NULL |
| `AddedDate` | DATE | `dateAdded` from Bullhorn |
| `ExistsInShazamme` | BOOLEAN, DEFAULT FALSE | TRUE if found in MSSQL `dbo.Candidate` |
| `DuplicateSetNumber` | INT | Which duplicate group (1, 2, 3...) |
| `CheckedOn` | TIMESTAMP | When the check was run |
| **UNIQUE** | `(AdvertiserId, BullhornCandidateID)` | Prevents re-inserting same candidate per advertiser |

**Shazamme verification logic**: Checks MSSQL `dbo.Candidate.BullhornCandidateID` first; falls back to `dbo.Candidate.EMail` if BH ID not found. If neither matches, `ExistsInShazamme = FALSE` (false alarm — candidate was not posted by Shazamme).

**`_migrations` table** — tracks applied migration scripts.

---

## Data Flow

### Flow 1: Add Advertiser (MSSQL → PostgreSQL)

1. User opens the app. The **top panel** shows a dropdown labeled "Add Advertiser".
2. Dropdown is populated by querying MSSQL: all advertisers where `IsValidated = 1` AND `ExternalSystem = 'Bullhorn'`, ordered by Company name.
3. User selects an advertiser from the dropdown and clicks "Add".
4. Server reads the full record from MSSQL and inserts/upserts into the PostgreSQL `Advertiser` table, mapping columns as defined above.
5. The left panel refreshes to show the updated list.

### Flow 2: Browse Advertisers (PostgreSQL → UI)

1. **Left panel** displays all advertisers stored in the PostgreSQL `Advertiser` table as a selectable list (showing Company name).
2. User clicks an advertiser in the list.
3. **Right panel** displays all columns for the selected advertiser (Id, AdvertiserID, Company, BullhornClientID, BullhornClientSecret, BullhornAPIUsername, BullhornAPIPassword, BullhornSessionToken, BullhornCorpToken, BullhornSwimlane, BullhornRestURL).

### Flow 3: Refresh Bullhorn Token

1. User clicks "Refresh Token" button on the right panel (or it is called automatically before "Find Duplicates").
2. Server runs the 3-step Bullhorn OAuth flow using stored credentials:
   - **Step 1**: `GET auth.bullhornstaffing.com/oauth/authorize` with client_id, username, password → auth code (follows redirects manually)
   - **Step 2**: `POST auth.bullhornstaffing.com/oauth/token` with auth code, client_id, client_secret → access token
   - **Step 3**: `POST rest.bullhornstaffing.com/rest-services/login` with access token → BhRestToken + restUrl
3. Swimlane and corp token are parsed from the restUrl.
4. Updated values saved to PostgreSQL Advertiser record.
5. Retry logic: if any step fails, waits 10 seconds and retries the full flow (mirrors the C# plugin behavior).

### Flow 4: Find Duplicates (PostgreSQL + Bullhorn API + MSSQL verification → UI)

1. User selects an advertiser from the left panel. The right panel shows its details.
2. User picks a date and match mode, then clicks "Find Duplicates".
3. Token is automatically refreshed (Flow 3) before the search.
4. Server uses the refreshed `BullhornRestURL` and `BullhornSessionToken` to call the Bullhorn REST API.
5. Duplicate detection runs using the union-find algorithm.
6. **Shazamme verification**: each duplicate candidate is looked up in MSSQL `dbo.Candidate` by `BullhornCandidateID` (fallback: `EMail`) to check if it was created by Shazamme.
7. Duplicate candidates are saved to PostgreSQL `Candidate` table with the `ExistsInShazamme` flag.
8. Results display with a "In Shazamme" column: **Yes** (confirmed from Shazamme), **No (false alarm)** (not from Shazamme), or **N/A**.
9. CSV export includes the `exists_in_shazamme` column.

---

## UI Layout (v2)

```
┌─────────────────────────────────────────────────────────────────┐
│  Header: Shazamme Data Quality Tool                             │
├─────────────────────────────────────────────────────────────────┤
│  TOP PANEL: [Add Advertiser ▼ dropdown]  [Add button]           │
├────────────────────┬────────────────────────────────────────────┤
│  LEFT PANEL        │  RIGHT PANEL                               │
│                    │                                            │
│  Advertiser list   │  Selected advertiser details               │
│  (from PostgreSQL) │  - AdvertiserID                            │
│                    │  - Company                                 │
│  > Company A  ◄──  │  - BullhornClientID                       │
│    Company B       │  - BullhornClientSecret                    │
│    Company C       │  - BullhornAPIUsername                     │
│    Company D       │  - BullhornAPIPassword                     │
│    ...             │  - BullhornSessionToken                    │
│                    │  - BullhornCorpToken                       │
│                    │  - BullhornSwimlane                        │
│                    │  - BullhornRestURL                         │
│                    ├────────────────────────────────────────────┤
│                    │  Duplicate Finder                           │
│                    │  [Date] [Match on ▼] [Page size] [Find]    │
│                    │                                            │
│                    │  Results / stats / clusters                 │
└────────────────────┴────────────────────────────────────────────┘
```

### Panel Details

**Top Panel — Add Advertiser**
- Dropdown populated from MSSQL (read-only query).
- Shows `Company` name in the dropdown.
- "Add" button copies the selected advertiser into PostgreSQL.
- Advertisers already in PostgreSQL should be indicated (e.g., grayed out or tagged) to prevent confusion.

**Left Panel — Advertiser List**
- Lists all advertisers from the PostgreSQL `Advertiser` table.
- Displays Company name.
- Clickable — selecting one populates the right panel.
- Highlight the currently selected advertiser.

**Right Panel — Advertiser Details + Duplicate Finder**
- Top section: read-only display of all advertiser columns from PostgreSQL.
- Bottom section: the existing duplicate finder form (date, match mode, page size, find button) and results area.
- The REST URL and token fields are pre-filled from the selected advertiser's stored values (user no longer types them manually).

---

## API Endpoints (v2)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serves the HTML UI |
| `GET` | `/api/mssql/advertisers` | Fetch Bullhorn advertisers from MSSQL (for dropdown) |
| `GET` | `/api/advertisers` | List all advertisers from PostgreSQL (for left panel) |
| `POST` | `/api/advertisers` | Add an advertiser (copy from MSSQL to PostgreSQL) |
| `GET` | `/api/advertisers/<id>` | Get single advertiser details from PostgreSQL |
| `POST` | `/api/find` | Run duplicate report (uses selected advertiser's credentials) |

### GET /api/mssql/advertisers

Queries MSSQL for validated Bullhorn advertisers. Returns:
```json
[
  { "AdvertiserID": "735cad2e-...", "Company": "Empresaria Group PLC" },
  { "AdvertiserID": "8d6d23d4-...", "Company": "GECO Internal" }
]
```

### POST /api/advertisers

Copies an advertiser from MSSQL into PostgreSQL.

**Request body:**
```json
{ "AdvertiserID": "735cad2e-6390-4aa1-a604-0032e122e4b8" }
```

**Response:**
```json
{ "success": true, "Company": "Empresaria Group PLC" }
```

### GET /api/advertisers

Returns all advertisers stored in PostgreSQL.
```json
[
  {
    "Id": 1,
    "AdvertiserID": "735cad2e-...",
    "Company": "Empresaria Group PLC",
    "BullhornSwimlane": "rest22"
  }
]
```

### GET /api/advertisers/<id>

Returns full details for one advertiser (all columns).

### POST /api/find

Same as v1, but `restUrl` and `token` can be omitted if `advertiserId` is provided — the server looks them up from PostgreSQL.
```json
{
  "advertiserId": 1,
  "date": "2026-07-16",
  "matchOn": "email",
  "count": 500
}
```

---

## Duplicate Detection Algorithm

_Unchanged from v1._

**Approach:** Union-Find (disjoint set)

1. Every candidate starts in its own set (keyed by Bullhorn `id`).
2. For each candidate:
   - **Email matching** (`email` or `either` mode): normalize email (lowercase + trim). Same email → merge sets.
   - **Name matching** (`name` or `either` mode): normalize name (lowercase + collapse whitespace). Same name → merge sets.
3. In `either` mode, transitive merges apply (A shares email with B, B shares name with C → {A, B, C}).
4. Only groups with 2+ members are reported.
5. Sorted: largest first, then by email/name for stability.

### Normalization Rules

| Field | Normalization |
|-------|---------------|
| Email | `.strip().lower()` — exact match after trim + lowercase |
| Name  | `.lower()` then collapse all whitespace to single spaces |

### Limitations

- No fuzzy/partial matching.
- No phonetic matching.
- Email is exact match only.

---

## Error Handling

| Error type | Handling |
|------------|----------|
| MSSQL connection failure | Error message in dropdown area |
| PostgreSQL connection failure | Error message on page load |
| Bullhorn `HTTPError` | Surfaces HTTP status + response excerpt |
| Bullhorn `URLError` | Reports network/DNS failure |
| `ValueError` (validation) | Missing required fields |
| Duplicate `AdvertiserID` insert | Upsert or user-friendly "already added" message |

---

## Migration Scripts

All database schema changes are tracked as numbered SQL migration scripts in `migrations/`.

| Script | Description |
|--------|-------------|
| `001_create_advertiser_table.sql` | Create Advertiser table with Id, AdvertiserID, BullhornClientID, BullhornClientSecret |
| `002_add_bullhorn_api_columns.sql` | Add BullhornAPIUsername, BullhornAPIPassword, BullhornSessionToken, BullhornCorpToken, BullhornSwimlane, BullhornRestURL |
| `003_add_company_column.sql` | Add Company column |
| `004_create_candidate_table.sql` | Create Candidate table with Shazamme verification flag, FK to Advertiser, unique constraint on (AdvertiserId, BullhornCandidateID) |

**Runner:** `python migrations/migrate.py`

| Command | Description |
|---------|-------------|
| `python migrate.py` | Apply all pending migrations |
| `python migrate.py --status` | Show applied and pending migrations |
| `python migrate.py --reset` | Drop the tracking table (not the data) |

---

## Configuration (.env)

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

The `.env` file is excluded from version control via `.gitignore`.

---

## Running the App

```bash
# Install dependencies (one-time)
pip install psycopg2-binary pymssql python-dotenv

# Apply database migrations
python migrations/migrate.py

# Start the app
python bullhorn_dupe_finder.py          # default port 8000
python bullhorn_dupe_finder.py 9001     # custom port
```

Open `http://localhost:<port>` in a browser. Press `Ctrl+C` to stop.

---

## File Structure

```
Check duplicates/
├── bullhorn_dupe_finder.py              # main app (server + UI + logic)
├── DESIGN.md                            # this file
├── .env                                 # database credentials (git-ignored)
├── .gitignore                           # excludes .env
└── migrations/
    ├── migrate.py                       # migration runner
    ├── 001_create_advertiser_table.sql
    ├── 002_add_bullhorn_api_columns.sql
    ├── 003_add_company_column.sql
    └── 004_create_candidate_table.sql
```

---

## Planned Enhancements

- [ ] **Date range support** — search across multiple days, not just one
- [ ] **Fuzzy name matching** — catch near-duplicates like "Jon" / "John"
- [ ] **Bulk actions** — mark/merge duplicates directly from the UI
- [ ] **Auto-refresh Bullhorn tokens** — use stored OAuth credentials to refresh expired session tokens
- [ ] **Scheduled reports** — run duplicate checks on a schedule and notify the user
- [ ] _(add more here)_
