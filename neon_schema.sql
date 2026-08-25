-- ===========================================================================
-- doc-extract -> NeonDB
--
-- Run once against your Neon database. The doc-extract MCP does NOT execute
-- this; the workflow's database node does. This file is the contract between
-- them.
--
-- Design note for the chat agent (workflow node 4):
-- Two representations are stored on purpose.
--   documents.markdown   -> what the AGENT READS. A clean rendering of the
--                           whole document. An LLM answering "what is the
--                           closing balance for One World?" does far better
--                           reading this than re-assembling rows from JSON.
--   document_lines       -> what the AGENT QUERIES. Typed columns, so
--                           "what is past due across all suppliers?" is a
--                           SUM/WHERE, not a guess.
-- Give the agent both and let it pick. Aggregations go to SQL; "what does
-- this document say?" goes to markdown.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS documents (
    id              BIGSERIAL PRIMARY KEY,
    checksum        TEXT UNIQUE NOT NULL,      -- from response.document.checksum
    file_name       TEXT,
    schema_version  TEXT NOT NULL,
    status          TEXT NOT NULL,             -- ok | needs_review | parsed_without_profile
    profile         TEXT,                      -- NULL when no profile matched
    profile_confidence NUMERIC(4,2),

    -- Typed header fields, present only when a profile matched.
    supplier_name   TEXT,
    supplier_code   TEXT,
    currency        CHAR(3),
    ageing_date     DATE,
    statement_date  DATE,
    total_balance   NUMERIC(18,3),

    -- Full-document representations, ALWAYS present.
    markdown        TEXT NOT NULL,             -- content.markdown
    plain_text      TEXT,                      -- content.text
    key_values      JSONB,                     -- content.key_values
    raw_json        JSONB NOT NULL,            -- the entire response

    validation_ok   BOOLEAN NOT NULL DEFAULT FALSE,
    page_count      INT,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Typed line items. Only populated when a profile matched.
CREATE TABLE IF NOT EXISTS document_lines (
    id               BIGSERIAL PRIMARY KEY,
    document_id      BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    line_no          INT NOT NULL,
    document_type    TEXT,
    document_no      TEXT,
    bp_reference_no  TEXT,
    posting_date     DATE,
    due_date         DATE,
    details          TEXT,
    amount           NUMERIC(18,3),
    running_balance  NUMERIC(18,3),
    UNIQUE (document_id, line_no)
);

CREATE TABLE IF NOT EXISTS document_summary (
    document_id  BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    section      TEXT NOT NULL,                -- 'buckets' | 'percentages'
    label        TEXT NOT NULL,                -- '0 - 30', 'Balance Due', ...
    value        NUMERIC(18,3),
    PRIMARY KEY (document_id, section, label)
);

-- Retrieval chunks. Only needed for long documents; a one-page statement
-- fits in a prompt whole, and feeding it whole beats retrieving pieces.
CREATE TABLE IF NOT EXISTS document_chunks (
    id           BIGSERIAL PRIMARY KEY,
    document_id  BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index  INT NOT NULL,
    heading      TEXT,
    body         TEXT NOT NULL,
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_lines_due      ON document_lines(due_date);
CREATE INDEX IF NOT EXISTS idx_lines_bpref    ON document_lines(bp_reference_no);
CREATE INDEX IF NOT EXISTS idx_docs_supplier  ON documents(supplier_code);
CREATE INDEX IF NOT EXISTS idx_docs_status    ON documents(status);

-- Full-text search over the document body, so the agent can locate a
-- document by content without embeddings.
CREATE INDEX IF NOT EXISTS idx_docs_fts
    ON documents USING GIN (to_tsvector('english', coalesce(plain_text, '')));

-- The view the chat agent should query first.
CREATE OR REPLACE VIEW v_document_lines AS
SELECT d.supplier_name, d.supplier_code, d.currency, d.ageing_date,
       d.file_name, d.status, l.*
FROM document_lines l
JOIN documents d ON d.id = l.document_id;


-- ===========================================================================
-- Node 3: insert. Idempotent on checksum -- re-running a workflow replaces
-- the document instead of double-counting the payable.
-- ===========================================================================

-- INSERT INTO documents (checksum, file_name, schema_version, status, profile,
--     profile_confidence, supplier_name, supplier_code, currency, ageing_date,
--     statement_date, total_balance, markdown, plain_text, key_values,
--     raw_json, validation_ok, page_count)
-- VALUES ($1, ...)
-- ON CONFLICT (checksum) DO UPDATE SET
--     markdown = EXCLUDED.markdown, raw_json = EXCLUDED.raw_json,
--     status   = EXCLUDED.status,   validation_ok = EXCLUDED.validation_ok,
--     ingested_at = now()
-- RETURNING id;
--
-- Then DELETE FROM document_lines WHERE document_id = $id; and re-insert.

-- JSON path -> column
--   document.checksum            -> documents.checksum
--   status                       -> documents.status
--   metadata.supplier_name       -> documents.supplier_name
--   content.markdown             -> documents.markdown        (REQUIRED)
--   content.text                 -> documents.plain_text
--   content.key_values           -> documents.key_values
--   content.chunks[]             -> document_chunks
--   line_items[]                 -> document_lines
--   summary.buckets{}            -> document_summary(section='buckets')
--   (whole response)             -> documents.raw_json


-- ===========================================================================
-- Node 4: queries the chat agent will actually run
-- ===========================================================================

-- Read one document in full (preferred for "what does this say?"):
--   SELECT markdown FROM documents WHERE supplier_name ILIKE '%one world%'
--   ORDER BY ageing_date DESC LIMIT 1;

-- Aggregate across documents (SQL, never markdown):
--   SELECT supplier_name, SUM(amount) AS past_due
--   FROM v_document_lines
--   WHERE due_date < CURRENT_DATE
--   GROUP BY supplier_name ORDER BY past_due;

-- Find a specific invoice by its reference, wrapped or not:
--   SELECT * FROM v_document_lines WHERE bp_reference_no = 'SI/08781/CN/00007';

-- Locate documents by free text:
--   SELECT id, file_name FROM documents
--   WHERE to_tsvector('english', plain_text) @@ plainto_tsquery('english', $1);

-- Anything awaiting a human:
--   SELECT id, file_name, status FROM documents WHERE NOT validation_ok;
