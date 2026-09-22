-- =============================================================================
-- schema.sql - the coordinator's PostgreSQL schema (Cloud SQL), production DDL.
--
-- Mirrors db/dbmodel.py exactly - same tables, columns, types, defaults, enums,
-- indexes, and foreign keys. Run once against a fresh database to provision it;
-- db/dbmodel.py itself creates no tables/types, it only reads/writes them.
--
-- Every primary key is TEXT, server-generated as '<PREFIX>-' || gen_random_uuid():
--   oem_companies        OEM-<uuid>
--   whitelist_senders    WLS-<uuid>
--   campaigns            CMP-<uuid>
--   campaign_suppliers   SUP-<uuid>
--   email_conversations  CONV-<uuid>
-- =============================================================================

-- --------------------------------------------------------------------------- --
-- extensions
-- --------------------------------------------------------------------------- --
CREATE EXTENSION IF NOT EXISTS citext;   -- case-insensitive TEXT (all email columns)
CREATE EXTENSION IF NOT EXISTS pgcrypto; -- gen_random_uuid() on Postgres < 13; no-op / built in on 13+

-- --------------------------------------------------------------------------- --
-- enum types
-- --------------------------------------------------------------------------- --
DO $$ BEGIN
    CREATE TYPE campaign_supplier_status AS ENUM ('pending', 'in_progress', 'completed');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE conversation_direction AS ENUM ('INCOMING', 'OUTGOING');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- --------------------------------------------------------------------------- --
