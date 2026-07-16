-- Migration: 003_add_company_column
-- Date: 2026-07-16
-- Description: Add Company column to Advertiser table (sourced from MSSQL dbo.Advertiser.Company)

ALTER TABLE "Advertiser"
    ADD COLUMN IF NOT EXISTS "Company" VARCHAR(100);
