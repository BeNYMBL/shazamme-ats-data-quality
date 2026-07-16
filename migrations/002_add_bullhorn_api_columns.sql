-- Migration: 002_add_bullhorn_api_columns
-- Date: 2026-07-16
-- Description: Add Bullhorn API credential and connection columns to Advertiser

ALTER TABLE "Advertiser"
    ADD COLUMN IF NOT EXISTS "BullhornAPIUsername" VARCHAR(255),
    ADD COLUMN IF NOT EXISTS "BullhornAPIPassword" VARCHAR(255),
    ADD COLUMN IF NOT EXISTS "BullhornSessionToken" VARCHAR(500),
    ADD COLUMN IF NOT EXISTS "BullhornCorpToken" VARCHAR(255),
    ADD COLUMN IF NOT EXISTS "BullhornSwimlane" VARCHAR(255),
    ADD COLUMN IF NOT EXISTS "BullhornRestURL" VARCHAR(500);