-- table 1: oem_companies - a paying customer organisation + subscription window
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS oem_companies (
    id              TEXT PRIMARY KEY DEFAULT ('OEM-' || gen_random_uuid()::text),
    company_name    TEXT NOT NULL,
    subscribed      BOOLEAN NOT NULL DEFAULT true,
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ NOT NULL,
    api_use_key     JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE oem_companies IS
    'A paying customer organisation, with a subscription window (started_at..ended_at).';

-- --------------------------------------------------------------------------- --
-- table 2: whitelist_senders - OEM staff allowed to ask the coordinator to work
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS whitelist_senders (
    id                      TEXT PRIMARY KEY DEFAULT ('WLS-' || gen_random_uuid()::text),
    oem_company_id          TEXT REFERENCES oem_companies (id) ON DELETE SET NULL,
    oem_user_name           TEXT NOT NULL,
    oem_user_email          TEXT NOT NULL UNIQUE,
    oem_user_phone_number   TEXT,
    is_active               BOOLEAN DEFAULT true,
    created_at              TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_whitelist_oem_company
    ON whitelist_senders (oem_company_id);

COMMENT ON TABLE whitelist_senders IS
    'People at an OEM company allowed to start work; every other sender is ignored '
    'unless they turn out to be a known campaign_suppliers contact.';

-- --------------------------------------------------------------------------- --
-- table 3: campaigns - one outreach request (a requirement chased across suppliers)
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS campaigns (
    id                      TEXT PRIMARY KEY DEFAULT ('CMP-' || gen_random_uuid()::text),
    oem_company_id          TEXT NOT NULL REFERENCES oem_companies (id) ON DELETE RESTRICT,
    title                   TEXT NOT NULL,
    org_email               CITEXT NOT NULL,
    summary                 JSONB NOT NULL,
    addl_email              CITEXT,
    is_active               BOOLEAN DEFAULT true,
    total_suppliers         INTEGER NOT NULL DEFAULT 0,
    started_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at                TIMESTAMPTZ,
    gcs_raw_payload_path    TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_campaigns_oem_active
    ON campaigns (oem_company_id, is_active);

COMMENT ON TABLE campaigns IS
    'One outreach request - a requirement chased across one or more suppliers.';
COMMENT ON COLUMN campaigns.summary IS
    'JSON object: {objective, instructions[], important_information[], cc_emails[], '
    'bcc_emails[], additional_information, deadline{mentioned_in_email, duration_days, '
    'explicit_date, calculated_end_date}}. Requirement-only - never a supplier''s name.';

-- --------------------------------------------------------------------------- --
-- table 4: campaign_suppliers - one row per supplier a campaign is chasing
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS campaign_suppliers (
    id                          TEXT PRIMARY KEY DEFAULT ('SUP-' || gen_random_uuid()::text),
    campaign_id                 TEXT NOT NULL REFERENCES campaigns (id) ON DELETE CASCADE,
    user_name                   TEXT,
    user_email                  CITEXT NOT NULL,
    user_phone_number           TEXT,
    company_name                TEXT,
    company_domain              TEXT,
    response_summary            JSONB,
    requirements_fulfillment    JSONB DEFAULT '{}'::jsonb,
    status                      campaign_supplier_status NOT NULL DEFAULT 'pending',
    assigned_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at                TIMESTAMPTZ,
    reminder_count               INTEGER NOT NULL DEFAULT 0,
    last_reminder_sent_at        TIMESTAMPTZ,

    -- out-of-office & alternate routing
    alternate_user_name          TEXT,
    alternate_email               CITEXT,
    alternate_phone                VARCHAR(50),
    out_of_office                  BOOLEAN NOT NULL DEFAULT false,
    is_active                      BOOLEAN NOT NULL DEFAULT true,
    ooo_till                        TIMESTAMPTZ,

    CONSTRAINT uq_campaign_user_email UNIQUE (campaign_id, user_email)
);

CREATE INDEX IF NOT EXISTS idx_supplier_campaign_active
    ON campaign_suppliers (campaign_id, is_active);
CREATE INDEX IF NOT EXISTS idx_supplier_status_ooo
    ON campaign_suppliers (status, ooo_till);
CREATE INDEX IF NOT EXISTS idx_supplier_is_active
    ON campaign_suppliers (is_active);
CREATE INDEX IF NOT EXISTS idx_supplier_ooo_till
    ON campaign_suppliers (ooo_till);

COMMENT ON TABLE campaign_suppliers IS
    'One row per supplier contact a campaign is chasing: progress (status), what '
    'they''ve provided so far (response_summary), and out-of-office/alternate routing.';
COMMENT ON COLUMN campaign_suppliers.response_summary IS
    'Cumulative JSON object of facts/data the supplier has provided so far - merges '
    'across replies, never overwritten wholesale.';
COMMENT ON COLUMN campaign_suppliers.requirements_fulfillment IS
    'JSON object: {fulfilled: bool, missing_items: [...]}, set by the '
    'supplier_reply_check agent on every reply.';

-- --------------------------------------------------------------------------- --
-- table 5: email_conversations - every email, either direction - the audit trail
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS email_conversations (
    id                      TEXT PRIMARY KEY DEFAULT ('CONV-' || gen_random_uuid()::text),
    campaign_id              TEXT REFERENCES campaigns (id) ON DELETE SET NULL,
    campaign_supplier_id      TEXT REFERENCES campaign_suppliers (id) ON DELETE SET NULL,
    message_id                 TEXT NOT NULL UNIQUE,
    thread_id                   TEXT NOT NULL,
    in_reply_to                  TEXT,
    sender_email                  CITEXT NOT NULL,
    recipients                     JSONB NOT NULL DEFAULT '{"to": [], "cc": [], "bcc": []}'::jsonb,
    direction                       conversation_direction NOT NULL,
    subject                          TEXT,
    body_text                         TEXT,
    attachment_urls                    JSONB DEFAULT '[]'::jsonb,
    processing_status                   VARCHAR(50) NOT NULL DEFAULT 'PROCESSED',
    created_at                            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_conversation_campaign
    ON email_conversations (campaign_id);
CREATE INDEX IF NOT EXISTS idx_conversation_supplier
    ON email_conversations (campaign_supplier_id);
CREATE INDEX IF NOT EXISTS idx_conversation_thread
    ON email_conversations (thread_id);
CREATE INDEX IF NOT EXISTS idx_conversation_sender
    ON email_conversations (sender_email);

COMMENT ON TABLE email_conversations IS
    'Every email in either direction, tied back to its campaign and supplier - '
    'the full audit trail / thread history.';
COMMENT ON COLUMN email_conversations.processing_status IS
    'e.g. PROCESSED, NO_SUBSCRIPTION, SKIPPED_<PURPOSE>, CAMPAIGN_EXTRACT_FAILED, '
    'DUPLICATE_CAMPAIGN, SUPPLIER_CHECK_FAILED, OUT_OF_OFFICE, SENT, SEND_FAILED.';
