#!/usr/bin/env python3
"""
Bullhorn Candidate Duplicate Finder
===================================
A lightweight, single-file, ZERO-dependency local app.

What it does
------------
1. Serves a small web UI (neat input fields, nothing is stored).
2. Takes your Bullhorn REST base URL, BhRestToken, and a date.
3. Calls /search/Candidate for candidates whose dateAdded falls on that day.
4. Follows pagination until every candidate for the day is retrieved.
5. Groups candidates that look like duplicates and reports
   name, email, and Bullhorn candidate id for each duplicate set.

Why a tiny Python server instead of a plain .html file?
-------------------------------------------------------
Browsers block cross-origin calls to bullhornstaffing.com (CORS), so a
pure client-side page cannot call the API. This server acts as a local
proxy: the browser talks to localhost, and localhost talks to Bullhorn.
Your token never leaves your machine and is never written to disk.

Run it
------
    python3 bullhorn_dupe_finder.py
Then open http://localhost:8000 in your browser.
(Optional: `python3 bullhorn_dupe_finder.py 9001` to use a different port.)
"""

import os
import sys
import json
import re
import time
import urllib.request
import urllib.parse
import urllib.error
from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import psycopg2
import psycopg2.extras
import pymssql
from dotenv import dotenv_values

# Load .env from same directory as this script
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV = dotenv_values(os.path.join(_SCRIPT_DIR, ".env"))

DEFAULT_PORT = 8000
# Fields we ask Bullhorn to return for each candidate.
FIELDS = "id,firstName,lastName,name,email,dateAdded"


# --------------------------------------------------------------------------- #
#  Database connections
# --------------------------------------------------------------------------- #
def get_pg():
    """Return a connection to the PostgreSQL Shazamme_DataQuality database."""
    return psycopg2.connect(
        host=_ENV["POSTGRES_HOST"],
        port=_ENV["POSTGRES_PORT"],
        user=_ENV["POSTGRES_USERNAME"],
        password=_ENV["POSTGRES_PASSWORD"],
        dbname=_ENV["POSTGRES_DATABASE"],
    )


def get_mssql():
    """Return a read-only connection to the Shazamme MSSQL database."""
    return pymssql.connect(
        server=_ENV["MSSQL_HOST"],
        port=_ENV["MSSQL_PORT"],
        user=_ENV["MSSQL_USER"],
        password=_ENV["MSSQL_PASSWORD"],
        database=_ENV["MSSQL_DB"],
    )


# --------------------------------------------------------------------------- #
#  Advertiser CRUD (MSSQL → PostgreSQL)
# --------------------------------------------------------------------------- #
_BULLHORN_COLUMNS = [
    "BullhornClientID", "BullhornClientSecret",
    "BullhornAPIUsername", "BullhornAPIPassword",
    "BullhornSessionToken", "BullhornCorpToken",
    "BullhornSwimlane", "BullhornRestURL",
]

_PG_ADVERTISER_COLS = [
    "Id", "AdvertiserID", "Company",
] + _BULLHORN_COLUMNS


