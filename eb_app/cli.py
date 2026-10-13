"""
Command line for Phase 1 checks:

    python -m eb_app.cli init              create/upgrade the application database
    python -m eb_app.cli ping              test the Oracle connection (read-only)
    python -m eb_app.cli scan [--mode full|incremental]
    python -m eb_app.cli download [--limit N]
    python -m eb_app.cli status            counts and the last scan runs
"""

import argparse
import json
import logging
import sys

from . import config, discovery, storage
from .oracle_source import OracleError, OracleSource


def _status() -> dict:
    with storage.session() as conn:
        one = lambda sql: conn.execute(sql).fetchone()[0]
        out = {
            "schema_version": one("PRAGMA user_version"),
            "pos_total": one("SELECT COUNT(*) FROM electricity_pos"),
            "pos_eligible": one("SELECT COUNT(*) FROM electricity_pos WHERE eligible = 1"),
            "plants": one("SELECT COUNT(DISTINCT plant_code) FROM electricity_pos WHERE eligible = 1"),
            "attachments": dict(conn.execute(
                "SELECT fetch_status, COUNT(*) FROM electricity_attachments GROUP BY fetch_status").fetchall()),
            "extractions": dict(conn.execute("SELECT bill_status, COUNT(*) FROM electricity_bill_extractions WHERE archived = 0 GROUP BY bill_status").fetchall()),
            "archived_extractions": one("SELECT COUNT(*) FROM electricity_bill_extractions WHERE archived = 1"),
            "bills": one("SELECT COUNT(*) FROM electricity_bills"),
            "open_alerts": dict(conn.execute("SELECT severity, COUNT(*) FROM electricity_alerts WHERE status = 'OPEN' GROUP BY severity").fetchall()),
            "last_runs": [dict(r) for r in conn.execute(
                "SELECT id, mode, trigger, started_at, finished_at, status, pos_inspected, new_pos, "
                "attachments_seen, new_attachments, message FROM electricity_scan_runs ORDER BY id DESC LIMIT 5")],
        }
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="eb_app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("ping")
    sc = sub.add_parser("scan")
    sc.add_argument("--mode", choices=["full", "incremental"], default="incremental")
    dl = sub.add_parser("download")
    dl.add_argument("--limit", type=int, default=10)
    sub.add_parser("status")
    pr = sub.add_parser("process", help="read downloaded attachments that have not been read by this reader version")
    pr.add_argument("--limit", type=int, default=25)
    pr.add_argument("--no-ocr", action="store_true")
    rd = sub.add_parser("read", help="read one file or attachment id and print the extraction")
    rd.add_argument("target", help="path to a PDF/image, or an attachment id")
    sub.add_parser("rebuild", help="rebuild bills, PF/demand analysis and alerts from the stored extractions (no re-reading)")
    cy = sub.add_parser("cycle", help="run ONE full scheduler cycle now: scan Oracle, download, read, alert")
    cy.add_argument("--mode", choices=["full", "incremental"], default=None)
    cy.add_argument("--budget-minutes", type=float, default=None)
    cy.add_argument("--no-mail", action="store_true")
    sub.add_parser("mail-test", help="show what alert e-mail would be sent (never sends)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.cmd == "init":
        print("schema version:", storage.init_db())
        return 0
    storage.init_db()
    if args.cmd == "rebuild":
        from . import bills
        with storage.session() as conn:
            print(json.dumps(bills.refresh_all(conn, evaluate_alerts=True)))
        return 0
    if args.cmd == "mail-test":
        from . import notify, settings
        from .ops_settings import load_smtp_settings
        with storage.session() as conn:
            cfg = settings.get_all(conn)
            print("enabled:", cfg["alert_email_enabled"], "| to:", cfg["alert_email_to"] or "(none)", "| smtp:", load_smtp_settings().public_dict())
            rows = conn.execute("SELECT * FROM electricity_alerts WHERE status='OPEN' ORDER BY first_seen LIMIT 3").fetchall()
            if rows:
                print(notify.build_digest(conn, rows, cfg["app_base_url"])[0])
        return 0
    if args.cmd == "read":
        from .reader.loader import load_document
        from .reader.pipeline import analyze_document
        target = args.target
        if target.isdigit():
            with storage.session() as conn:
                row = conn.execute("SELECT local_path FROM electricity_attachments WHERE attached_document_id = ?", (int(target),)).fetchone()
            if not row or not row["local_path"]:
                print("attachment not downloaded", file=sys.stderr)
                return 2
            target = str(config.STORAGE_DIR / row["local_path"])
        result = analyze_document(load_document(target))
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        return 0
    if args.cmd == "process":
        from .reader import store
        print(json.dumps(store.process_pending(limit=args.limit, run_ocr=not args.no_ocr), indent=2))
        return 0
    if args.cmd == "status":
        print(json.dumps(_status(), indent=2, default=str))
        return 0

    source = OracleSource()
    try:
        if args.cmd == "ping":
            print("settings:", source.settings.public_dict())
            print("oracle:", source.ping())
        elif args.cmd == "scan":
            print(json.dumps(discovery.run_scan(source, mode=args.mode, trigger="cli"), indent=2))
        elif args.cmd == "download":
            print(json.dumps(discovery.download_pending(source, limit=args.limit), indent=2))
        elif args.cmd == "cycle":
            from . import scheduler
            print(json.dumps(scheduler.run_cycle(source, trigger="cli", force_mode=args.mode, budget_minutes=args.budget_minutes, send_mail=not args.no_mail), indent=2, default=str))
    except OracleError as exc:
        print("ORACLE ERROR:", exc, file=sys.stderr)
        return 2
    finally:
        source.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
