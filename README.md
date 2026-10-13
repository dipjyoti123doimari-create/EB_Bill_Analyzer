# RDC Electricity Intelligence (RDC-EB)

Monitors electricity bills for RDC Concrete plants: finds the bills attached to electricity POs in Oracle EBS R12 (item code **EXP00084**), reads them (any DISCOM, any layout, PDF / scan / photo),
checks power factor, contract demand and penalties, raises alerts and gives management a dashboard in the RDC-OPS look and feel.

**Status: all six phases are built and tested** (see "Verification status" for exactly what was proven and what is still pending). Full documentation: [`docs/DOCUMENTATION.md`](docs/DOCUMENTATION.md) · schema: [`docs/SCHEMA.md`](docs/SCHEMA.md) · field-level accuracy: [`docs/VALIDATION.md`](docs/VALIDATION.md).

## Run

```
python -m venv .venv ; .venv\Scripts\python -m pip install -r requirements.txt      # once (own environment; RDC-OPS is untouched)
START_EB.bat                                                                         # website + 2-hourly scanner, restarted automatically   -> http://localhost:2002
.venv\Scripts\python app.py                                                          # the same without the watchdog
.venv\Scripts\python -m eb_app.worker                                                # scanner without the website
.venv\Scripts\python -m eb_app.cli status|ping|scan|download|process|read|rebuild|cycle|mail-test
.venv\Scripts\python -m unittest discover -s tests                                   # tests
```
Log in with your RDC-OPS username and password (Super Admin = everything; HO / Finance viewer = view + export; Regional / Plant user = their plants, upload, and corrections to their own uploads).

## What it does

| Phase | Delivered |
|---|---|
| 1 Oracle discovery | Read-only PO / plant / attachment discovery with administrator-managed identification rules (now editable in the app). 4,318 POs, 116 plants, 3,516 attachments catalogued. |
| 2 Universal bill reader | Template-free reading with positions and evidence, local OCR (English + Hindi / Marathi, Kannada, Tamil, Telugu models), PF / demand / charges, billing-basis from arithmetic, corrections with audit and a regression queue. |
| 3 PF / demand / tariff engine | Billed vs calculated PF with tolerance and review; management bands; **actual** penalties kept apart from **theoretical** kVAh savings; demand utilisation with unit safety; effective-dated, approval-gated tariff-rule registry (none built in); energy-weighted company / plant / state / board analytics and trends. |
| 4 Automation | Restart-safe scheduler (every 2 h, incremental + daily full reconcile, back-off, locks, retries, backfill), persistent job queue, manual upload with validation / duplicate control / linking to Oracle, versioned history (nothing overwritten). |
| 5 Website | Dashboard (10 KPIs, charts), plant table (filters, server-side paging, sort, columns, Excel), plant page, bill viewer (original + evidence highlight + corrections), review / exception list, alerts + e-mail (dedupe, acknowledge, escalate), automation monitor, 11 Excel reports, tariff rules, settings, audit log. |
| 6 Validation | 287 automated tests (all pass) incl. failure injection, security checks and a 2,880-bill performance run; hand-verified accuracy sets for the key fields; documentation. |

## Honest accuracy statement

* The reader is **not** 100 % accurate and is never described as such. On layouts it had never seen, the first read got about **66–83 % of fields right**; after fixes the same bills score 90 %+ (they become regression data).
  Hand-verified sets: development 80/80, held-out batches 34/36 and 27/29 (after fixes), scans 39/39, **penalty amounts 31/31** (`tests/gold`, `scripts/dev/evaluate_gold.py`).
* No bill is marked **VERIFIED** until a person verifies or corrects it. The dashboard therefore says, on every page, how many of the figures come from bills nobody has checked, and it leaves a bill's
  contradictory figure (kWh/kVAh that cannot give the printed PF, a bill total 10× the payable) **out of company totals** instead of averaging a misread number into them.
* Penalties are the amounts printed on the bills. A violation, an expected penalty or a PF-penalty check exists only where an **approved tariff rule** with a source document exists — none is built in.
* Estimated savings are theoretical, computed only for kVAh-billed bills with consistent PF data, and are never added to, or described as, a penalty.

## Architecture in one paragraph

Separate app (port 2002, own `.venv`) in the RDC-OPS design system; it reuses RDC-OPS's Oracle settings, SMTP account and users **read-only** and never imports or changes RDC-OPS code. Oracle access is one guarded read-only module.
SQLite (`data/eb.db`) holds mirrors, extractions with evidence, bills, analysis, alerts, jobs and audit; originals are stored unchanged by SHA-256 under `storage/`. OCR is local — no document leaves the machine; the optional AI hook is off.

## Verification status

See section 13 of the documentation for the evidence table and section 15 for what is **pending verification** (approved tariff rules, human verification of bills, live e-mail to the wider list, an unattended 2-hour wall-clock observation, AI provider approval).
