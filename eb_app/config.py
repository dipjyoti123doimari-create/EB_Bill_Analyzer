"""
config.py
=========
Paths and constants for RDC Electricity Intelligence.

Nothing secret lives here. Oracle credentials are read at run time from the
existing RDC-OPS settings (see ops_settings.py) or from EB_ORACLE_* environment
variables, and are never written to source, logs or the browser.
"""

import os
from pathlib import Path

APP_NAME = "RDC Electricity Intelligence"
BASE_DIR = Path(__file__).resolve().parent.parent

# Our own application database and original-document store.
DATA_DIR = Path(os.environ.get("EB_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "eb.db"
STORAGE_DIR = Path(os.environ.get("EB_STORAGE_DIR", BASE_DIR / "storage"))
ORIGINALS_DIR = STORAGE_DIR / "originals"

# Item code used by Oracle EBS to raise electricity POs (confirmed by the business).
DEFAULT_ELECTRICITY_ITEM_CODE = "EXP00084"

# Oracle attachment entity names that can carry a PO attachment.
ATTACHMENT_ENTITIES = ("PO_HEADERS", "PO_LINES")

# FND_DOCUMENT_DATATYPES ids, verified against the live data dictionary.
DATATYPE_FILE = 6        # file stored in FND_LOBS (downloadable)
DATATYPE_WEB_PAGE = 5    # URL, e.g. dms.rdcconcrete.com permalink (not in the database)

# Scheduler interval required by the business (Phase 4 wires this up).
SCAN_INTERVAL_HOURS = 2

# Incremental scans re-read attachments changed since (last success - overlap).
INCREMENTAL_OVERLAP_HOURS = 24

# Oracle behaviour
ORACLE_CALL_TIMEOUT_MS = 60_000
ORACLE_IN_CHUNK = 900            # Oracle allows at most 1000 expressions in an IN list
ORACLE_POOL_MAX = 3

# Largest attachment we will download (guards against a corrupt or hostile file).
MAX_ATTACHMENT_BYTES = 40 * 1024 * 1024


def locate_rdc_ops_dir() -> Path | None:
    """Find the existing RDC-OPS project (the app whose Oracle settings we reuse).

    Order: RDC_OPS_DIR environment variable, then sibling folders of this project.
    A folder counts as RDC-OPS when it has oracle_connector.py and data/app.db.
    """
    env = os.environ.get("RDC_OPS_DIR")
    candidates = [Path(env)] if env else []
    candidates += [p for p in BASE_DIR.parent.iterdir() if p.is_dir()]
    for c in candidates:
        if (c / "oracle_connector.py").is_file() and (c / "data" / "app.db").is_file():
            return c
    return None