def fetch_mssql_advertisers():
    """Fetch validated Bullhorn advertisers from MSSQL (read-only)."""
    conn = get_mssql()
    cur = conn.cursor(as_dict=True)
    cur.execute("""
        SELECT a.AdvertiserID, a.Company
        FROM dbo.Advertiser a
        INNER JOIN dbo.ExternalSystem es ON a.ExternalSystemID = es.ExternalSystemID
        WHERE a.IsValidated = 1 AND es.ExternalSystem = 'Bullhorn'
        ORDER BY a.Company
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    # Convert UUIDs to strings
    for r in rows:
        r["AdvertiserID"] = str(r["AdvertiserID"])
    return rows


def fetch_mssql_advertiser_full(advertiser_id: str) -> dict:
    """Fetch a single advertiser's full Bullhorn details from MSSQL."""
    conn = get_mssql()
    cur = conn.cursor(as_dict=True)
    cur.execute("""
        SELECT a.AdvertiserID, a.Company,
               a.BullhornClientID, a.BullhornClientSecret,
               a.BullhornAPIUsername, a.BullhornAPIPassword,
               a.BullhornSessionToken, a.BullhornCorpToken,
               a.BullhornSwimlane, a.BullhornRestURL
        FROM dbo.Advertiser a
        INNER JOIN dbo.ExternalSystem es ON a.ExternalSystemID = es.ExternalSystemID
        WHERE a.IsValidated = 1 AND es.ExternalSystem = 'Bullhorn'
          AND a.AdvertiserID = %s
    """, (advertiser_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        for k, v in row.items():
            if hasattr(v, "hex"):  # UUID
                row[k] = str(v)
            elif v is None:
                row[k] = ""
            else:
                row[k] = str(v)
    return row


def upsert_advertiser_to_pg(data: dict):
    """Insert or update an advertiser in PostgreSQL."""
    conn = get_pg()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO "Advertiser" (
            "AdvertiserID", "Company",
            "BullhornClientID", "BullhornClientSecret",
            "BullhornAPIUsername", "BullhornAPIPassword",
            "BullhornSessionToken", "BullhornCorpToken",
            "BullhornSwimlane", "BullhornRestURL"
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT ("AdvertiserID") DO UPDATE SET
            "Company" = EXCLUDED."Company",
            "BullhornClientID" = EXCLUDED."BullhornClientID",
            "BullhornClientSecret" = EXCLUDED."BullhornClientSecret",
            "BullhornAPIUsername" = EXCLUDED."BullhornAPIUsername",
            "BullhornAPIPassword" = EXCLUDED."BullhornAPIPassword",
            "BullhornSessionToken" = EXCLUDED."BullhornSessionToken",
            "BullhornCorpToken" = EXCLUDED."BullhornCorpToken",
            "BullhornSwimlane" = EXCLUDED."BullhornSwimlane",
            "BullhornRestURL" = EXCLUDED."BullhornRestURL"
    """, (
        data["AdvertiserID"], data.get("Company", ""),
        data.get("BullhornClientID", ""), data.get("BullhornClientSecret", ""),
        data.get("BullhornAPIUsername", ""), data.get("BullhornAPIPassword", ""),
        data.get("BullhornSessionToken", ""), data.get("BullhornCorpToken", ""),
        data.get("BullhornSwimlane", ""), data.get("BullhornRestURL", ""),
    ))
    conn.commit()
    cur.close()
    conn.close()


def list_pg_advertisers():
    """List all advertisers from PostgreSQL."""
    conn = get_pg()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT "Id", "AdvertiserID"::text, "Company", "BullhornSwimlane"
        FROM "Advertiser" ORDER BY "Company"
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def get_pg_advertiser(adv_id: int):
    """Get a single advertiser by PK from PostgreSQL."""
    conn = get_pg()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT "Id", "AdvertiserID"::text, "Company",
               "BullhornClientID", "BullhornClientSecret",
               "BullhornAPIUsername", "BullhornAPIPassword",
               "BullhornSessionToken", "BullhornCorpToken",
               "BullhornSwimlane", "BullhornRestURL"
        FROM "Advertiser" WHERE "Id" = %s
    """, (adv_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else None


def update_pg_advertiser_tokens(adv_id: int, session_token: str, rest_url: str,
                                 swimlane: str, corp_token: str):
    """Update Bullhorn session tokens in PostgreSQL after a refresh."""
    conn = get_pg()
    cur = conn.cursor()
    cur.execute("""
        UPDATE "Advertiser"
        SET "BullhornSessionToken" = %s,
            "BullhornRestURL" = %s,
            "BullhornSwimlane" = %s,
            "BullhornCorpToken" = %s
        WHERE "Id" = %s
    """, (session_token, rest_url, swimlane, corp_token, adv_id))
    conn.commit()
    cur.close()
    conn.close()


# --------------------------------------------------------------------------- #
#  Candidate persistence + Shazamme verification
# --------------------------------------------------------------------------- #
def _check_exists_in_shazamme(candidates: list) -> dict:
    """Check which candidates exist in MSSQL dbo.Candidate.
    Returns a dict of BullhornCandidateID -> True/False.
    Matches by BullhornCandidateId. If BullhornCandidateId is empty, falls back to Email.
    """
    if not candidates:
        return {}

    conn = get_mssql()
    cur = conn.cursor()
    result = {}

    for c in candidates:
        bh_id = str(c.get("id", "")).strip()
        email = (c.get("email") or "").strip().lower()

        if bh_id:
            cur.execute(
                "SELECT COUNT(*) FROM dbo.Candidate WHERE BullhornCandidateID = %s",
                (bh_id,),
            )
            count = cur.fetchone()[0]
            if count > 0:
                result[bh_id] = True
                continue
            # Fallback: check by email if BH ID not found
            if email:
                cur.execute(
                    "SELECT COUNT(*) FROM dbo.Candidate WHERE EMail = %s",
                    (email,),
                )
                count = cur.fetchone()[0]
                result[bh_id] = count > 0
            else:
                result[bh_id] = False
        else:
            result[bh_id] = False

    cur.close()
    conn.close()
    return result


def save_duplicate_candidates(adv_id: int, groups: list, all_candidates: list,
                               date_str: str):
    """Save duplicate candidates to PostgreSQL and verify against Shazamme MSSQL.
    Deletes existing candidates for this advertiser before inserting fresh results.
    Returns enriched groups with ExistsInShazamme flag per member.
    """
    # Delete previous candidates for this advertiser
    conn = get_pg()
    cur = conn.cursor()
    cur.execute('DELETE FROM "Candidate" WHERE "AdvertiserId" = %s', (adv_id,))
    conn.commit()
    cur.close()
    conn.close()

    # Collect all duplicate candidate records
    dup_candidates = []
    for g in groups:
        for m in g:
            dup_candidates.append(m)

    if not dup_candidates:
        return []

    # Check which exist in Shazamme
    shazamme_check = _check_exists_in_shazamme(dup_candidates)

    # Save to PostgreSQL
    conn = get_pg()
    cur = conn.cursor()
    set_number = 0
    enriched_groups = []

    for g in groups:
        set_number += 1
        enriched_members = []
        for c in sorted(g, key=lambda c: c.get("id", 0)):
            bh_id = str(c.get("id", "")).strip()
            name = (c.get("name") or "").strip()
            if not name:
                parts = [(c.get("firstName") or "").strip(),
                         (c.get("lastName") or "").strip()]
                name = " ".join(p for p in parts if p)
            email = (c.get("email") or "").strip()
            exists = shazamme_check.get(bh_id, False)

            # Upsert — skip if already saved
            cur.execute("""
                INSERT INTO "Candidate"
                    ("AdvertiserId", "BullhornCandidateID", "CandidateName",
                     "Email", "AddedDate", "ExistsInShazamme", "DuplicateSetNumber")
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT ("AdvertiserId", "BullhornCandidateID") DO UPDATE SET
                    "CandidateName" = EXCLUDED."CandidateName",
                    "Email" = EXCLUDED."Email",
                    "AddedDate" = EXCLUDED."AddedDate",
                    "ExistsInShazamme" = EXCLUDED."ExistsInShazamme",
                    "DuplicateSetNumber" = EXCLUDED."DuplicateSetNumber",
                    "CheckedOn" = NOW()
            """, (adv_id, bh_id, name or None, email or None,
                  date_str, exists, set_number))

            enriched_members.append({
                "id": c.get("id"),
                "name": name or "(no name)",
                "email": email or "(no email)",
                "existsInShazamme": exists,
            })

        enriched_groups.append({
            "size": len(enriched_members),
            "members": enriched_members,
        })

    conn.commit()
    cur.close()
    conn.close()
    return enriched_groups


# --------------------------------------------------------------------------- #
#  Bullhorn Token Refresh (OAuth flow)
# --------------------------------------------------------------------------- #
def _bh_get_auth_code(client_id: str, username: str, password: str) -> str:
    """Step 1: OAuth authorize — get an auth code by following redirects."""
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "action": "Login",
        "username": username,
        "password": password,
    })
    url = "https://auth.bullhornstaffing.com/oauth/authorize?" + params

    # Follow redirects manually to capture the auth code from a localhost redirect
    max_redirects = 10
    for _ in range(max_redirects):
        req = urllib.request.Request(url, method="GET")
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            # No redirect — check final URL for code
            final_url = resp.geturl()
            parsed = urlparse(final_url)
            qs = parse_qs(parsed.query)
            if "code" in qs:
                return qs["code"][0]
            raise ValueError(f"No auth code in final response URL: {final_url}")
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307):
                location = e.headers.get("Location", "")
                parsed = urlparse(location)
                if parsed.hostname and parsed.hostname.lower() == "localhost":
                    qs = parse_qs(parsed.query)
                    if "code" in qs:
                        return qs["code"][0]
                    raise ValueError("Auth redirect to localhost but no code parameter.")
                url = location
                continue
            raise
    raise ValueError("Too many redirects while obtaining auth code.")


def _bh_get_access_token(auth_code: str, client_id: str, client_secret: str) -> str:
    """Step 2: Exchange auth code for an access token."""
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": auth_code,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode("utf-8")
    url = "https://auth.bullhornstaffing.com/oauth/token"

    # May need to follow a 307 redirect
    for _ in range(3):
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            body = json.loads(resp.read().decode("utf-8"))
            if "access_token" not in body:
                raise ValueError("No access_token in response.")
            return body["access_token"]
        except urllib.error.HTTPError as e:
            if e.code == 307:
                url = e.headers.get("Location", url)
                continue
            detail = e.read().decode("utf-8", "replace")[:500]
            raise ValueError(f"Access token request failed (HTTP {e.code}): {detail}")
    raise ValueError("Too many redirects while obtaining access token.")


def _bh_get_rest_token(access_token: str) -> dict:
    """Step 3: Login to REST API to get BhRestToken and restUrl."""
    url = "https://rest.bullhornstaffing.com/rest-services/login?version=*&access_token=" + access_token

    for _ in range(3):
        req = urllib.request.Request(url, data=b"", method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            body = json.loads(resp.read().decode("utf-8"))
            if "BhRestToken" not in body or "restUrl" not in body:
                raise ValueError(f"Missing BhRestToken or restUrl in response: {body}")
            return body
        except urllib.error.HTTPError as e:
            if e.code == 307:
                url = e.headers.get("Location", url)
                continue
            detail = e.read().decode("utf-8", "replace")[:500]
            raise ValueError(f"REST login failed (HTTP {e.code}): {detail}")
    raise ValueError("Too many redirects while obtaining REST token.")


def _parse_rest_url(rest_url: str) -> tuple:
    """Extract swimlane and corp token from a Bullhorn restUrl."""
    parsed = urlparse(rest_url)
    # Swimlane = first part of hostname (e.g., "rest22" from "rest22.bullhornstaffing.com")
    swimlane = parsed.hostname.split(".")[0] if parsed.hostname else ""
    # Corp token = second path segment (e.g., "5ac5s0" from "/rest-services/5ac5s0/")
    parts = [p for p in parsed.path.split("/") if p]
    corp_token = parts[1] if len(parts) > 1 else ""
    return swimlane, corp_token


def refresh_bullhorn_token(adv_id: int) -> dict:
    """Full Bullhorn token refresh for an advertiser. Returns updated fields."""
    adv = get_pg_advertiser(adv_id)
    if not adv:
        raise ValueError(f"Advertiser with Id={adv_id} not found.")

    client_id = (adv.get("BullhornClientID") or "").strip()
    client_secret = (adv.get("BullhornClientSecret") or "").strip()
    username = (adv.get("BullhornAPIUsername") or "").strip()
    password = (adv.get("BullhornAPIPassword") or "").strip()

    if not client_id or not client_secret:
        raise ValueError("BullhornClientID and BullhornClientSecret are required.")
    if not username or not password:
        raise ValueError("BullhornAPIUsername and BullhornAPIPassword are required.")

    # Step 1: Auth code
    auth_code = _bh_get_auth_code(client_id, username, password)

    # Step 2: Access token
    try:
        access_token = _bh_get_access_token(auth_code, client_id, client_secret)
    except Exception:
        time.sleep(10)
        auth_code = _bh_get_auth_code(client_id, username, password)
        time.sleep(1)
        access_token = _bh_get_access_token(auth_code, client_id, client_secret)

    # Step 3: REST token
    try:
        rest_result = _bh_get_rest_token(access_token)
    except Exception:
        time.sleep(10)
        auth_code = _bh_get_auth_code(client_id, username, password)
        time.sleep(1)
        access_token = _bh_get_access_token(auth_code, client_id, client_secret)
        time.sleep(1)
        rest_result = _bh_get_rest_token(access_token)

    session_token = rest_result["BhRestToken"]
    rest_url = rest_result["restUrl"]
    swimlane, corp_token = _parse_rest_url(rest_url)

    # Save to PostgreSQL
    update_pg_advertiser_tokens(adv_id, session_token, rest_url, swimlane, corp_token)

    return {
        "BullhornSessionToken": session_token,
        "BullhornRestURL": rest_url,
        "BullhornSwimlane": swimlane,
        "BullhornCorpToken": corp_token,
    }


# --------------------------------------------------------------------------- #
#  Bullhorn API + duplicate-detection logic
# --------------------------------------------------------------------------- #
def _normalize_base_url(rest_url: str) -> str:
    rest_url = rest_url.strip()
    if not rest_url:
        raise ValueError("REST base URL is required.")
    if not rest_url.startswith(("http://", "https://")):
        rest_url = "https://" + rest_url
    if not rest_url.endswith("/"):
        rest_url += "/"
    return rest_url


def fetch_candidates(rest_url: str, token: str, date_str: str, count: int):
    """Fetch every candidate added on `date_str`, following pagination."""
    day = datetime.strptime(date_str, "%Y-%m-%d")
    start_range = day.strftime("%Y%m%d") + "000000"
    end_range = day.strftime("%Y%m%d") + "235959"
    query = f"dateAdded:[{start_range} TO {end_range}]"

    base = _normalize_base_url(rest_url) + "search/Candidate"
    candidates, start, total, pages = [], 0, None, 0

    while True:
        params = {
            "query": query,
            "start": start,
            "count": count,
            "fields": FIELDS,
            "sort": "id",
            "BhRestToken": token.strip(),
        }
        url = base + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        pages += 1
        data = payload.get("data", []) or []
        total = payload.get("total", 0)
        candidates.extend(data)

        start += count
        # Stop when we've collected everything or a page came back empty.
        if not data or start >= total or pages > 1000:
            break

    return candidates, total, pages


def _display_name(c: dict) -> str:
    name = (c.get("name") or "").strip()
    if name:
        return name
    parts = [(c.get("firstName") or "").strip(), (c.get("lastName") or "").strip()]
    return " ".join(p for p in parts if p).strip()


def _name_key(c: dict) -> str:
    return " ".join(_display_name(c).lower().split())


def group_duplicates(candidates: list, match_on: str):
    """
    Cluster duplicates with union-find.
    match_on: 'email' | 'name' | 'either'
    Returns a list of groups (each a list of candidates), size >= 2.
    """
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for c in candidates:
        find(c["id"])

    email_seen, name_seen = {}, {}
    for c in candidates:
        cid = c["id"]
        email = (c.get("email") or "").strip().lower()
        nkey = _name_key(c)

        if match_on in ("email", "either") and email:
            if email in email_seen:
                union(cid, email_seen[email])
            else:
                email_seen[email] = cid

        if match_on in ("name", "either") and nkey:
            if nkey in name_seen:
                union(cid, name_seen[nkey])
            else:
                name_seen[nkey] = cid

    clusters = defaultdict(list)
    for c in candidates:
        clusters[find(c["id"])].append(c)

    groups = [g for g in clusters.values() if len(g) > 1]
    # Sort largest groups first, then by first email/name for stability.
    groups.sort(key=lambda g: (-len(g), (g[0].get("email") or ""), _name_key(g[0])))
    return groups


def run_report(params: dict) -> dict:
    rest_url = params.get("restUrl", "")
    token = params.get("token", "")
    date_str = params.get("date", "")
    match_on = params.get("matchOn", "email")
    adv_id = params.get("advertiserId")
    count = int(params.get("count") or 500)
    count = max(1, min(count, 500))  # Bullhorn caps search count at 500.

    if not token.strip():
        raise ValueError("BhRestToken is required.")
    if not date_str:
        raise ValueError("Date is required.")
    if match_on not in ("email", "name", "either"):
        match_on = "email"

    candidates, total, pages = fetch_candidates(rest_url, token, date_str, count)
    groups = group_duplicates(candidates, match_on)

    # If advertiser context is available, save to PostgreSQL and verify in Shazamme
    if adv_id and groups:
        out_groups = save_duplicate_candidates(int(adv_id), groups, candidates, date_str)
    else:
        out_groups = []
        for g in groups:
            members = [
                {
                    "id": c.get("id"),
                    "name": _display_name(c) or "(no name)",
                    "email": (c.get("email") or "").strip() or "(no email)",
                    "existsInShazamme": None,
                }
                for c in sorted(g, key=lambda c: c.get("id", 0))
            ]
            out_groups.append({"size": len(members), "members": members})

    dup_records = sum(g["size"] for g in out_groups)

    return {
        "date": date_str,
        "matchOn": match_on,
        "totalFetched": len(candidates),
        "reportedTotal": total,
        "pages": pages,
        "duplicateGroups": len(out_groups),
        "duplicateRecords": dup_records,
        "groups": out_groups,
    }


# --------------------------------------------------------------------------- #
#  HTTP server
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_ok(self, obj):
        self._send(200, json.dumps(obj), "application/json")

    def _json_err(self, msg):
        self._send(200, json.dumps({"error": str(msg)}), "application/json")

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        elif path == "/api/mssql/advertisers":
            try:
                self._json_ok(fetch_mssql_advertisers())
            except Exception as e:
                self._json_err(f"MSSQL error: {e}")
        elif path == "/api/advertisers":
            try:
                self._json_ok(list_pg_advertisers())
            except Exception as e:
                self._json_err(f"PostgreSQL error: {e}")
        elif re.match(r"^/api/advertisers/\d+$", path):
            adv_id = int(path.split("/")[-1])
            try:
                row = get_pg_advertiser(adv_id)
                if row:
                    self._json_ok(row)
                else:
                    self._json_err("Advertiser not found.")
            except Exception as e:
                self._json_err(f"Error: {e}")
        else:
            self._send(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/advertisers":
                params = self._read_body()
                adv_uuid = params.get("AdvertiserID", "")
                if not adv_uuid:
                    self._json_err("AdvertiserID is required.")
                    return
                mssql_row = fetch_mssql_advertiser_full(adv_uuid)
                if not mssql_row:
                    self._json_err("Advertiser not found in MSSQL or not a validated Bullhorn advertiser.")
                    return
                upsert_advertiser_to_pg(mssql_row)
                self._json_ok({"success": True, "Company": mssql_row.get("Company", "")})

            elif re.match(r"^/api/advertisers/\d+/refresh-token$", path):
                adv_id = int(path.split("/")[3])
                result = refresh_bullhorn_token(adv_id)
                self._json_ok({"success": True, **result})

            elif path == "/api/find":
                params = self._read_body()
                # If advertiserId is provided, load credentials from PostgreSQL
                adv_id = params.get("advertiserId")
                if adv_id:
                    adv = get_pg_advertiser(int(adv_id))
                    if not adv:
                        self._json_err("Advertiser not found.")
                        return
                    params.setdefault("restUrl", adv.get("BullhornRestURL", ""))
                    params.setdefault("token", adv.get("BullhornSessionToken", ""))
                result = run_report(params)
                self._json_ok(result)

            else:
                self._send(404, "Not found", "text/plain; charset=utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            self._json_err(f"Bullhorn returned HTTP {e.code}. Details: {detail[:500]}")
        except urllib.error.URLError as e:
            self._json_err(f"Network error: {e.reason}")
        except ValueError as e:
            self._json_err(str(e))
        except Exception as e:
            self._json_err(f"Unexpected error: {e}")

    def log_message(self, *args):
        pass  # keep the console quiet


# --------------------------------------------------------------------------- #
#  UI (served as a single static page; no data is persisted anywhere)
# --------------------------------------------------------------------------- #
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shazamme Data Quality Tool</title>
<style>
  :root{
    --ink:#10151c; --panel:#ffffff; --bg:#eef1f4; --line:#d9e0e7;
    --muted:#5d6b7a; --accent:#0f766e; --accent-soft:#d7efeb;
    --warn:#b45309; --danger:#b42318; --chip:#eef2f6;
    --mono:"SFMono-Regular",ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace;
    --sans:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    --radius:12px;
  }
  *{box-sizing:border-box}
  body{
    margin:0; background:
      radial-gradient(1200px 500px at 80% -10%, #e7f4f1 0%, transparent 60%),
      var(--bg);
    color:var(--ink); font-family:var(--sans); line-height:1.5;
    -webkit-font-smoothing:antialiased;
  }
  .wrap{max-width:1200px; margin:0 auto; padding:24px 20px 80px}
  header{margin-bottom:20px}
  .eyebrow{
    font-family:var(--mono); font-size:11px; letter-spacing:.18em;
    text-transform:uppercase; color:var(--accent); margin:0 0 6px
  }
  h1{font-size:24px; margin:0 0 4px; letter-spacing:-.01em}
  .sub{color:var(--muted); margin:0; max-width:70ch; font-size:13px}

  .card{
    background:var(--panel); border:1px solid var(--line);
    border-radius:var(--radius); padding:18px;
    box-shadow:0 1px 2px rgba(16,21,28,.04);
  }
  label{display:block; font-size:12px; font-weight:600; margin:0 0 6px; color:#33414f}
  .hint{font-weight:400; color:var(--muted)}
  input,select{
    width:100%; padding:9px 11px; font-size:13px; font-family:var(--sans);
    border:1px solid var(--line); border-radius:8px; background:#fbfcfd; color:var(--ink);
    transition:border-color .12s, box-shadow .12s;
  }
  input.mono{font-family:var(--mono); font-size:12px}
  input:focus,select:focus{
    outline:none; border-color:var(--accent);
    box-shadow:0 0 0 3px var(--accent-soft);
  }

  /* ---------- Top panel ---------- */
  .top-panel{
    display:flex; align-items:flex-end; gap:12px; margin-bottom:16px;
  }
  .top-panel .field{flex:1}
  .top-panel button{
    background:var(--accent); color:#fff; border:0; border-radius:8px;
    padding:9px 18px; font-size:13px; font-weight:600; cursor:pointer;
    font-family:var(--sans); white-space:nowrap;
  }
  .top-panel button:hover{background:#0b5f58}
  .top-panel button:disabled{opacity:.55;cursor:progress}
  #top-status{margin-bottom:12px}

  /* ---------- Main layout ---------- */
  .main-layout{display:grid; grid-template-columns:280px 1fr; gap:16px; align-items:start}
  @media(max-width:768px){.main-layout{grid-template-columns:1fr}}

  /* ---------- Left panel ---------- */
  .left-panel{max-height:calc(100vh - 200px); overflow-y:auto}
  .left-panel h3{font-size:13px; margin:0 0 10px; color:var(--muted); text-transform:uppercase; letter-spacing:.08em}
  .adv-list{list-style:none; margin:0; padding:0}
  .adv-list li{
    padding:10px 12px; cursor:pointer; border-radius:8px;
    font-size:13px; margin-bottom:4px; border:1px solid transparent;
    transition:background .1s, border-color .1s;
  }
  .adv-list li:hover{background:var(--accent-soft)}
  .adv-list li.active{background:var(--accent-soft); border-color:var(--accent); font-weight:600}
  .adv-list .empty-msg{color:var(--muted); font-size:12px; font-style:italic; padding:12px}

  /* ---------- Right panel ---------- */
  .right-panel .placeholder{
    text-align:center; padding:60px 20px; color:var(--muted); font-size:14px;
  }

  /* Detail grid */
  .detail-grid{display:grid; grid-template-columns:1fr 1fr; gap:10px 16px; margin-bottom:16px}
  .detail-grid .field-label{font-size:10.5px; text-transform:uppercase; letter-spacing:.07em; color:var(--muted); margin-bottom:2px}
  .detail-grid .field-value{font-family:var(--mono); font-size:12px; word-break:break-all; color:var(--ink);
    background:#f8f9fb; padding:6px 8px; border-radius:6px; border:1px solid var(--line); min-height:30px}
  .detail-grid .full{grid-column:1/-1}

  /* Buttons row */
  .btn-row{display:flex; gap:10px; margin-bottom:16px; flex-wrap:wrap}
  button.action{
    border:1px solid var(--line); border-radius:8px; padding:8px 16px;
    font-size:12px; font-weight:600; cursor:pointer; font-family:var(--sans);
    background:#fff; color:#33414f; transition:border-color .12s, color .12s;
  }
  button.action:hover{border-color:var(--accent); color:var(--accent)}
  button.action:disabled{opacity:.55;cursor:progress}
  button.action.primary{background:var(--accent); color:#fff; border-color:var(--accent)}
  button.action.primary:hover{background:#0b5f58}

  /* Divider */
  .divider{border:0; border-top:1px solid var(--line); margin:16px 0}

  /* ---------- Duplicate finder form ---------- */
  .find-form{display:grid; grid-template-columns:1fr 1fr 1fr auto; gap:10px; align-items:flex-end}
  @media(max-width:640px){.find-form{grid-template-columns:1fr 1fr}}

  .msg{border-radius:8px; padding:10px 12px; font-size:13px; border:1px solid transparent; margin-top:12px}
  .msg.err{background:#fef2f1; border-color:#f4c7c1; color:var(--danger)}
  .msg.ok{background:#f0faf8; border-color:#cfe6e2; color:var(--accent)}
  .msg.busy{background:#f2f7f6; border-color:#cfe6e2; color:#0b5f58; font-family:var(--mono)}

  /* ---------- Results ---------- */
  .summary{display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin:16px 0 6px}
  @media(max-width:640px){.summary{grid-template-columns:1fr 1fr}}
  .stat{background:var(--panel); border:1px solid var(--line); border-radius:9px; padding:12px}
  .stat .n{font-size:22px; font-weight:700; font-family:var(--mono); letter-spacing:-.02em}
  .stat .l{font-size:10px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin-top:2px}
  .stat.flag .n{color:var(--danger)}
  .stat.clean .n{color:var(--accent)}

  .toolbar{display:flex; justify-content:space-between; align-items:center; margin:20px 0 8px}
  .toolbar h2{font-size:14px; margin:0}
  button.csv{
    background:#fff; border:1px solid var(--line); border-radius:7px; padding:6px 10px;
    font-size:11px; font-weight:600; cursor:pointer; color:#33414f; font-family:var(--sans);
  }
  button.csv:hover{border-color:var(--accent); color:var(--accent)}

  .cluster{
    border:1px solid var(--line); border-left:3px solid var(--danger);
    border-radius:9px; background:var(--panel); margin-bottom:10px; overflow:hidden;
  }
  .cluster-head{
    display:flex; justify-content:space-between; align-items:center;
    padding:8px 12px; background:#fbfcfd; border-bottom:1px solid var(--line);
  }
  .cluster-head .key{font-size:12px; font-weight:600}
  .cluster-head .badge{
    font-family:var(--mono); font-size:10px; color:var(--danger);
    background:#fdece9; border:1px solid #f4c7c1; padding:2px 7px; border-radius:20px;
  }
  table{width:100%; border-collapse:collapse; font-size:12px}
  th{text-align:left; font-size:10px; text-transform:uppercase; letter-spacing:.07em;
    color:var(--muted); padding:7px 12px; border-bottom:1px solid var(--line); font-weight:600}
  td{padding:8px 12px; border-bottom:1px solid #eef2f5; vertical-align:top}
  tr:last-child td{border-bottom:0}
  td.id{font-family:var(--mono); color:var(--accent)}
  td.email{font-family:var(--mono); font-size:11px; color:#33414f}
  .empty{color:var(--muted); font-style:italic}
  .tag{display:inline-block; font-size:10px; font-weight:600; padding:2px 8px; border-radius:12px; font-family:var(--mono)}
  .tag.yes{background:#e6f7ed; color:#15803d; border:1px solid #bbf7d0}
  .tag.no{background:#fef2f1; color:var(--danger); border:1px solid #f4c7c1}
  .tag.unknown{background:var(--chip); color:var(--muted); border:1px solid var(--line)}
  .ok-empty{
    text-align:center; padding:28px; color:var(--muted);
    border:1px dashed var(--line); border-radius:9px; background:var(--panel);
  }
  .ok-empty .big{font-size:14px; color:var(--accent); font-weight:600; margin-bottom:4px}

  footer{margin-top:24px; font-size:11px; color:var(--muted)}
  footer code{font-family:var(--mono); background:var(--chip); padding:1px 4px; border-radius:4px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <p class="eyebrow">Shazamme / Data Quality</p>
    <h1>Candidate Duplicate Finder</h1>
    <p class="sub">Add advertisers from Shazamme, refresh Bullhorn tokens, and find duplicate candidates by email or name.</p>
  </header>

  <!-- TOP PANEL: Add Advertiser -->
  <div class="card top-panel">
    <div class="field">
      <label for="mssql-dropdown">Add Advertiser from Shazamme</label>
      <select id="mssql-dropdown"><option value="">Loading advertisers...</option></select>
    </div>
    <button id="add-btn" disabled>Add</button>
  </div>
  <div id="top-status"></div>

  <!-- MAIN LAYOUT -->
  <div class="main-layout">
    <!-- LEFT PANEL: Advertiser list -->
    <div class="card left-panel">
      <h3>Advertisers</h3>
      <ul class="adv-list" id="adv-list">
        <li class="empty-msg">No advertisers added yet.</li>
      </ul>
    </div>

    <!-- RIGHT PANEL -->
    <div class="card right-panel" id="right-panel">
      <div class="placeholder">Select an advertiser from the list to view details.</div>
    </div>
  </div>

  <footer>
    Timezone note: the <code>dateAdded</code> day window resolves against Bullhorn's configured timezone.
  </footer>
</div>

<script>
(function(){
  var $ = function(id){ return document.getElementById(id); };
  var selectedAdvId = null;

  function esc(s){
    return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  }
  function todayISO(){
    var d=new Date();
    return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");
  }

  // ---- Load MSSQL dropdown ----
  function loadMssqlDropdown(){
    fetch("/api/mssql/advertisers").then(function(r){return r.json()}).then(function(data){
      if(data.error){$("mssql-dropdown").innerHTML='<option value="">'+esc(data.error)+'</option>';return;}
      var opts='<option value="">-- Select advertiser ('+data.length+') --</option>';
      data.forEach(function(a){
        opts+='<option value="'+esc(a.AdvertiserID)+'">'+esc(a.Company)+'</option>';
      });
      $("mssql-dropdown").innerHTML=opts;
      $("add-btn").disabled=false;
    }).catch(function(e){
      $("mssql-dropdown").innerHTML='<option value="">Failed to load</option>';
    });
  }

  // ---- Add advertiser ----
  $("add-btn").addEventListener("click",function(){
    var uuid=$("mssql-dropdown").value;
    if(!uuid){$("top-status").innerHTML='<div class="msg err">Select an advertiser first.</div>';return;}
    $("add-btn").disabled=true;
    $("top-status").innerHTML='<div class="msg busy">Adding advertiser...</div>';
    fetch("/api/advertisers",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({AdvertiserID:uuid})
    }).then(function(r){return r.json()}).then(function(data){
      if(data.error){$("top-status").innerHTML='<div class="msg err">'+esc(data.error)+'</div>';return;}
      $("top-status").innerHTML='<div class="msg ok">Added: '+esc(data.Company)+'</div>';
      loadAdvList();
    }).catch(function(e){
      $("top-status").innerHTML='<div class="msg err">'+esc(e.message)+'</div>';
    }).finally(function(){$("add-btn").disabled=false;});
  });

  // ---- Load advertiser list (left panel) ----
  function loadAdvList(){
    fetch("/api/advertisers").then(function(r){return r.json()}).then(function(data){
      if(data.error||!data.length){
        $("adv-list").innerHTML='<li class="empty-msg">No advertisers added yet.</li>';
        return;
      }
      var html="";
      data.forEach(function(a){
        var cls=a.Id===selectedAdvId?" active":"";
        html+='<li class="adv-item'+cls+'" data-id="'+a.Id+'">'+esc(a.Company||"(no name)")
          +'<br><span style="font-size:10px;color:var(--muted)">'+esc(a.BullhornSwimlane||"")+'</span></li>';
      });
      $("adv-list").innerHTML=html;
      // attach click handlers
      document.querySelectorAll(".adv-item").forEach(function(li){
        li.addEventListener("click",function(){
          selectedAdvId=parseInt(this.getAttribute("data-id"));
          document.querySelectorAll(".adv-item").forEach(function(el){el.classList.remove("active")});
          this.classList.add("active");
          loadAdvDetail(selectedAdvId);
        });
      });
    });
  }

  // ---- Load advertiser detail (right panel) ----
  function loadAdvDetail(id){
    fetch("/api/advertisers/"+id).then(function(r){return r.json()}).then(function(a){
      if(a.error){$("right-panel").innerHTML='<div class="msg err">'+esc(a.error)+'</div>';return;}
      var html='<h3 style="margin:0 0 14px;font-size:16px">'+esc(a.Company||"(no name)")+'</h3>';

      // Detail grid
      html+='<div class="detail-grid">';
      html+=field("AdvertiserID",a.AdvertiserID,"full");
      html+=field("Company",a.Company);
      html+=field("Swimlane",a.BullhornSwimlane);
      html+=field("Client ID",a.BullhornClientID,"full");
      html+=field("Client Secret",a.BullhornClientSecret,"full");
      html+=field("API Username",a.BullhornAPIUsername);
      html+=field("API Password",a.BullhornAPIPassword);
      html+=field("Session Token",a.BullhornSessionToken,"full");
      html+=field("Corp Token",a.BullhornCorpToken);
      html+=field("REST URL",a.BullhornRestURL,"full");
      html+='</div>';

      // Buttons
      html+='<div class="btn-row">';
      html+='<button class="action" id="refresh-btn">Refresh Token</button>';
      html+='</div>';
      html+='<div id="refresh-status"></div>';

      html+='<hr class="divider">';

      // Duplicate finder form
      html+='<h3 style="margin:0 0 10px;font-size:14px">Find Duplicates</h3>';
      html+='<div class="find-form" id="find-form">';
      html+='<div><label for="f-date">Date added</label><input id="f-date" type="date" value="'+todayISO()+'"></div>';
      html+='<div><label for="f-match">Match on</label><select id="f-match">'
        +'<option value="email" selected>Email</option><option value="name">Full name</option>'
        +'<option value="either">Email or name</option></select></div>';
      html+='<div><label for="f-count">Page size</label><input id="f-count" type="number" min="1" max="500" value="500"></div>';
      html+='<div><button class="action primary" id="find-btn">Find Duplicates</button></div>';
      html+='</div>';
      html+='<div id="find-status"></div>';
      html+='<div id="results"></div>';

      $("right-panel").innerHTML=html;

      // Refresh token handler
      $("refresh-btn").addEventListener("click",function(){
        doRefreshToken(id);
      });

      // Find duplicates handler
      $("find-btn").addEventListener("click",function(){
        doFindDuplicates(id);
      });
    });
  }

  function field(label,value,cls){
    var c=cls?" "+cls:"";
    return '<div class="'+c.trim()+'"><div class="field-label">'+esc(label)+'</div>'
      +'<div class="field-value">'+esc(value||"—")+'</div></div>';
  }

  // ---- Refresh token ----
  function doRefreshToken(advId){
    $("refresh-btn").disabled=true;
    $("refresh-status").innerHTML='<div class="msg busy">Refreshing Bullhorn token (this may take a few seconds)...</div>';
    fetch("/api/advertisers/"+advId+"/refresh-token",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"})
    .then(function(r){return r.json()})
    .then(function(data){
      if(data.error){
        $("refresh-status").innerHTML='<div class="msg err">'+esc(data.error)+'</div>';
        return;
      }
      $("refresh-status").innerHTML='<div class="msg ok">Token refreshed. Session: '+esc(data.BullhornSessionToken).substring(0,30)+'... Swimlane: '+esc(data.BullhornSwimlane)+'</div>';
      // Reload detail to show updated tokens
      loadAdvDetail(advId);
    })
    .catch(function(e){
      $("refresh-status").innerHTML='<div class="msg err">'+esc(e.message)+'</div>';
    })
    .finally(function(){
      var b=$("refresh-btn"); if(b) b.disabled=false;
    });
  }

  // ---- Find duplicates ----
  function doFindDuplicates(advId){
    var btn=$("find-btn");
    btn.disabled=true;
    $("find-status").innerHTML='<div class="msg busy">Refreshing token before search...</div>';
    $("results").innerHTML="";

    // Step 1: auto-refresh token
    fetch("/api/advertisers/"+advId+"/refresh-token",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"})
    .then(function(r){return r.json()})
    .then(function(data){
      if(data.error){
        $("find-status").innerHTML='<div class="msg err">Token refresh failed: '+esc(data.error)+'</div>';
        btn.disabled=false;
        return;
      }
      $("find-status").innerHTML='<div class="msg busy">Token refreshed. Fetching candidates and paginating...</div>';

      // Step 2: find duplicates
      var body={
        advertiserId:advId,
        date:$("f-date").value,
        matchOn:$("f-match").value,
        count:$("f-count").value
      };
      return fetch("/api/find",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})
        .then(function(r){return r.json()})
        .then(function(result){
          if(result.error){$("find-status").innerHTML='<div class="msg err">'+esc(result.error)+'</div>';return;}
          $("find-status").innerHTML="";
          renderResults(result);
        });
    })
    .catch(function(e){
      $("find-status").innerHTML='<div class="msg err">'+esc(e.message)+'</div>';
    })
    .finally(function(){btn.disabled=false;});
  }

  // ---- Render duplicate results ----
  function renderResults(data){
    var r=$("results");
    var clean=data.duplicateGroups===0;
    var html="";

    html+='<div class="summary">';
    html+='<div class="stat"><div class="n">'+data.totalFetched+'</div><div class="l">Fetched</div></div>';
    html+='<div class="stat"><div class="n">'+data.pages+'</div><div class="l">API pages</div></div>';
    html+='<div class="stat '+(clean?"clean":"flag")+'"><div class="n">'+data.duplicateGroups+'</div><div class="l">Dup sets</div></div>';
    html+='<div class="stat '+(clean?"clean":"flag")+'"><div class="n">'+data.duplicateRecords+'</div><div class="l">Dup records</div></div>';
    html+='</div>';

    var modeLabel={email:"email",name:"full name",either:"email or name"}[data.matchOn]||data.matchOn;

    if(clean){
      html+='<div class="ok-empty"><div class="big">No duplicates found</div>'
        +'No candidates added on '+esc(data.date)+' share a matching '+esc(modeLabel)+'.</div>';
      r.innerHTML=html; return;
    }

    html+='<div class="toolbar"><h2>'+data.duplicateGroups+' duplicate set'
      +(data.duplicateGroups===1?"":"s")+' &middot; matched on '+esc(modeLabel)
      +'</h2><button class="csv" id="csv-btn">Export CSV</button></div>';

    data.groups.forEach(function(g){
      var first=g.members[0];
      var keyText=data.matchOn==="name"
        ?(first.name||"(no name)")
        :(first.email!=="(no email)"?first.email:(first.name||"(no name)"));
      html+='<div class="cluster"><div class="cluster-head"><span class="key">'+esc(keyText)
        +'</span><span class="badge">'+g.size+' matches</span></div>';
      html+='<table><thead><tr><th>Name</th><th>Email</th><th>Bullhorn ID</th><th>In Shazamme</th></tr></thead><tbody>';
      g.members.forEach(function(m){
        var nm=m.name==="(no name)"?'<span class="empty">(no name)</span>':esc(m.name);
        var em=m.email==="(no email)"?'<span class="empty">(no email)</span>':esc(m.email);
        var shaz=m.existsInShazamme===true?'<span class="tag yes">Yes</span>'
          :m.existsInShazamme===false?'<span class="tag no">No (false alarm)</span>'
          :'<span class="tag unknown">N/A</span>';
        html+='<tr><td>'+nm+'</td><td class="email">'+em+'</td><td class="id">'+esc(m.id)+'</td><td>'+shaz+'</td></tr>';
      });
      html+='</tbody></table></div>';
    });

    r.innerHTML=html;

    var csvBtn=$("csv-btn");
    if(csvBtn){csvBtn.addEventListener("click",function(){
      var rows=[["duplicate_set","candidate_name","email","bullhorn_id","exists_in_shazamme"]];
      data.groups.forEach(function(g,i){
        g.members.forEach(function(m){
          var shaz=m.existsInShazamme===true?"Yes":m.existsInShazamme===false?"No":"N/A";
          rows.push(["set_"+(i+1),m.name==="(no name)"?"":m.name,m.email==="(no email)"?"":m.email,m.id,shaz]);
        });
      });
      var csv=rows.map(function(row){
        return row.map(function(cell){
          var s=String(cell==null?"":cell);
          return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;
        }).join(",");
      }).join("\n");
      var blob=new Blob([csv],{type:"text/csv"});
      var a=document.createElement("a");
      a.href=URL.createObjectURL(blob);
      a.download="bullhorn_duplicates_"+data.date+".csv";
      a.click();
      URL.revokeObjectURL(a.href);
    });}
  }

  // ---- Init ----
  loadMssqlDropdown();
  loadAdvList();
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
def main():
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"Invalid port '{sys.argv[1]}', using {DEFAULT_PORT}.")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("Bullhorn Candidate Duplicate Finder")
    print(f"  -> open http://localhost:{port} in your browser")
    print("  -> press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
