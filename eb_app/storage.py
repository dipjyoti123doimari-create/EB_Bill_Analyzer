"""
storage.py
==========
Application database (SQLite, same engine family as RDC-OPS) for RDC Electricity
Intelligence. Nothing is ever written to Oracle EBS.

Phase 1 creates the tables needed by PO / attachment discovery. Later phases add
bills, extracted fields, tariff rules, alerts and audit tables via MIGRATIONS.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime

from . import config

# Ordered, append-only list. Each entry runs once; index+1 is the schema version.
MIGRATIONS = [
    # 1 — identification rules, POs, attachments, scan bookkeeping
    """
    CREATE TABLE electricity_id_rules (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_type   TEXT NOT NULL CHECK (rule_type IN
                    ('ITEM_CODE','SUPPLIER_INCLUDE','SUPPLIER_EXCLUDE','DESCRIPTION_EXCLUDE','PLANT_EXCLUDE')),
        value       TEXT NOT NULL,
        active      INTEGER NOT NULL DEFAULT 1,
        note        TEXT,
        created_at  TEXT NOT NULL,
        UNIQUE (rule_type, value)
    );

    CREATE TABLE electricity_pos (
        po_header_id      INTEGER PRIMARY KEY,
        po_number         TEXT NOT NULL,
        org_id            INTEGER,
        supplier_id       INTEGER,
        supplier_name     TEXT,
        supplier_code     TEXT,
        vendor_site_id    INTEGER,
        plant_org_id      INTEGER,
        plant_code        TEXT,
        plant_name        TEXT,
        plant_count       INTEGER,
        po_created        TEXT,
        po_updated        TEXT,
        auth_status       TEXT,
        cancel_flag       TEXT,
        closed_code       TEXT,
        po_type           TEXT,
        comments          TEXT,
        line_description  TEXT,
        billing_start     TEXT,
        billing_end       TEXT,
        consumer_hint     TEXT,
        eligible          INTEGER NOT NULL DEFAULT 1,
        exclusion_reason  TEXT,
        first_seen        TEXT NOT NULL,
        last_seen         TEXT NOT NULL
    );
    CREATE INDEX ix_el_pos_plant    ON electricity_pos (plant_code);
    CREATE INDEX ix_el_pos_created  ON electricity_pos (po_created);
    CREATE INDEX ix_el_pos_number   ON electricity_pos (po_number);

    CREATE TABLE electricity_attachments (
        attached_document_id INTEGER PRIMARY KEY,
        po_header_id         INTEGER NOT NULL REFERENCES electricity_pos (po_header_id),
        entity_name          TEXT NOT NULL,
        pk1_value            TEXT NOT NULL,
        document_id          INTEGER NOT NULL,
        seq_num              INTEGER,
        datatype_id          INTEGER,
        media_id             INTEGER,
        file_name            TEXT,
        url                  TEXT,
        doc_description      TEXT,
        content_type         TEXT,
        size_bytes           INTEGER,
        att_created          TEXT,
        att_updated          TEXT,
        fetch_status         TEXT NOT NULL DEFAULT 'PENDING'
                             CHECK (fetch_status IN ('PENDING','DOWNLOADED','WEB_LINK','FAILED','TOO_LARGE')),
        fetch_error          TEXT,
        sha256               TEXT,
        local_path           TEXT,
        downloaded_at        TEXT,
        revision             INTEGER NOT NULL DEFAULT 1,
        first_seen           TEXT NOT NULL,
        last_seen            TEXT NOT NULL
    );
    CREATE INDEX ix_el_att_po     ON electricity_attachments (po_header_id);
    CREATE INDEX ix_el_att_status ON electricity_attachments (fetch_status);
    CREATE INDEX ix_el_att_sha    ON electricity_attachments (sha256);

    CREATE TABLE electricity_scan_runs (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        mode              TEXT NOT NULL,
        trigger           TEXT NOT NULL,
        started_at        TEXT NOT NULL,
        finished_at       TEXT,
        status            TEXT NOT NULL DEFAULT 'RUNNING' CHECK (status IN ('RUNNING','SUCCESS','FAILED')),
        pos_inspected     INTEGER DEFAULT 0,
        new_pos           INTEGER DEFAULT 0,
        attachments_seen  INTEGER DEFAULT 0,
        new_attachments   INTEGER DEFAULT 0,
        changed_attachments INTEGER DEFAULT 0,
        message           TEXT
    );

    CREATE TABLE electricity_state (
        key    TEXT PRIMARY KEY,
        value  TEXT
    );
    """,
    # 2 — bill extraction results, per-field evidence, charge lines, manual corrections (Phase 2)
    """
    CREATE TABLE electricity_bill_extractions (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        attachment_id        INTEGER NOT NULL REFERENCES electricity_attachments (attached_document_id),
        sha256               TEXT NOT NULL,
        reader_version       TEXT NOT NULL,
        status               TEXT NOT NULL CHECK (status IN ('OK','FAILED')),
        bill_status          TEXT NOT NULL CHECK (bill_status IN
                             ('VERIFIED','NEEDS_REVIEW','PARTIAL','FAILED','UNSUPPORTED','NOT_A_BILL','DUPLICATE')),
        duplicate_of         INTEGER REFERENCES electricity_bill_extractions (id),
        doc_kind             TEXT,
        pages                INTEGER,
        ocr_pages            INTEGER,
        mean_ocr_conf        REAL,
        discom_code          TEXT,
        state                TEXT,
        issuer_type          TEXT,
        layout_signature     TEXT,
        billing_basis        TEXT,
        basis_confidence     REAL,
        needs_review_count   INTEGER DEFAULT 0,
        missing_json         TEXT,
        flags_json           TEXT,
        warnings_json        TEXT,
        po_check_json        TEXT,
        result_json          TEXT,
        error                TEXT,
        created_at           TEXT NOT NULL,
        UNIQUE (attachment_id, sha256, reader_version)
    );
    CREATE INDEX ix_el_ext_status ON electricity_bill_extractions (bill_status);
    CREATE INDEX ix_el_ext_att    ON electricity_bill_extractions (attachment_id);
    CREATE INDEX ix_el_ext_sha    ON electricity_bill_extractions (sha256);

    CREATE TABLE electricity_bill_fields (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        extraction_id        INTEGER NOT NULL REFERENCES electricity_bill_extractions (id) ON DELETE CASCADE,
        field_name           TEXT NOT NULL,
        status               TEXT NOT NULL CHECK (status IN
                             ('FOUND','ZERO','NOT_FOUND','NOT_APPLICABLE','UNREADABLE','CALCULATED')),
        value_text           TEXT,
        value_num            REAL,
        unit_original        TEXT,
        unit_normalized      TEXT,
        original_label       TEXT,
        page                 INTEGER,
        bbox_json            TEXT,
        method               TEXT,
        ocr_conf             REAL,
        confidence           REAL,
        verification_status  TEXT NOT NULL CHECK (verification_status IN ('AUTO','NEEDS_REVIEW','VERIFIED','CORRECTED')),
        evidence             TEXT,
        alternatives_json    TEXT,
        extracted_value_text TEXT,
        corrected_by         TEXT,
        corrected_at         TEXT,
        UNIQUE (extraction_id, field_name)
    );
    CREATE INDEX ix_el_fld_name ON electricity_bill_fields (field_name);

    CREATE TABLE electricity_charge_lines (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        extraction_id  INTEGER NOT NULL REFERENCES electricity_bill_extractions (id) ON DELETE CASCADE,
        label          TEXT,
        category       TEXT,
        amount         REAL,
        numbers_json   TEXT,
        page           INTEGER,
        bbox_json      TEXT,
        conf           REAL
    );
    CREATE INDEX ix_el_chg_ext ON electricity_charge_lines (extraction_id);

    CREATE TABLE electricity_manual_corrections (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        extraction_id     INTEGER NOT NULL REFERENCES electricity_bill_extractions (id),
        field_name        TEXT NOT NULL,
        original_value    TEXT,
        corrected_value   TEXT,
        original_unit     TEXT,
        corrected_unit    TEXT,
        corrected_by      TEXT NOT NULL,
        corrected_at      TEXT NOT NULL,
        reason            TEXT,
        discom_code       TEXT,
        layout_signature  TEXT,
        page              INTEGER,
        bbox_json         TEXT,
        context_json      TEXT,
        regression_status TEXT NOT NULL DEFAULT 'PENDING' CHECK (regression_status IN ('PENDING','APPROVED','REJECTED'))
    );
    CREATE INDEX ix_el_cor_ext ON electricity_manual_corrections (extraction_id);
    """,
    # 3 — manual uploads, canonical bills, analysis, tariff rules, alerts, jobs, audit (Phases 3-5).
    # The extraction table is rebuilt so that an extraction can belong to a manual upload instead of an Oracle attachment
    # (this migration runs with foreign keys off, see NO_FK_MIGRATIONS).
    """
    CREATE TABLE electricity_uploads (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        plant_code           TEXT,
        plant_name           TEXT,
        consumer_number      TEXT,
        discom_code          TEXT,
        po_number            TEXT,
        billing_month        TEXT,
        original_name        TEXT,
        sha256               TEXT NOT NULL,
        local_path           TEXT NOT NULL,
        size_bytes           INTEGER,
        content_type         TEXT,
        uploaded_by          TEXT NOT NULL,
        uploaded_at          TEXT NOT NULL,
        status               TEXT NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED','PROCESSING','PROCESSED','FAILED','REJECTED')),
        error                TEXT,
        extraction_id        INTEGER,
        linked_attachment_id INTEGER,
        link_note            TEXT
    );
    CREATE INDEX ix_el_upl_sha ON electricity_uploads (sha256);

    CREATE TABLE electricity_bill_extractions_new (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        attachment_id        INTEGER REFERENCES electricity_attachments (attached_document_id),
        sha256               TEXT NOT NULL,
        reader_version       TEXT NOT NULL,
        status               TEXT NOT NULL CHECK (status IN ('OK','FAILED')),
        bill_status          TEXT NOT NULL CHECK (bill_status IN
                             ('VERIFIED','NEEDS_REVIEW','PARTIAL','FAILED','UNSUPPORTED','NOT_A_BILL','DUPLICATE')),
        duplicate_of         INTEGER REFERENCES electricity_bill_extractions_new (id),
        doc_kind             TEXT,
        pages                INTEGER,
        ocr_pages            INTEGER,
        mean_ocr_conf        REAL,
        discom_code          TEXT,
        state                TEXT,
        issuer_type          TEXT,
        layout_signature     TEXT,
        billing_basis        TEXT,
        basis_confidence     REAL,
        needs_review_count   INTEGER DEFAULT 0,
        missing_json         TEXT,
        flags_json           TEXT,
        warnings_json        TEXT,
        po_check_json        TEXT,
        result_json          TEXT,
        error                TEXT,
        created_at           TEXT NOT NULL,
        upload_id            INTEGER REFERENCES electricity_uploads (id),
        archived             INTEGER NOT NULL DEFAULT 0,
        resolved_by          TEXT,
        resolved_at          TEXT,
        resolution_note      TEXT,
        CHECK (attachment_id IS NOT NULL OR upload_id IS NOT NULL),
        UNIQUE (attachment_id, sha256, reader_version),
        UNIQUE (upload_id, sha256, reader_version)
    );
    INSERT INTO electricity_bill_extractions_new
        (id, attachment_id, sha256, reader_version, status, bill_status, duplicate_of, doc_kind, pages, ocr_pages, mean_ocr_conf,
         discom_code, state, issuer_type, layout_signature, billing_basis, basis_confidence, needs_review_count, missing_json,
         flags_json, warnings_json, po_check_json, result_json, error, created_at)
    SELECT id, attachment_id, sha256, reader_version, status, bill_status, duplicate_of, doc_kind, pages, ocr_pages, mean_ocr_conf,
         discom_code, state, issuer_type, layout_signature, billing_basis, basis_confidence, needs_review_count, missing_json,
         flags_json, warnings_json, po_check_json, result_json, error, created_at
    FROM electricity_bill_extractions;
    DROP TABLE electricity_bill_extractions;
    ALTER TABLE electricity_bill_extractions_new RENAME TO electricity_bill_extractions;
    CREATE INDEX ix_el_ext_status ON electricity_bill_extractions (bill_status);
    CREATE INDEX ix_el_ext_att    ON electricity_bill_extractions (attachment_id);
    CREATE INDEX ix_el_ext_upl    ON electricity_bill_extractions (upload_id);
    CREATE INDEX ix_el_ext_sha    ON electricity_bill_extractions (sha256);

    ALTER TABLE electricity_attachments ADD COLUMN retry_count   INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE electricity_attachments ADD COLUMN next_retry_at TEXT;
    ALTER TABLE electricity_scan_runs   ADD COLUMN downloaded     INTEGER DEFAULT 0;
    ALTER TABLE electricity_scan_runs   ADD COLUMN download_failed INTEGER DEFAULT 0;
    ALTER TABLE electricity_scan_runs   ADD COLUMN processed      INTEGER DEFAULT 0;
    ALTER TABLE electricity_scan_runs   ADD COLUMN process_failed INTEGER DEFAULT 0;

    CREATE TABLE electricity_plants (
        plant_code  TEXT PRIMARY KEY,
        plant_name  TEXT,
        state       TEXT,
        org_id      INTEGER,
        first_seen  TEXT NOT NULL,
        last_seen   TEXT NOT NULL
    );

    CREATE TABLE electricity_consumer_accounts (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        plant_code          TEXT NOT NULL,
        consumer_key        TEXT NOT NULL,
        consumer_number     TEXT NOT NULL,
        discom_code         TEXT,
        state               TEXT,
        meter_number        TEXT,
        consumer_category   TEXT,
        voltage_category    TEXT,
        schedule_enabled    INTEGER NOT NULL DEFAULT 0,
        expected_every_days INTEGER,
        grace_days          INTEGER,
        first_seen          TEXT NOT NULL,
        last_seen           TEXT NOT NULL,
        UNIQUE (plant_code, consumer_key)
    );

    CREATE TABLE electricity_bills (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        extraction_id         INTEGER NOT NULL UNIQUE REFERENCES electricity_bill_extractions (id) ON DELETE CASCADE,
        source                TEXT NOT NULL CHECK (source IN ('ORACLE_EBS','MANUAL_UPLOAD')),
        plant_code            TEXT,
        account_id            INTEGER REFERENCES electricity_consumer_accounts (id),
        po_header_id          INTEGER,
        po_number             TEXT,
        attachment_id         INTEGER,
        upload_id             INTEGER,
        bill_status           TEXT NOT NULL,
        quality               TEXT NOT NULL DEFAULT 'UNVERIFIED' CHECK (quality IN ('VERIFIED','UNVERIFIED')),
        consumer_number       TEXT,
        meter_number          TEXT,
        discom_code           TEXT,
        state                 TEXT,
        bill_number           TEXT,
        bill_date             TEXT,
        due_date              TEXT,
        period_start          TEXT,
        period_end            TEXT,
        billing_month         TEXT,
        billing_days          INTEGER,
        billing_basis         TEXT,
        kwh                   REAL,
        kvah                  REAL,
        pf_billed             REAL,
        pf_calculated         REAL,
        sanctioned_load       REAL,
        sanctioned_load_unit  TEXT,
        contract_demand       REAL,
        contract_demand_unit  TEXT,
        max_demand            REAL,
        max_demand_unit       TEXT,
        billing_demand        REAL,
        billing_demand_unit   TEXT,
        total_bill_amount     REAL,
        total_payable         REAL,
        cost_amount           REAL,
        cost_source           TEXT,
        data_flags            TEXT NOT NULL DEFAULT ',',
        energy_charges        REAL,
        fixed_charges         REAL,
        demand_charges        REAL,
        fuel_adjustment       REAL,
        electricity_duty      REAL,
        wheeling              REAL,
        taxes_surcharges      REAL,
        arrears               REAL,
        adjustments_credits   REAL,
        late_payment          REAL,
        pf_penalty            REAL,
        pf_penalty_status     TEXT,
        pf_incentive          REAL,
        pf_incentive_status   TEXT,
        excess_demand_penalty REAL,
        excess_demand_status  TEXT,
        group_key             TEXT,
        is_primary            INTEGER NOT NULL DEFAULT 1,
        revision_count        INTEGER NOT NULL DEFAULT 1,
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL
    );
    CREATE INDEX ix_el_bill_plant  ON electricity_bills (plant_code, billing_month);
    CREATE INDEX ix_el_bill_month  ON electricity_bills (billing_month);
    CREATE INDEX ix_el_bill_acct   ON electricity_bills (account_id, billing_month);
    CREATE INDEX ix_el_bill_state  ON electricity_bills (state);
    CREATE INDEX ix_el_bill_discom ON electricity_bills (discom_code);
    CREATE INDEX ix_el_bill_status ON electricity_bills (bill_status);
    CREATE INDEX ix_el_bill_group  ON electricity_bills (group_key);

    CREATE TABLE electricity_tariff_rules (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        state                    TEXT,
        discom_code              TEXT,
        consumer_category        TEXT,
        voltage_category         TEXT,
        effective_from           TEXT NOT NULL,
        effective_to             TEXT,
        billing_basis_rule       TEXT CHECK (billing_basis_rule IS NULL OR billing_basis_rule IN ('kWh','kVAh','Mixed')),
        pf_penalty_threshold     REAL,
        pf_penalty_rule_json     TEXT,
        pf_incentive_rule_json   TEXT,
        contract_demand_tolerance_pct REAL,
        excess_demand_rule_json  TEXT,
        demand_charge_rule_json  TEXT,
        tariff_rates_json        TEXT,
        source_reference         TEXT,
        approval_status          TEXT NOT NULL DEFAULT 'DRAFT' CHECK (approval_status IN ('DRAFT','APPROVED','REJECTED')),
        approved_by              TEXT,
        approved_at              TEXT,
        created_by               TEXT NOT NULL,
        created_at               TEXT NOT NULL,
        note                     TEXT,
        CHECK (state IS NOT NULL OR discom_code IS NOT NULL)
    );
    CREATE INDEX ix_el_tar_lookup ON electricity_tariff_rules (approval_status, state, discom_code, effective_from);

    CREATE TABLE electricity_pf_analysis (
        bill_id             INTEGER PRIMARY KEY REFERENCES electricity_bills (id) ON DELETE CASCADE,
        billing_basis       TEXT,
        pf_billed           REAL,
        pf_calculated       REAL,
        pf_difference       REAL,
        validation_status   TEXT NOT NULL,
        pf_used             REAL,
        pf_source           TEXT,
        performance_status  TEXT NOT NULL,
        direction           TEXT,
        actual_pf_penalty   REAL,
        actual_pf_incentive REAL,
        penalty_status      TEXT,
        expected_pf_penalty REAL,
        expected_status     TEXT,
        expected_vs_bill    REAL,
        tariff_rule_id      INTEGER,
        current_kvah        REAL,
        target_pf           REAL,
        target_kvah         REAL,
        kvah_reduction      REAL,
        marginal_rate       REAL,
        rate_source         TEXT,
        estimated_savings   REAL,
        savings_status      TEXT NOT NULL,
        notes_json          TEXT,
        computed_at         TEXT NOT NULL
    );

    CREATE TABLE electricity_demand_analysis (
        bill_id               INTEGER PRIMARY KEY REFERENCES electricity_bills (id) ON DELETE CASCADE,
        contract_demand       REAL,
        contract_demand_unit  TEXT,
        max_demand            REAL,
        max_demand_unit       TEXT,
        billing_demand        REAL,
        comparison_status     TEXT NOT NULL,
        utilization_pct       REAL,
        excess_demand         REAL,
        exceeds_contract      INTEGER,
        violation_status      TEXT NOT NULL,
        tolerance_pct         REAL,
        tariff_rule_id        INTEGER,
        actual_excess_penalty REAL,
        penalty_status        TEXT,
        expected_excess_penalty REAL,
        expected_status       TEXT,
        expected_vs_bill      REAL,
        notes_json            TEXT,
        computed_at           TEXT NOT NULL
    );

    CREATE TABLE electricity_alerts (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        alert_type       TEXT NOT NULL,
        severity         TEXT NOT NULL CHECK (severity IN ('CRITICAL','WARNING','INFO')),
        bill_id          INTEGER,
        extraction_id    INTEGER,
        account_id       INTEGER,
        plant_code       TEXT,
        dedupe_key       TEXT NOT NULL,
        title            TEXT NOT NULL,
        message          TEXT,
        required_action  TEXT,
        detail_json      TEXT,
        status           TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','ACKNOWLEDGED','RESOLVED')),
        first_seen       TEXT NOT NULL,
        last_seen        TEXT NOT NULL,
        occurrences      INTEGER NOT NULL DEFAULT 1,
        acknowledged_by  TEXT,
        acknowledged_at  TEXT,
        resolved_by      TEXT,
        resolved_at      TEXT,
        resolution_note  TEXT,
        last_emailed_at  TEXT,
        email_count      INTEGER NOT NULL DEFAULT 0,
        escalation_level INTEGER NOT NULL DEFAULT 0,
        escalated_at     TEXT
    );
    CREATE UNIQUE INDEX ux_el_alert_open ON electricity_alerts (dedupe_key) WHERE status <> 'RESOLVED';
    CREATE INDEX ix_el_alert_status ON electricity_alerts (status, severity);
    CREATE INDEX ix_el_alert_plant  ON electricity_alerts (plant_code);

    CREATE TABLE electricity_notifications (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        alert_id    INTEGER,
        kind        TEXT NOT NULL,
        recipients  TEXT,
        subject     TEXT,
        status      TEXT NOT NULL CHECK (status IN ('SENT','FAILED','SKIPPED')),
        error       TEXT,
        created_at  TEXT NOT NULL
    );

    CREATE TABLE electricity_processing_errors (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        stage        TEXT NOT NULL,
        ref_type     TEXT,
        ref_id       INTEGER,
        message      TEXT NOT NULL,
        detail       TEXT,
        created_at   TEXT NOT NULL,
        resolved_at  TEXT,
        resolved_by  TEXT
    );
    CREATE INDEX ix_el_err_ref ON electricity_processing_errors (ref_type, ref_id);

    CREATE TABLE electricity_audit_logs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT NOT NULL,
        username     TEXT,
        role         TEXT,
        action       TEXT NOT NULL,
        entity_type  TEXT,
        entity_id    TEXT,
        details_json TEXT,
        ip_address   TEXT
    );
    CREATE INDEX ix_el_aud_ts ON electricity_audit_logs (ts);

    CREATE TABLE electricity_jobs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        kind            TEXT NOT NULL CHECK (kind IN ('PROCESS_ATTACHMENT','PROCESS_UPLOAD','RETRY_EXTRACTION')),
        ref_id          INTEGER NOT NULL,
        status          TEXT NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED','RUNNING','DONE','FAILED')),
        attempts        INTEGER NOT NULL DEFAULT 0,
        max_attempts    INTEGER NOT NULL DEFAULT 3,
        next_attempt_at TEXT,
        claimed_by      TEXT,
        claimed_at      TEXT,
        finished_at     TEXT,
        error           TEXT,
        created_at      TEXT NOT NULL
    );
    CREATE INDEX ix_el_job_status ON electricity_jobs (status, next_attempt_at);
    CREATE UNIQUE INDEX ux_el_job_active ON electricity_jobs (kind, ref_id) WHERE status IN ('QUEUED','RUNNING');

    CREATE TABLE electricity_job_locks (
        name         TEXT PRIMARY KEY,
        owner        TEXT NOT NULL,
        acquired_at  TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL,
        expires_at   TEXT NOT NULL
    );
    """,
]


NO_FK_MIGRATIONS = {3}


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def connect(db_path=None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    if str(path) != ":memory:":
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def session(db_path=None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path=None) -> int:
    """Apply any pending migrations; return the current schema version."""
    conn = connect(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for n, script in enumerate(MIGRATIONS, start=1):
            if n > version:
                if n in NO_FK_MIGRATIONS:
                    conn.execute("PRAGMA foreign_keys = OFF")        # a table rebuild must not cascade-delete the child rows
                try:
                    conn.executescript("BEGIN;\n" + script + f"\nPRAGMA user_version = {n};\nCOMMIT;")
                except Exception:
                    if conn.in_transaction:
                        conn.rollback()
                    raise
                finally:
                    conn.execute("PRAGMA foreign_keys = ON")
        seed_default_rules(conn)
        conn.commit()
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def seed_default_rules(conn: sqlite3.Connection) -> None:
    """Insert the confirmed item code once; admins manage everything else."""
    conn.execute(
        "INSERT OR IGNORE INTO electricity_id_rules (rule_type, value, active, note, created_at) VALUES (?,?,?,?,?)",
        ("ITEM_CODE", config.DEFAULT_ELECTRICITY_ITEM_CODE, 1, "Item code used for electricity POs in Oracle EBS", now_iso()))


def get_state(conn, key, default=None):
    row = conn.execute("SELECT value FROM electricity_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn, key, value) -> None:
    conn.execute("INSERT INTO electricity_state (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
