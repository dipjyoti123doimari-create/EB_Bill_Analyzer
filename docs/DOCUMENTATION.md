# RDC Electricity Intelligence (RDC-EB) — documentation

Status of every requirement is in section 13 ("Test results") and section 14 ("Known limitations"). Anything that could not be proven here is listed
as **pending verification** in section 15 — it is not claimed as done.

1. [System architecture](#1-system-architecture)
2. [Files created / modified](#2-files-created--modified)
3. [Oracle queries used](#3-oracle-queries-used)
4. [Database schema](#4-database-schema)
5. [Extraction pipeline](#5-extraction-pipeline)
6. [PF calculation logic](#6-pf-calculation-logic)
7. [Demand penalty logic](#7-demand-penalty-logic)
8. [Scheduler configuration](#8-scheduler-configuration)
9. [Manual upload process](#9-manual-upload-process)
10. [Alert configuration](#10-alert-configuration)
11. [Application run commands](#11-application-run-commands)
12. [Deployment instructions](#12-deployment-instructions)
13. [Test results](#13-test-results)
14. [Known limitations](#14-known-limitations)
15. [Pending integrations / configuration](#15-pending-integrations--configuration)

---

## 1. System architecture

```
 Oracle EBS R12 (read-only)                                  RDC-OPS (not touched)
   PO / supplier / plant / FND attachments                     app.db: Oracle + SMTP settings, users  ── read-only ──┐
        │  SELECT only (guard in oracle_source.py)                                                                      │
        ▼                                                                                                             ▼
 ┌──────────────── RDC-EB  (separate app, own .venv, port 2002) ────────────────────────────────────────────────────────────┐
 │  scheduler.py  tick every 60 s ─► "is a scan due?" (stored in the DB) ─► run_cycle():                                    │
 │     discovery.run_scan ► download_pending (retry/back-off) ► store.process_* (reader) ► alerts ► notify (e-mail)         │
 │  jobs.py       persistent queue + worker thread: manual uploads, re-reads (OCR gate: one reading at a time)              │
 │  reader/       universal bill reader: cells with positions (native PDF text or local OCR) ► fields ► checks ► evidence   │
 │  bills.py      extraction ─► canonical bill (+ consistency flags) ─► analysis/engine.py (PF, demand, tariff rules)        │
 │  analytics.py  management analytics, filters, paging      reports.py  Excel      alerts.py / notify.py  alerts + e-mail │
 │  app.py        Flask website in the RDC-OPS look (login = RDC-OPS users, roles, CSRF, audit)                             │
 │  data/eb.db (SQLite, WAL)       storage/originals/<sha256>  (Oracle files)      storage/uploads/<sha256>  (manual)       │
 └────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Key decisions
* **Separate application, same design system.** RDC-OPS is live on :2001 without version control and keeps its connector paths relative to its own folder, so
  adding a module to it would have risked it. RDC-EB copies RDC-OPS's stylesheet / JavaScript / sidebar / topbar / KPI cards / multi-selects / theme picker, and
  reuses its *settings*: the Oracle connection (read from RDC-OPS's `app_settings`, SQLite `mode=ro`), the SMTP account, and its users and roles. No RDC-OPS module is
  imported and no RDC-OPS file is written.
* **One Oracle connector.** `eb_app/oracle_source.py` is the only module that talks to Oracle; it uses the same `oracledb` thick-mode pool approach and settings as RDC-OPS.
  Every statement passes `_assert_read_only()` (SELECT/WITH only, no `;`, no DML/DDL words) and a test fails if any other module opens a connection.
* **Everything the user sees can be traced.** Extraction → per-field evidence (page, position, label, method, confidence) → bill → analysis → dashboard/report; corrections and
  verifications are audited and the original reading is kept.
* **Never fabricate.** No tariff rule is built in; no penalty is inferred; estimates are separate figures; absent ≠ zero ≠ unreadable (see 5, 6, 7).

## 2. Files created / modified

Phases 1–2 (earlier) — `eb_app/{config,ops_settings,oracle_source,identification,discovery,storage,cli}.py`, `eb_app/reader/*` (loader, ocr, ocr_indic, layout, extractor, fields,
pipeline, charges, store, textnorm, model, ai_assist), `tests/test_phase1.py`, `tests/test_reader.py`, `tests/gold/*.json`, `scripts/dev/*`.

Phases 3–6 (this delivery)

| Area | Files |
|---|---|
| Schema | `eb_app/storage.py` (migration 3: uploads, plants, accounts, bills, tariff rules, PF / demand analysis, alerts, notifications, errors, audit, jobs, locks; extraction table rebuilt to allow manual uploads) |
| Analysis engine | `eb_app/analysis/{engine,tariff}.py`, `eb_app/settings.py`, `eb_app/bills.py`, `eb_app/analytics.py` |
| Automation | `eb_app/{scheduler,jobs,worker,uploads,discovery}.py`, `watchdog.py`, `START_EB.bat` |
| Alerts / e-mail | `eb_app/{alerts,notify,audit}.py`, `eb_app/ops_settings.py` (SMTP settings, read-only) |
| Website | `app.py`, `eb_app/{auth,viewer,reports}.py`, `templates/*.html`, `static/css/eb.css`, `static/js/{eb,charts}.js` |
| Reader fixes found by Phase 3 testing | `eb_app/reader/charges.py` (a number that is a PF value / a rate / the bill total is *not* a penalty amount → `UNREADABLE`), `reader/textnorm.py` (`3.234.47` → 3,234.47), `reader/store.py` (uploads, archive-not-delete, retry, outdated re-read), `reader/pipeline.py` |
| Tests / tools | `tests/{test_phase3,test_phase4,test_phase5,test_phase6,synth}.py`, `tests/gold/penalties.json`, `scripts/{make_schema_doc}.py`, `scripts/dev/{penalty_lines,field}.py` |
| Docs | `README.md`, `docs/DOCUMENTATION.md`, `docs/SCHEMA.md` |

RDC-OPS: **not modified** (its database is opened `mode=ro`).

## 3. Oracle queries used

All are plain `SELECT`s with bind variables, run through `OracleSource.query()` (guarded). Object names were verified against the live data dictionary in Phase 1.

| Purpose | Statement (abridged) |
|---|---|
| Item code → item ids | `SELECT DISTINCT segment1, inventory_item_id FROM apps.mtl_system_items_b WHERE segment1 IN (:c0…)` |
| PO lines of the item | `SELECT l.po_line_id, l.po_header_id, l.line_num, l.item_description, l.item_id FROM apps.po_lines_all l WHERE l.item_id IN (:i0…)` |
| PO header + supplier + plant | `po_headers_all h LEFT JOIN ap_suppliers s … LEFT JOIN po_lines_all l (item) LEFT JOIN po_distributions_all d LEFT JOIN org_organization_definitions od` — plant = `d.destination_organization_id` → `od.organization_code/name`; billing period = `h.attribute1/2`; consumer hint = `h.attribute3` |
| Attachments (header and line level) | `fnd_attached_documents a JOIN fnd_documents d LEFT JOIN fnd_documents_tl tl LEFT JOIN fnd_lobs lb` filtered by `a.entity_name` / `a.pk1_value IN (…)` and, for incremental scans, `a.last_update_date >= :since`; `dbms_lob.getlength` for the size |
| File content | `SELECT file_name, file_content_type, file_data FROM apps.fnd_lobs WHERE file_id = :f` (streamed, size-capped at 40 MB) |
| Connection test | `SELECT user, sys_context('USERENV','DB_NAME') FROM dual` |

`FND_DOCUMENTS.DATATYPE_ID` 6 = file in `FND_LOBS`; 5 = web link (581 DMS links exist and cannot be downloaded). `STORAGE_TYPE` is NULL in this instance and is not used.

## 4. Database schema

22 tables, versioned migrations (`PRAGMA user_version` = 3), foreign keys on, WAL mode. Full column list: **`docs/SCHEMA.md`** (generated from the migrations).
Relationships: `plant → consumer account → billing period → bill → PO → attachment` is `electricity_plants → electricity_consumer_accounts → electricity_bills(group_key =
account|month) → electricity_pos → electricity_attachments`, with `electricity_bills.extraction_id → electricity_bill_extractions → electricity_bill_fields / electricity_charge_lines`.
Uniqueness: one extraction per (document, hash, reader version); one bill per extraction; one open alert per dedupe key; one active job per (kind, reference); one scan lock.

## 5. Extraction pipeline

`file → loader (type by content; HTTP-wrapper unwrapping) → cells with bounding boxes (native PDF text, or local RapidOCR with orientation / deskew; Devanagari / Kannada / Tamil / Telugu
second pass) → extractor (semantic label dictionary + spatial relations: inline, right, below, table intersection, section total, stacked headers) → joint kWh / kVAh / PF selection
(kWh÷kVAh must reproduce the printed PF) → billing basis from the arithmetic of the energy charge → charge summary → confidence + verification status → store`.
Details, traps handled and measured accuracy: README "Phase 2". New in Phase 6: a number on a penalty line that is a PF value, a *rate* or as large as the bill is **not** taken as the
penalty (`UNREADABLE` + review) — found because the Phase 3 totals exposed ₹9.97 lakh "excess MD charges" on a ₹1.1 lakh bill.

Per-field record: value, original label, original / normalised unit, page, bounding box, evidence text, method, OCR confidence, confidence, status (`FOUND / ZERO / NOT_FOUND /
NOT_APPLICABLE / UNREADABLE / CALCULATED`), verification (`AUTO / NEEDS_REVIEW / VERIFIED / CORRECTED`), alternatives that were rejected.

## 6. PF calculation logic (`eb_app/analysis/engine.py`)

* **PF as per bill** is stored as read; **PF calculated = kWh ÷ kVAh** is computed independently from the same bill and only when 0.30 ≤ kWh/kVAh ≤ 1 (kWh > kVAh is impossible; < 0.30 is a misread, not a power factor).
  **Difference** and **validation status**: `MATCH`, `MISMATCH` (beyond `pf_tolerance`, default 0.02 → alert), `INVALID_ENERGY`, `NOT_COMPARABLE_*`, `NO_PF`. The billed PF is never overwritten.
* **Management bands** (settings): ≥ 0.99 Excellent · ≥ 0.95 Good · ≥ 0.90 Attention · < 0.90 Critical — *internal monitoring bands, not electricity-board thresholds.*
* **Actual penalty / incentive** = the amount printed on the bill (sign decides penalty vs incentive; a combined label is never guessed). **Expected penalty** is computed only from an
  **approved** effective-dated tariff rule (`pct_per_step` % of a named base per `step` of PF below `threshold`, with floor/ceil/exact rounding and optional cap) and compared with the bill.
  Without a rule: `UNAVAILABLE_NO_RULE`.
* **kWh-billed bills:** PF affects penalties only → no saving (`NOT_APPLICABLE_KWH_BASIS`).
  **kVAh-billed bills:** `current kVAh = kWh ÷ PF`, `target kVAh = kWh ÷ target PF` (default 0.99), `reduction = current − target`, `estimated saving = reduction × rate`.
  Rate = the approved rule's marginal ₹/kVAh if there is one, else the bill's own average energy charge per kVAh — labelled `BILL_AVERAGE_ENERGY_RATE` (indicative only).
  Status says `ESTIMATE_UNVERIFIED_INPUTS` until the bill is verified. Never computed when the basis is unknown / mixed or PF inputs contradict each other. **Never added to, or called, a penalty.**
* **Company / plant / state PF is energy-weighted:** Σ kWh ÷ Σ kVAh over plausible, self-consistent bills — shown beside (never mixed with) the plain mean of billed PFs.

## 7. Demand penalty logic

* Contract demand, maximum recorded demand, billing demand and sanctioned load are four separate fields with their own units.
* **Utilisation % = maximum demand ÷ contract demand × 100**, only when the units are compatible: kVA/MVA/VA together, kW/MW/HP together (1 HP = 0.7457 kW). **kW is never compared with kVA**
  (the PF at the peak is unknown) and an energy unit (kWh/kVAh) in a demand field is rejected → `UNITS_INCOMPATIBLE` / `UNIT_UNKNOWN` → review.
  A recorded demand above 3× the contract is treated as a misread (`IMPLAUSIBLE_VALUE`), not a violation.
* **Above contract demand is not automatically a violation or a penalty.** Violation = recorded demand above the *permitted limit* (contract × (1 + tolerance %)) of an **approved** rule;
  without a rule the status is `UNVERIFIED_NO_RULE` (informational alert). If the electricity board itself billed an excess-demand penalty the bill is `VIOLATION_BILLED` (an actual fact).
* Regular **demand charges are never** treated as an excess-demand penalty (separate fields). Expected excess penalty (approved rule): (MD − reference) × rate × multiplier, reference = contract demand
  or tolerance limit as the rule states. Violation frequency per plant = violations ÷ months with a comparable reading.

## 8. Scheduler configuration

* Runs inside the website process (APScheduler thread + job worker) or standalone (`python -m eb_app.worker`); both can run at once — a database lock allows one scan at a time.
* Interval `scan_interval_minutes` (default **120 = 2 hours**). The schedule (`sched.next_due`) is stored in the database: a restart resumes it; a long outage triggers a scan at start-up.
* Failure back-off: 5, 10, 20, 40 … minutes (never longer than the interval); after `scheduler_failure_alert_after` (3) consecutive failures a CRITICAL alert is raised and e-mailed.
* The first cycle of each day after `reconcile_hour` (02:00) is a **full** reconciliation; the others are **incremental** (attachments changed since the last success minus a 24 h overlap), so an old PO that
  receives a bill months later is found. `backfill_from` limits how far back downloads go. Per cycle: `download_batch` (200) attachments and `process_budget_minutes` (90) of reading — the rest continues next cycle.
* Failed downloads retry after 10, 20, 40 … minutes (max 5); failed jobs retry 3 times with back-off; interrupted work is re-queued at start-up.
* All settings: **Settings** page (admin). Monitoring, *Scan now*, *Retry failed*, logs: **Automation** page.

## 9. Manual upload process

**Upload Bill** (administrators, regional and plant users — plant users only for their plants): plant (required), consumer number, board, PO number, billing month (optional), file.
Validation by *content*: PDF / JPG / PNG / TIFF / WebP / BMP, ≤ 40 MB, image size cap, PDFs with JavaScript / launch actions / embedded files rejected; the file is stored under its SHA-256 (the client's name is
only a label). The same file is never stored twice. The file is queued, read by the same reader (the form's consumer number and month act as the independent hint a PO header gives), then the
bill page opens as an **editable verification form** (original on the left, values + evidence on the right). Corrections keep the original value and are audited. If the file is later found attached to an
Oracle PO it is linked (`linked_attachment_id`) and the Oracle copy is marked duplicate — the bill with the person's work stays.

## 10. Alert configuration

Types: extraction failed · missing PF · missing kWh/kVAh (where needed) · PF inconsistent with kWh/kVAh · PF below management band · actual PF penalty · contract demand exceeded (rule-based = CRITICAL; no rule =
INFO) · actual excess-demand penalty · penalty amount unreadable · amounts contradict each other · demand misread · duplicate / revised bill · Oracle attachment download failure · repeated scheduler failure ·
missing expected bill (only for accounts that have an expected-bill schedule; never assumes monthly).
Severity CRITICAL / WARNING / INFO. Each alert has a dedupe key (**one alert, not one per scan**), resolves itself when the condition clears, and can be acknowledged / resolved by a person.
E-mail (Settings): on/off, recipients, CC, minimum severity (default CRITICAL), re-send interval for still-open alerts (default 72 h, 0 = never), escalation after N hours unacknowledged to a second recipient list,
link base URL. One digest e-mail per run (max 40 alerts); content = plant, board, month, bill no., PO, PF, contract / maximum demand, penalty, problem, required action, link. Uses the SMTP account stored in RDC-OPS.

## 11. Application run commands

```
.venv\Scripts\python app.py                         website + scheduler + worker  (http://127.0.0.1:2002; EB_HOST / EB_PORT)
START_EB.bat                                         the same, restarted automatically by watchdog.py (Windows)
.venv\Scripts\python -m eb_app.worker               scheduler + worker without the website
.venv\Scripts\python -m eb_app.cli init|ping|scan|download|process|read|status|rebuild|cycle|mail-test
.venv\Scripts\python -m unittest discover -s tests  all tests
.venv\Scripts\python scripts\dev\evaluate_gold.py penalties.json     accuracy against hand-verified bills
.venv\Scripts\python scripts\make_schema_doc.py     regenerate docs/SCHEMA.md
```

## 12. Deployment instructions

1. Python 3.12; `python -m venv .venv`; `.venv\Scripts\python -m pip install -r requirements.txt` (own virtual environment — RDC-OPS's packages are untouched). First OCR run downloads the model files once.
2. The Oracle Instant Client and the Oracle / SMTP / user settings are taken from RDC-OPS automatically (same PC, sibling folder; `RDC_OPS_DIR` points elsewhere). Overrides: `EB_ORACLE_*`, `EB_SMTP_*`, `EB_AUTH_DB`.
3. `START_EB.bat` (shortcut in the Windows Startup folder). Logs: `data/server.log`. Back up `data/eb.db` and `storage/` (the originals).
4. Network: the default is localhost only. To let other PCs in, set `EB_HOST=0.0.0.0` and put HTTPS in front (then set `EB_HTTPS=1` so the cookie is `Secure`) — the login is the RDC-OPS login.
5. First start: the scheduler scans at once and then reads the backlog (about 2,700 attachments) over several 90-minute cycles; the dashboard fills as bills are read. `backfill_from` limits the history.
6. Switch alert e-mails on in Settings after reviewing the first alerts (the first backfill creates many; the default threshold only e-mails CRITICAL).

## 13. Test results

Run date 2026-10-13, Windows 11, Python 3.12, live Oracle RDCAZPRD (read-only). `python -m unittest discover -s tests` → **287 tests, all passing** (about 3½ minutes). They cover the Phase 1 discovery logic, the reader (incl. real hand-verified bills), the PF / demand / tariff engine, analytics, alerts and e-mail, the scheduler and job queue, uploads, the whole website and Excel reports, failure injection, security and performance.

Evidence against the completion requirements of the brief (6.8):

| Requirement | Evidence | Status |
|---|---|---|
| RDC-OPS styling reused | RDC-OPS `main.css`, `app.js`, `background.js`, sidebar/topbar/KPI/multi-select/theme picker copied unchanged (`static/css/eb.css` only adds); screenshots of every page reviewed against RDC-OPS | **done** |
| Oracle EBS connection works | `cli ping` (read-only database user, production database); a live scheduler cycle on 2026-10-13 15:35 found 2 new POs, 2 new attachments, downloaded 200 and read 45 attachments | **done** |
| Electricity POs identified | item EXP00084 → 4,320 POs, 116 plants; rules editable in the app and re-applied without Oracle | **done** |
| PO attachments accessible | FND_ATTACHED_DOCUMENTS → FND_DOCUMENTS → FND_LOBS read live; 581 web links reported as non-downloadable | **done** (web links: pending — need manual upload) |
| Different bill layouts processed | 98 suppliers and 110 distinct layout signatures in the 198-file corpus, native and scanned, English / Hindi; hand-verified sets (`docs/VALIDATION.md`): 181 verified values → 172 correct, 0 wrong, 2 missing, 0 wrong released automatically. First read of *unseen* layouts: 66 % and 83 % of fields right | **done, with the limits in 14** |
| kWh vs kVAh distinguished | basis from energy-charge arithmetic (86 of 180 bills decided, the rest `Unknown` → manual setting); 10/11 verified bases correct, 0 wrong | **done, ~half need a person** |
| PF calculations verified | independent kWh ÷ kVAh, tolerance, mismatch alerts, energy-weighted aggregates — hand-computed expectations in `tests/test_phase3.py` | **done** |
| Actual penalties extracted correctly | `tests/gold/penalties.json`: 31 PF-penalty / excess-demand amounts read off the printed bills → 31 correct; implausible numbers (PF value, rate, bill total on a penalty line) are `UNREADABLE`, never an amount | **done** |
| Contract-demand violations follow valid tariff rules | rule-gated logic and unit-safety tested (tolerance, units, no rule, spanning rule change). **No approved tariff rule exists yet**, so no rule-based violation is reported | **logic done; rules pending** |
| Automatic 2-hour scanning works | 2 h default; schedule persisted; tested with a fake clock (due / not due / back-off / restart) and **with the real app + live Oracle at a 3-minute interval**: cycles ran by themselves, the process was killed in the middle of a cycle, restarted, and resumed within 5 s with the stale lock freed; the website's health endpoint answered in 120 ms median (max 404 ms) while bills were being read (full-page timings under load were measured only on synthetic data, see Performance) | **done at accelerated interval; an unattended 2-hour wall-clock observation is pending** |
| Manual upload works | browser flow tested (validate → queue → read → editable form → correct → audit); duplicate upload; link to Oracle copy; hostile files refused | **done** |
| Originals remain accessible | stored unchanged by SHA-256 (hash verified after read / correct / retry); served to authorised users only | **done** |
| Historical data retained | re-reads archive, revised bills kept, nothing deleted | **done** |
| In-app and e-mail alerts work | 20+ alert rules tested; e-mail digest, de-duplication, re-send, escalation, failure retry tested with a fake SMTP server. **No e-mail has been sent to the wider list**; a real send was made once to the author's own address on request earlier in this project (the test report) | **in-app done; live e-mail pending approval** |
| Excel reports work | 11 workbooks open and verified (headers, number formats, filters, notes, formula-injection safe); a 2,880-bill register in ~5 s | **done** |
| Validation and failure handling | failure injection: Oracle down / timeout / bad login, full disk, failed OCR, corrupt PDF, interrupted download, concurrent workers, crash mid-cycle | **done** |
| Security and authorisation | login = RDC-OPS users, 5 roles, plant scoping on every page / file / alert / report, CSRF on every change, secure cookie, upload validation, SQL parameterisation checks, no secret in any response or source, audit log | **done** (HTTPS is a deployment step) |
| Performance | 120 plants × 24 months = 2,880 bills: KPIs < 1.5 s, plant table page < 0.8 s, dashboard page < 4 s (asserted in tests) | **done** |

Failures found *by* this testing and fixed (all with regression tests): an "Excess MD charges ₹9.97 lakh" on a ₹1.1 lakh bill and BESCOM "PF penalty ₹0.9" (a PF value / rate read as money);
"Fuel Adjustment Charges" filed as a credit; OCR "3.234.47" for 3,234.47; kWh/kVAh misreads giving company PF 0.40 (now excluded and counted); a maximum demand 57× the contract; a stale scan lock after a crash
blocking the restart for 30 minutes; PIL's global image limit being changed by the upload check; a PDF served with a CSP sandbox that browsers refuse to display.

## 14. Known limitations

* **Extraction accuracy on unseen layouts is 66–83 % of fields right on the first read** (measured, README); most wrong values are flagged for review but *no bill is VERIFIED until a person verifies it*. Every figure on the dashboard is therefore labelled with how many bills are unverified. At the 0.95 critical-confidence default only about 28 % of correct values release automatically — the review workload is real.
* **Billing basis (kWh / kVAh) is `Unknown` for about half of the bills** (it needs the energy-charge arithmetic); the saving is unavailable for those until a person sets the basis.
* **No tariff rule is shipped.** Until an administrator enters and approves rules (with a source document), independent penalty checks and contract-demand *violations* are unavailable by design; only the amounts printed on the bills are reported.
* Languages: English good; Hindi / Marathi partial; Kannada / Tamil / Telugu OCR wired but no vocabulary; Bengali / Gujarati / Malayalam / Odia / Punjabi have no OCR model (reported, never guessed).
* Photographs of bills are flagged (capped at 0.8 / review). The evidence highlight on a scan that had to be rotated is approximate or omitted (the viewer says so).
* 581 attachments are web links (DMS), not stored in Oracle — they cannot be read automatically (they are listed; upload them manually).
* Some PO ↔ plant relations are many-to-one (a PO with several distribution organisations takes the first); `plant_count` is stored for review.
* SQLite is used (as in RDC-OPS). It is adequate for this volume (120 plants × 12 months × a few meters); concurrency is handled with WAL + short transactions.
* Excel exports are full-fidelity but not streamed; a register of >100 k rows will be slow.

## 15. Pending integrations / configuration

* **Approved tariff rules** (state / board, PF penalty & incentive, tolerance, excess-demand, rates) — to be entered from the tariff orders. Pending: independent penalty verification and violation flags.
* **Verification of bills by a person** — nothing is verified yet; this is the main outstanding work.
* **E-mail recipients / switch-on** — SMTP is read from RDC-OPS; recipients and the switch are in Settings (off by default). A live send was tested only to the author's own address; sending to the wider list is pending your approval.
* **AI-assisted extraction** — hook exists, **off**, no provider bundled; needs an approved provider before any document text could leave the machine.
* **2-hour interval in a long unattended run** — verified with an accelerated interval and a real restart (section 13); one uninterrupted wall-clock 2-hour observation on the production PC is pending.
* **Consumer category / voltage level per account** — needed to match category-specific tariff rules; set per account on the plant page.
* HTTPS / network exposure — deployment decision for IT.
