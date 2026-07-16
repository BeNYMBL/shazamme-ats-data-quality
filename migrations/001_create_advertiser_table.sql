-- Migration: 001_create_advertiser_table
-- Date: 2026-07-16
-- Description: Create the Advertiser table with core identity and Bullhorn OAuth columns

CREATE TABLE IF NOT EXISTS "Advertiser" (
    "Id" SERIAL PRIMARY KEY,
    "AdvertiserID" UUID NOT NULL UNIQUE,
    "BullhornClientID" VARCHAR(255),
    "BullhornClientSecret" VARCHAR(255)
);
