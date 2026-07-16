-- Migration: 004_create_candidate_table
-- Date: 2026-07-17
-- Description: Create Candidate table to store duplicate candidates found via Bullhorn API,
--              with cross-reference flag for Shazamme verification

CREATE TABLE IF NOT EXISTS "Candidate" (
    "Id" SERIAL PRIMARY KEY,
    "AdvertiserId" INT NOT NULL REFERENCES "Advertiser"("Id"),
    "BullhornCandidateID" VARCHAR(50) NOT NULL,
    "CandidateName" VARCHAR(200),
    "Email" VARCHAR(250),
    "AddedDate" DATE,
    "ExistsInShazamme" BOOLEAN NOT NULL DEFAULT FALSE,
    "DuplicateSetNumber" INT,
    "CheckedOn" TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE ("AdvertiserId", "BullhornCandidateID")
);
