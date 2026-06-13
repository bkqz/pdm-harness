"""
tools.py — Checkpoint layer (The Checkpoints)

Provides deterministic, SQL-backed verification functions.
Each function returns a hard binary PASS/FAIL state grounded in relational
ERP/CMMS data. These are NOT semantic checks — they are exact constraint queries.
The engine calls these checkpoints after every LLM draft before any approval.

Asset modelled: Main Oil Line (MOL) Centrifugal Pump — API 610 OH2 type,
Well Pad 3, Oil Gathering Station. Flowserve PVXM 12x10-17, 200 kW / 6 kV.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from . import alarms
from . import observability as obs
from .material import ValidationResult, WorkOrderRequest

# Always resolve relative to project root, regardless of working directory
DB_PATH = Path(__file__).parent.parent / "erp_state.db"


def _get_connection() -> sqlite3.Connection:
    """Opens a connection to the local ERP state database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def bootstrap_db() -> None:
    """
    Drops and recreates all ERP/CMMS tables, then seeds them with realistic
    MOL pump data. Called once at engine startup to guarantee a known baseline.
    Drop-and-recreate (not IF NOT EXISTS) ensures schema changes are always applied.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    cur.executescript("""
        DROP TABLE IF EXISTS maintenance_bom;
        DROP TABLE IF EXISTS work_orders;
        DROP TABLE IF EXISTS part_compatibility;
        DROP TABLE IF EXISTS purchase_orders;
        DROP TABLE IF EXISTS inventory;
        DROP TABLE IF EXISTS assets;

        CREATE TABLE assets (
            asset_id              TEXT PRIMARY KEY,
            name                  TEXT NOT NULL,
            location              TEXT NOT NULL,
            asset_type            TEXT NOT NULL,
            manufacturer          TEXT,
            model_number          TEXT,
            serial_number         TEXT,
            installation_date_utc TEXT,
            last_service_utc      TEXT,
            rated_flow_m3h        REAL,
            rated_head_m          REAL,
            rated_power_kw        REAL,
            criticality           TEXT DEFAULT 'B'
        );

        CREATE TABLE inventory (
            part_number      TEXT PRIMARY KEY,
            description      TEXT NOT NULL,
            category         TEXT NOT NULL,
            manufacturer     TEXT,
            quantity         INTEGER NOT NULL DEFAULT 0,
            min_stock_level  INTEGER NOT NULL DEFAULT 1,
            unit_cost_usd    REAL NOT NULL DEFAULT 0.0,
            lead_time_days   INTEGER DEFAULT 7,
            storage_location TEXT
        );

        CREATE TABLE part_compatibility (
            part_number TEXT NOT NULL,
            asset_id    TEXT NOT NULL,
            PRIMARY KEY (part_number, asset_id)
        );

        CREATE TABLE purchase_orders (
            po_id          TEXT PRIMARY KEY,
            part_number    TEXT NOT NULL,
            asset_id       TEXT NOT NULL,
            quantity       INTEGER NOT NULL DEFAULT 1,
            unit_cost_usd  REAL NOT NULL,
            total_cost_usd REAL NOT NULL,
            status         TEXT NOT NULL DEFAULT 'PENDING',
            raised_utc     TEXT NOT NULL,
            run_id         TEXT NOT NULL
        );

        CREATE TABLE work_orders (
            ticket_id          TEXT PRIMARY KEY,
            asset_id           TEXT NOT NULL,
            status             TEXT NOT NULL CHECK(status IN ('OPEN','IN_PROGRESS','CLOSED')),
            priority           TEXT NOT NULL,
            fault_description  TEXT,
            action_taken       TEXT,
            technician         TEXT,
            estimated_cost_usd REAL,
            actual_cost_usd    REAL,
            parts_used         TEXT,
            created_utc        TEXT NOT NULL,
            closed_utc         TEXT
        );

        CREATE TABLE maintenance_bom (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id       TEXT NOT NULL,
            fault_category TEXT NOT NULL,
            part_number    TEXT NOT NULL,
            quantity       INTEGER NOT NULL DEFAULT 1,
            notes          TEXT
        );
    """)

    # ------------------------------------------------------------------
    # Asset register
    # ------------------------------------------------------------------
    assets = [
        # --- MOL Pump Train ---
        ("MOL-PUMP-001",
         "Main Oil Line Centrifugal Pump",
         "Well Pad 3 / Oil Gathering Station / Skid A",
         "PUMP", "Flowserve", "PVXM 12x10-17", "FLS-2019-00441",
         "2019-04-12T06:00:00", "2026-05-08T14:30:00",
         320.0, 185.0, 200.0, "A"),

        ("MOL-MOTOR-001",
         "Main Oil Line Pump Drive Motor 200kW",
         "Well Pad 3 / Oil Gathering Station / Skid A",
         "MOTOR", "WEG", "W22 200kW 6kV IE3", "WEG-2019-77821",
         "2019-04-12T06:00:00", "2026-04-15T09:00:00",
         None, None, 200.0, "A"),

        ("MOL-COUP-001",
         "Pump-Motor Flexible Disc Coupling",
         "Well Pad 3 / Oil Gathering Station / Skid A",
         "COUPLING", "Rexnord", "Thomas 710 Series", "RXN-2019-0988",
         "2019-04-12T06:00:00", "2026-05-08T14:30:00",
         None, None, None, "A"),

        # --- Support Equipment ---
        ("PUMP-042",
         "Centrifugal Feed Pump",
         "Plant A / Line 3",
         "PUMP", "Sulzer", "CPT-40-200", "SUL-2020-1122",
         "2020-06-01T08:00:00", "2025-11-01T08:00:00",
         95.0, 62.0, 18.5, "B"),

        ("COMP-017",
         "Instrument Air Compressor",
         "Plant B / Utility Room",
         "COMPRESSOR", "Atlas Copco", "GA55+ VSD", "AC-2018-5543",
         "2018-03-15T10:00:00", "2025-09-15T14:00:00",
         None, None, 55.0, "B"),

        ("FAN-008",
         "Cooling Tower Fan",
         "Plant A / Roof Level",
         "FAN", "Howden", "VAH-1400-6P", "HOW-2021-3310",
         "2021-07-20T07:00:00", "2026-01-10T06:30:00",
         None, None, 7.5, "C"),
    ]

    cur.executemany("""
        INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, assets)

    # ------------------------------------------------------------------
    # Spare parts inventory  (MOL pump-specific + general plant stock)
    # ------------------------------------------------------------------
    inventory = [
        # === BEARINGS ===
        ("BRG-6318-C3",
         "SKF 6318/C3 Deep Groove Ball Bearing — Pump Drive-End",
         "BEARING", "SKF", 3, 1, 285.00, 14, "WH-A / Rack B3"),

        ("BRG-6314-C3",
         "SKF 6314/C3 Deep Groove Ball Bearing — Pump Non-Drive-End",
         "BEARING", "SKF", 4, 1, 168.00, 14, "WH-A / Rack B3"),

        ("BRG-7319-BECBM",
         "SKF 7319 BECBM Angular Contact Bearing — Pump Thrust (pair)",
         "BEARING", "SKF", 2, 1, 420.00, 21, "WH-A / Rack B3"),

        ("BRG-6316-C3-MOT",
         "SKF 6316/C3 Deep Groove Bearing — Motor Drive-End",
         "BEARING", "SKF", 2, 1, 198.00, 14, "WH-A / Rack B4"),

        ("BRG-6313-C3-MOT",
         "SKF 6313/C3 Deep Groove Bearing — Motor Non-Drive-End",
         "BEARING", "SKF", 3, 1, 145.00, 14, "WH-A / Rack B4"),

        ("BRG-6309-C3",
         "SKF 6309/C3 Deep Groove Ball Bearing — PUMP-042 DE/NDE",
         "BEARING", "SKF", 4, 2, 98.00, 10, "WH-A / Rack B2"),

        # === MECHANICAL SEALS ===
        ("SEAL-JC-T2-75MM",
         "John Crane Type 2 API 682 Cartridge Mechanical Seal 75mm — MOL Pump",
         "SEAL", "John Crane", 2, 1, 2850.00, 28, "WH-B / Sealed Cabinet S1"),

        ("SEAL-GLAND-316",
         "Seal Gland Plate AISI 316L SS — MOL Pump",
         "SEAL", "Flowserve", 2, 1, 485.00, 21, "WH-B / Rack S2"),

        ("SEAL-ORING-KIT-VIT",
         "Viton O-Ring Kit Primary Seal 75mm (full set)",
         "SEAL", "John Crane", 8, 2, 75.00, 7, "WH-B / Bin S5"),

        ("SEAL-BUFFER-FLUID-5L",
         "Barrier/Buffer Fluid Shell Irus P32 (5L) — API Plan 52/53",
         "SEAL", "Shell", 6, 2, 115.00, 3, "WH-B / Fluid Store"),

        ("SEAL-007",
         "Mechanical Seal Type-B — PUMP-042 (legacy)",
         "SEAL", "EagleBurgmann", 4, 1, 385.00, 14, "WH-B / Bin S6"),

        # === WEAR PARTS ===
        ("IMP-PVXM-304MM",
         "Impeller D304mm 316SS Trim — MOL-PUMP-001 BEP Trim",
         "WEAR_PART", "Flowserve", 1, 1, 4200.00, 42, "WH-C / Heavy Shelf H1"),

        ("WEAR-RING-CASE-SS",
         "Casing Wear Ring 316SS D325mm — MOL Pump",
         "WEAR_PART", "Flowserve", 2, 1, 580.00, 21, "WH-C / Shelf H2"),

        ("WEAR-RING-IMP-SS",
         "Impeller Wear Ring 316SS D300mm — MOL Pump",
         "WEAR_PART", "Flowserve", 2, 1, 520.00, 21, "WH-C / Shelf H2"),

        ("IMP-150MM-316SS",
         "Impeller 150mm 316SS — PUMP-042",
         "WEAR_PART", "Sulzer", 0, 1, 850.00, 28, "WH-C / Heavy Shelf H3"),

        # === GASKETS ===
        ("GASK-SW-DN200-VIT",
         "Spiral Wound Gasket DN200 PN40 Viton/316SS — MOL Pump Discharge",
         "GASKET", "Flexitallic", 6, 2, 112.00, 7, "WH-A / Bin G1"),

        ("GASK-SW-DN150-VIT",
         "Spiral Wound Gasket DN150 PN40 Viton/316SS — MOL Pump Suction",
         "GASKET", "Flexitallic", 8, 2, 88.00, 7, "WH-A / Bin G1"),

        ("GASK-CASING-VIT-3MM",
         "Casing Parting Gasket Viton 3mm — MOL Pump",
         "GASKET", "Flowserve", 4, 1, 195.00, 14, "WH-A / Bin G2"),

        ("GASK-DN100-VIT",
         "Spiral Wound Gasket DN100 PN16 Viton/316SS — General",
         "GASKET", "Flexitallic", 12, 4, 68.00, 7, "WH-A / Bin G3"),

        # === COUPLING ===
        ("COUP-DISC-PACK-710",
         "Rexnord Thomas 710 Disc Pack Flexible Element (set of 3)",
         "COUPLING", "Rexnord", 3, 1, 285.00, 14, "WH-A / Rack C1"),

        ("COUP-SPACER-SS",
         "Coupling Spacer Shaft 316SS 75mm — MOL Pump",
         "COUPLING", "Rexnord", 1, 1, 620.00, 21, "WH-A / Rack C1"),

        # === INSTRUMENTATION & SENSORS ===
        ("SENS-VIB-EDDY-5MM",
         "Bently Nevada 3300 XL Eddy Current Probe 5mm + Driver (pair)",
         "INSTRUMENT", "Baker Hughes", 4, 1, 845.00, 21, "WH-B / Instrument Cab"),

        ("SENS-VIB-4-20MA",
         "IMI 648B01 4-20mA Vibration Transmitter 100mV/g",
         "INSTRUMENT", "IMI Sensors", 4, 2, 465.00, 14, "WH-B / Instrument Cab"),

        ("SENS-TEMP-PT100",
         "PT100 RTD Temperature Sensor D6mm x 100mm — Bearing Housing",
         "INSTRUMENT", "Endress+Hauser", 6, 2, 85.00, 7, "WH-B / Bin I3"),

        ("TRANS-PRESS-EJA110E",
         "Yokogawa EJA110E Differential Pressure Transmitter 0-1 MPa",
         "INSTRUMENT", "Yokogawa", 2, 1, 1380.00, 28, "WH-B / Instrument Cab"),

        # === LUBRICATION ===
        ("GREASE-SKF-LGMT3-1KG",
         "SKF LGMT 3 General Purpose Bearing Grease 1kg",
         "LUBRICATION", "SKF", 12, 4, 52.00, 3, "WH-A / Lube Store"),

        ("OIL-MOBIL-DTE25-20L",
         "Mobil DTE 25 Turbine & Bearing Oil ISO VG 46 (20L)",
         "LUBRICATION", "Mobil", 4, 2, 165.00, 3, "WH-A / Lube Store"),

        # === HARDWARE ===
        ("BOLT-STUD-M24-316",
         "Stud Bolt M24x160 AISI 316SS A4-80 (pack of 4)",
         "HARDWARE", "Bollhoff", 20, 4, 48.00, 5, "WH-A / Fastener Bay"),

        ("NUT-HEX-M24-316",
         "Hex Nut M24 A4-80 AISI 316SS (pack of 10)",
         "HARDWARE", "Bollhoff", 15, 4, 22.00, 5, "WH-A / Fastener Bay"),

        ("GASK-DOWTY-M24",
         "Dowty Seal M24 Bonded Steel/NBR (pack of 10)",
         "HARDWARE", "Dowty", 8, 2, 18.00, 5, "WH-A / Bin G4"),
    ]

    cur.executemany("""
        INSERT INTO inventory
        VALUES (?,?,?,?,?,?,?,?,?)
    """, inventory)

    # ------------------------------------------------------------------
    # Part-to-asset compatibility  (the LLM may only pick from this list)
    # ------------------------------------------------------------------
    compatibility = [
        # --- MOL-PUMP-001: centrifugal pump bearings ---
        ("BRG-6318-C3",        "MOL-PUMP-001"),
        ("BRG-6314-C3",        "MOL-PUMP-001"),
        ("BRG-7319-BECBM",     "MOL-PUMP-001"),
        # --- MOL-PUMP-001: mechanical seal system ---
        ("SEAL-JC-T2-75MM",    "MOL-PUMP-001"),
        ("SEAL-GLAND-316",     "MOL-PUMP-001"),
        ("SEAL-ORING-KIT-VIT", "MOL-PUMP-001"),
        ("SEAL-BUFFER-FLUID-5L","MOL-PUMP-001"),
        # --- MOL-PUMP-001: wear parts ---
        ("IMP-PVXM-304MM",     "MOL-PUMP-001"),
        ("WEAR-RING-CASE-SS",  "MOL-PUMP-001"),
        ("WEAR-RING-IMP-SS",   "MOL-PUMP-001"),
        # --- MOL-PUMP-001: gaskets ---
        ("GASK-SW-DN200-VIT",  "MOL-PUMP-001"),
        ("GASK-SW-DN150-VIT",  "MOL-PUMP-001"),
        ("GASK-CASING-VIT-3MM","MOL-PUMP-001"),
        # --- MOL-PUMP-001: coupling ---
        ("COUP-DISC-PACK-710", "MOL-PUMP-001"),
        ("COUP-SPACER-SS",     "MOL-PUMP-001"),
        # --- MOL-PUMP-001: instruments ---
        ("SENS-VIB-EDDY-5MM",  "MOL-PUMP-001"),
        ("SENS-VIB-4-20MA",    "MOL-PUMP-001"),
        ("SENS-TEMP-PT100",    "MOL-PUMP-001"),
        ("TRANS-PRESS-EJA110E","MOL-PUMP-001"),
        # --- MOL-PUMP-001: lubrication & hardware ---
        ("GREASE-SKF-LGMT3-1KG","MOL-PUMP-001"),
        ("OIL-MOBIL-DTE25-20L","MOL-PUMP-001"),
        ("BOLT-STUD-M24-316",  "MOL-PUMP-001"),
        ("NUT-HEX-M24-316",    "MOL-PUMP-001"),
        ("GASK-DOWTY-M24",     "MOL-PUMP-001"),

        # --- MOL-MOTOR-001 ---
        ("BRG-6316-C3-MOT",    "MOL-MOTOR-001"),
        ("BRG-6313-C3-MOT",    "MOL-MOTOR-001"),
        ("GREASE-SKF-LGMT3-1KG","MOL-MOTOR-001"),

        # --- MOL-COUP-001 ---
        ("COUP-DISC-PACK-710", "MOL-COUP-001"),
        ("COUP-SPACER-SS",     "MOL-COUP-001"),
        ("BOLT-STUD-M24-316",  "MOL-COUP-001"),
        ("NUT-HEX-M24-316",    "MOL-COUP-001"),

        # --- PUMP-042 ---
        ("BRG-6309-C3",        "PUMP-042"),
        ("SEAL-007",           "PUMP-042"),
        ("IMP-150MM-316SS",    "PUMP-042"),
        ("GASK-DN100-VIT",     "PUMP-042"),
        ("GREASE-SKF-LGMT3-1KG","PUMP-042"),
        ("OIL-MOBIL-DTE25-20L","PUMP-042"),
        ("BOLT-STUD-M24-316",  "PUMP-042"),
        ("NUT-HEX-M24-316",    "PUMP-042"),

        # --- COMP-017 ---
        ("OIL-MOBIL-DTE25-20L","COMP-017"),
        ("GREASE-SKF-LGMT3-1KG","COMP-017"),

        # --- FAN-008 ---
        ("GREASE-SKF-LGMT3-1KG","FAN-008"),
    ]

    cur.executemany(
        "INSERT INTO part_compatibility VALUES (?,?)", compatibility
    )

    # ------------------------------------------------------------------
    # Work order history  (one OPEN to test duplicate-detection guardrail)
    # ------------------------------------------------------------------
    work_orders = [
        # === MOL-PUMP-001 — OPEN ticket (blocks new orders on this asset) ===
        ("WO-MOL-2026-003",
         "MOL-PUMP-001", "OPEN", "HIGH",
         "NDE bearing vibration elevated — DCS alarm triggered at 6.8 mm/s RMS. "
         "PT100 bearing temperature reading 81C, trending upward over past 48 hours.",
         None, None,
         1600.00, None,
         None,
         "2026-06-12T07:45:00", None),

        # === MOL-PUMP-001 history (CLOSED) ===
        ("WO-MOL-2026-002",
         "MOL-PUMP-001", "CLOSED", "HIGH",
         "DE bearing temperature rising — PT100 reading 92C (alarm set-point 85C). "
         "Vibration also elevated at 7.2 mm/s RMS.",
         "Replaced SKF 6318/C3 DE bearing and 7319 BECBM thrust bearing pair. "
         "Flushed bearing housing, repacked with SKF LGMT 3 grease. Vibration returned to 1.8 mm/s.",
         "J. Kowalski", 1850.00, 1620.00,
         '["BRG-6318-C3","BRG-7319-BECBM","GREASE-SKF-LGMT3-1KG"]',
         "2026-05-03T06:00:00", "2026-05-08T14:30:00"),

        ("WO-MOL-2026-001",
         "MOL-PUMP-001", "CLOSED", "MEDIUM",
         "Routine 2000-hour planned maintenance.",
         "Inspected coupling disc pack. Replaced API Plan 11 orifice filter. "
         "Updated vibration baseline: DE 1.9 mm/s, NDE 1.6 mm/s.",
         "A. Mukherjee", 420.00, 390.00,
         '["COUP-DISC-PACK-710","GREASE-SKF-LGMT3-1KG"]',
         "2026-02-10T07:00:00", "2026-02-10T16:00:00"),

        ("WO-MOL-2025-005",
         "MOL-PUMP-001", "CLOSED", "HIGH",
         "Coupling disc pack fatigue crack — lateral vibration spike to 9.1 mm/s.",
         "Replaced Rexnord Thomas 710 disc pack. Hot alignment check within API 686 tolerance.",
         "J. Kowalski", 1100.00, 980.00,
         '["COUP-DISC-PACK-710","COUP-SPACER-SS"]',
         "2025-11-18T08:00:00", "2025-11-19T15:30:00"),

        ("WO-MOL-2025-004",
         "MOL-MOTOR-001", "CLOSED", "HIGH",
         "Motor winding insulation resistance below threshold — IR test 85 MOhm (min 100 MOhm).",
         "Motor stator rewound (phase B). Post-rewind IR: 1.2 GOhm.",
         "Electrotechnik GmbH (contractor)", 12500.00, 11800.00,
         '[]',
         "2025-09-22T06:00:00", "2025-10-05T17:00:00"),

        ("WO-MOL-2025-003",
         "MOL-PUMP-001", "CLOSED", "MEDIUM",
         "Reduced pump flow — DCS trending 278 m3/h against rated 320 m3/h. Suspected impeller wear.",
         "Casing and impeller wear rings replaced (1.8mm clearance -> 0.3mm). Flow recovered to 318 m3/h.",
         "A. Mukherjee", 2200.00, 2050.00,
         '["WEAR-RING-CASE-SS","WEAR-RING-IMP-SS","GASK-CASING-VIT-3MM","BOLT-STUD-M24-316"]',
         "2025-07-14T08:00:00", "2025-07-16T12:00:00"),

        ("WO-MOL-2025-001",
         "MOL-PUMP-001", "CLOSED", "MEDIUM",
         "Annual planned maintenance — 8000h overhaul. Full bearing replacement.",
         "Replaced all pump bearings. Inspected John Crane seal — O-rings replaced. "
         "Laser aligned to 0.04mm offset.",
         "J. Kowalski", 8500.00, 7950.00,
         '["BRG-6318-C3","BRG-6314-C3","BRG-7319-BECBM","SEAL-ORING-KIT-VIT","GASK-CASING-VIT-3MM","GREASE-SKF-LGMT3-1KG"]',
         "2025-03-10T06:00:00", "2025-03-13T17:00:00"),

        # === PUMP-042 history ===
        ("WO-P042-2025-001",
         "PUMP-042", "CLOSED", "MEDIUM",
         "Mechanical seal weeping — minor oil seepage at gland plate.",
         "Replaced mechanical seal and gland O-rings. Leak resolved.",
         "I. Petrov", 620.00, 580.00,
         '["SEAL-007","GASK-DN100-VIT"]',
         "2025-10-14T09:00:00", "2025-11-01T08:00:00"),

        # === COMP-017 history ===
        ("WO-C017-2025-001",
         "COMP-017", "CLOSED", "LOW",
         "Routine 2000h service — oil change, filter replacement, valve inspection.",
         "Changed compressor oil. Replaced air/oil separator and inlet filter.",
         "A. Mukherjee", 480.00, 445.00,
         '[]',
         "2025-08-20T08:00:00", "2025-09-15T14:00:00"),

        # === FAN-008 history ===
        ("WO-F008-2026-001",
         "FAN-008", "CLOSED", "LOW",
         "Planned seasonal inspection — blade condition, bearing grease, belt tension.",
         "Blade leading edges showing minor surface erosion — acceptable. Belt tension adjusted.",
         "I. Petrov", 180.00, 165.00,
         '["GREASE-SKF-LGMT3-1KG"]',
         "2026-01-08T08:00:00", "2026-01-10T06:30:00"),
    ]

    cur.executemany("""
        INSERT INTO work_orders
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, work_orders)

    # ------------------------------------------------------------------
    # Maintenance BOM — maps (asset_id, fault_category) → parts to order
    # The agent diagnoses a fault_category; the harness looks up this table
    # to determine which parts to pull from inventory. Part numbers and costs
    # never pass through the agent — only the fault category label does.
    # ------------------------------------------------------------------
    maintenance_bom = [
        # ── MOL-PUMP-001: Flowserve PVXM 12×10-17 ─────────────────────────
        # BEARING_WEAR — full bearing set replaced together (opportunity maintenance)
        ("MOL-PUMP-001", "BEARING_WEAR", "BRG-6318-C3",        1, "Drive-end deep groove bearing"),
        ("MOL-PUMP-001", "BEARING_WEAR", "BRG-6314-C3",        1, "Non-drive-end deep groove bearing"),
        ("MOL-PUMP-001", "BEARING_WEAR", "BRG-7319-BECBM",     1, "Thrust bearing pair"),
        ("MOL-PUMP-001", "BEARING_WEAR", "GREASE-SKF-LGMT3-1KG", 1, "Bearing housing repack grease"),

        # SEAL_FAILURE — full cartridge seal replacement per API 682
        ("MOL-PUMP-001", "SEAL_FAILURE", "SEAL-JC-T2-75MM",    1, "John Crane cartridge seal"),
        ("MOL-PUMP-001", "SEAL_FAILURE", "SEAL-ORING-KIT-VIT", 1, "Viton O-ring replacement set"),
        ("MOL-PUMP-001", "SEAL_FAILURE", "SEAL-GLAND-316",     1, "316SS gland plate"),
        ("MOL-PUMP-001", "SEAL_FAILURE", "SEAL-BUFFER-FLUID-5L", 1, "API Plan 52/53 barrier fluid"),

        # IMPELLER_WEAR — wet-end rebuild with wear ring replacement
        ("MOL-PUMP-001", "IMPELLER_WEAR", "IMP-PVXM-304MM",    1, "BEP-trim impeller"),
        ("MOL-PUMP-001", "IMPELLER_WEAR", "WEAR-RING-CASE-SS", 1, "Casing wear ring 316SS"),
        ("MOL-PUMP-001", "IMPELLER_WEAR", "WEAR-RING-IMP-SS",  1, "Impeller wear ring 316SS"),
        ("MOL-PUMP-001", "IMPELLER_WEAR", "GASK-CASING-VIT-3MM", 1, "Casing parting gasket"),

        # COUPLING_FAULT — disc pack replacement per API 686
        ("MOL-PUMP-001", "COUPLING_FAULT", "COUP-DISC-PACK-710", 1, "Thomas 710 flexible disc pack"),
        ("MOL-PUMP-001", "COUPLING_FAULT", "COUP-SPACER-SS",   1, "Coupling spacer shaft 316SS"),

        # VIBRATION_SENSOR — instrument replacement
        ("MOL-PUMP-001", "VIBRATION_SENSOR", "SENS-VIB-EDDY-5MM", 1, "Eddy-current probe + driver"),
        ("MOL-PUMP-001", "VIBRATION_SENSOR", "SENS-VIB-4-20MA",   1, "4-20mA vibration transmitter"),

        # LUBRICATION — routine lube service
        ("MOL-PUMP-001", "LUBRICATION", "GREASE-SKF-LGMT3-1KG", 1, "Bearing housing repack"),
        ("MOL-PUMP-001", "LUBRICATION", "OIL-MOBIL-DTE25-20L",  1, "Turbine/bearing oil top-up"),

        # ROUTINE_INSPECTION — labour only, no parts consumed
        # (no BOM entries needed; harness returns empty parts list → cost = labour)

        # ── MOL-MOTOR-001: WEG W22 200kW ───────────────────────────────────
        ("MOL-MOTOR-001", "BEARING_WEAR",   "BRG-6316-C3-MOT",    1, "Motor DE bearing"),
        ("MOL-MOTOR-001", "BEARING_WEAR",   "BRG-6313-C3-MOT",    1, "Motor NDE bearing"),
        ("MOL-MOTOR-001", "BEARING_WEAR",   "GREASE-SKF-LGMT3-1KG", 1, "Motor bearing grease"),
        ("MOL-MOTOR-001", "LUBRICATION",    "GREASE-SKF-LGMT3-1KG", 1, "Motor bearing grease"),

        # ── MOL-COUP-001: Rexnord Thomas 710 ───────────────────────────────
        ("MOL-COUP-001", "COUPLING_FAULT",  "COUP-DISC-PACK-710", 1, "Flexible disc pack set"),
        ("MOL-COUP-001", "COUPLING_FAULT",  "COUP-SPACER-SS",     1, "Spacer shaft"),
        ("MOL-COUP-001", "COUPLING_FAULT",  "BOLT-STUD-M24-316",  2, "Coupling stud bolts (packs)"),
        ("MOL-COUP-001", "COUPLING_FAULT",  "NUT-HEX-M24-316",    2, "Coupling hex nuts (packs)"),
        ("MOL-COUP-001", "ROUTINE_INSPECTION", "GREASE-SKF-LGMT3-1KG", 1, "Coupling lubrication check"),

        # ── PUMP-042: Sulzer CPT-40-200 ────────────────────────────────────
        ("PUMP-042", "BEARING_WEAR",     "BRG-6309-C3",          2, "DE + NDE bearing pair"),
        ("PUMP-042", "BEARING_WEAR",     "GREASE-SKF-LGMT3-1KG", 1, "Bearing grease repack"),
        ("PUMP-042", "SEAL_FAILURE",     "SEAL-007",             1, "Mechanical seal EagleBurgmann"),
        ("PUMP-042", "SEAL_FAILURE",     "GASK-DN100-VIT",       1, "Gland gasket Viton DN100"),
        ("PUMP-042", "IMPELLER_WEAR",    "IMP-150MM-316SS",      1, "316SS replacement impeller"),
        ("PUMP-042", "IMPELLER_WEAR",    "GASK-DN100-VIT",       1, "Casing gasket"),
        ("PUMP-042", "LUBRICATION",      "GREASE-SKF-LGMT3-1KG", 1, "Bearing grease"),
        ("PUMP-042", "ROUTINE_INSPECTION", "GREASE-SKF-LGMT3-1KG", 1, "Lubrication top-up at inspection"),

        # ── COMP-017: Atlas Copco GA55+ VSD ────────────────────────────────
        ("COMP-017", "LUBRICATION",      "OIL-MOBIL-DTE25-20L",  1, "Compressor oil change"),
        ("COMP-017", "ROUTINE_INSPECTION", "OIL-MOBIL-DTE25-20L", 1, "Oil change at 2000h service"),

        # ── FAN-008: Howden VAH-1400-6P ────────────────────────────────────
        ("FAN-008", "BEARING_WEAR",      "GREASE-SKF-LGMT3-1KG", 1, "Fan bearing grease repack"),
        ("FAN-008", "LUBRICATION",       "GREASE-SKF-LGMT3-1KG", 1, "Fan bearing grease"),
        ("FAN-008", "ROUTINE_INSPECTION","GREASE-SKF-LGMT3-1KG", 1, "Grease top-up at inspection"),
    ]

    cur.executemany(
        "INSERT INTO maintenance_bom (asset_id, fault_category, part_number, quantity, notes) "
        "VALUES (?,?,?,?,?)",
        maintenance_bom,
    )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Checkpoint functions
# ---------------------------------------------------------------------------

def raise_purchase_order(
    part_number: str,
    asset_id: str,
    quantity: int,
    unit_cost_usd: float,
    run_id: str,
) -> str:
    """
    Creates a PENDING purchase order for an approved but out-of-stock part.
    Called automatically by check_inventory when quantity is zero and the part
    is approved for this asset. Returns the generated PO ID.
    The PO is written to the ERP database immediately and is independent of
    whether the triggering work order is ultimately approved or rejected.
    """
    po_id = f"PO-{uuid.uuid4().hex[:8].upper()}"
    raised_utc = datetime.now(timezone.utc).isoformat()
    total = quantity * unit_cost_usd

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO purchase_orders VALUES (?,?,?,?,?,?,?,?,?)",
        (po_id, part_number, asset_id, quantity, unit_cost_usd, total, "PENDING", raised_utc, run_id),
    )
    conn.commit()
    conn.close()

    obs.log_purchase_order(run_id, po_id, part_number, asset_id, quantity, unit_cost_usd)
    return po_id


def check_inventory(
    part_numbers: List[str],
    asset_id: str,
    run_id: str,
) -> ValidationResult:
    """
    Verifies parts against inventory and the approved parts list for this asset.
    - Parts in catalogue with stock > 0: PASS.
    - Parts approved for this asset but out of stock: PASS + automatic PO raised.
    - Parts not in catalogue or not approved for this asset: FAIL (blocking).
    This separation prevents both hallucinated part numbers and unapproved substitutions.
    """
    if not part_numbers:
        return ValidationResult(
            passed=True,
            check_name="check_inventory",
            detail="No parts required; inventory check skipped.",
            blocking=True,
        )

    conn = _get_connection()
    cur = conn.cursor()
    rejected: List[str] = []
    po_raised: List[str] = []
    low_stock: List[str] = []

    for part in part_numbers:
        row = cur.execute(
            "SELECT description, quantity, min_stock_level, unit_cost_usd "
            "FROM inventory WHERE part_number = ?",
            (part,),
        ).fetchone()

        if row is None:
            rejected.append(f"{part} (not in catalogue — use only approved part numbers)")
        elif row["quantity"] == 0:
            compat = cur.execute(
                "SELECT 1 FROM part_compatibility WHERE part_number = ? AND asset_id = ?",
                (part, asset_id),
            ).fetchone()
            if compat:
                # Approved but out of stock — raise a PO automatically
                po_id = raise_purchase_order(part, asset_id, 1, row["unit_cost_usd"], run_id)
                alarms.fire_alarm(
                    alarms.inventory_shortage(
                        run_id, asset_id, part, row["description"], po_id, row["unit_cost_usd"]
                    )
                )
                po_raised.append(
                    f"{part} ({row['description']}) — out of stock; {po_id} raised "
                    f"(${row['unit_cost_usd']:.2f})"
                )
            else:
                rejected.append(f"{part} (out of stock and not approved for {asset_id})")
        elif row["quantity"] <= row["min_stock_level"]:
            low_stock.append(f"{part} (qty {row['quantity']}, at minimum reorder level)")

    conn.close()

    if rejected:
        alarms.fire_alarm(alarms.part_not_approved(run_id, asset_id, rejected))
        return ValidationResult(
            passed=False,
            check_name="check_inventory",
            detail=(
                f"Parts rejected: {'; '.join(rejected)}. "
                "Use only part numbers from the approved catalogue provided in the prompt."
            ),
            blocking=True,
        )

    notes: List[str] = []
    if po_raised:
        notes.append(f"Purchase orders raised: {'; '.join(po_raised)}")
    if low_stock:
        notes.append(f"Low stock: {'; '.join(low_stock)}")

    detail = f"Inventory check passed for {len(part_numbers)} part(s)."
    if notes:
        detail += " | " + " | ".join(notes)

    return ValidationResult(
        passed=True,
        check_name="check_inventory",
        detail=detail,
        blocking=True,
    )


def check_active_tickets(asset_id: str, run_id: str) -> ValidationResult:
    """
    Checks whether an OPEN or IN_PROGRESS work order already exists for the asset.
    Prevents duplicate work orders on the same asset, which would cause
    conflicting maintenance actions and corrupt ERP scheduling integrity.
    """
    conn = _get_connection()
    cur = conn.cursor()

    rows = cur.execute(
        """
        SELECT ticket_id, status, priority, fault_description
        FROM work_orders
        WHERE asset_id = ? AND status IN ('OPEN', 'IN_PROGRESS')
        """,
        (asset_id,),
    ).fetchall()
    conn.close()

    if rows:
        existing = ", ".join(
            f"{r['ticket_id']} [{r['status']} / {r['priority']}]" for r in rows
        )
        alarms.fire_alarm(alarms.duplicate_ticket(run_id, asset_id, existing))
        return ValidationResult(
            passed=False,
            check_name="check_active_tickets",
            detail=(
                f"Asset {asset_id} already has active ticket(s): {existing}. "
                "Escalate or update the existing ticket rather than creating a duplicate."
            ),
            blocking=True,
        )

    return ValidationResult(
        passed=True,
        check_name="check_active_tickets",
        detail=f"No active tickets found for asset {asset_id}. Safe to raise new work order.",
        blocking=True,
    )


def check_asset_exists(asset_id: str, run_id: str) -> ValidationResult:
    """
    Confirms the asset ID exists in the ERP register and returns its operational
    context. Blocks work orders for unknown assets to prevent orphaned records.
    """
    conn = _get_connection()
    cur = conn.cursor()
    row = cur.execute(
        """
        SELECT name, location, asset_type, manufacturer, model_number,
               rated_flow_m3h, rated_head_m, rated_power_kw,
               criticality, last_service_utc
        FROM assets WHERE asset_id = ?
        """,
        (asset_id,),
    ).fetchone()
    conn.close()

    if row is None:
        alarms.fire_alarm(alarms.unknown_asset(run_id, asset_id))
        return ValidationResult(
            passed=False,
            check_name="check_asset_exists",
            detail=(
                f"Asset '{asset_id}' not found in ERP register. "
                "Verify the asset ID before raising a work order."
            ),
            blocking=True,
        )

    specs: List[str] = []
    if row["rated_flow_m3h"]:
        specs.append(f"rated flow {row['rated_flow_m3h']} m3/h")
    if row["rated_head_m"]:
        specs.append(f"head {row['rated_head_m']} m")
    if row["rated_power_kw"]:
        specs.append(f"power {row['rated_power_kw']} kW")

    spec_str = f" | Specs: {', '.join(specs)}" if specs else ""
    return ValidationResult(
        passed=True,
        check_name="check_asset_exists",
        detail=(
            f"Asset confirmed: {row['name']} ({row['manufacturer']} {row['model_number']}) "
            f"at {row['location']}{spec_str} | "
            f"Criticality: {row['criticality']} | "
            f"Last service: {row['last_service_utc']}"
        ),
        blocking=True,
    )


def get_available_parts(asset_id: str) -> str:
    """
    Returns a formatted catalogue of in-stock parts approved for this asset.
    Injected into the LLM prompt so the model cannot hallucinate part numbers
    — it can only pick from what is physically in the warehouse and pre-approved.
    """
    conn = _get_connection()
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT i.part_number, i.description, i.category, i.quantity, i.unit_cost_usd
        FROM inventory i
        JOIN part_compatibility pc ON i.part_number = pc.part_number
        WHERE pc.asset_id = ? AND i.quantity > 0
        ORDER BY i.category, i.part_number
        """,
        (asset_id,),
    ).fetchall()
    conn.close()

    if not rows:
        return (
            f"APPROVED PARTS FOR {asset_id}: none in stock. "
            "Use an empty list for required_parts."
        )

    lines = [f"APPROVED IN-STOCK PARTS FOR {asset_id} (use ONLY these part numbers):"]
    current_cat = None
    for row in rows:
        if row["category"] != current_cat:
            current_cat = row["category"]
            lines.append(f"  [{current_cat}]")
        lines.append(
            f"    {row['part_number']}: {row['description']}"
            f" | qty {row['quantity']} | ${row['unit_cost_usd']:.2f} each"
        )
    return "\n".join(lines)


def select_parts_for_fault(asset_id: str, fault_category: str) -> Dict[str, int]:
    """
    Looks up the maintenance BOM for a given asset and fault classification.
    Returns {part_number: quantity} — the exact parts and quantities the harness
    will source from inventory. The agent never sees or influences this mapping:
    it only provides a fault_category label; the harness does the rest.

    Returns an empty dict for ROUTINE_INSPECTION or any BOM gap.
    In that case the work order carries no parts — cost is labour only.
    """
    conn = _get_connection()
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT part_number, SUM(quantity) AS qty
        FROM maintenance_bom
        WHERE asset_id = ? AND fault_category = ?
        GROUP BY part_number
        ORDER BY part_number
        """,
        (asset_id, fault_category),
    ).fetchall()
    conn.close()
    return {row["part_number"]: row["qty"] for row in rows}


def compute_order_cost(parts_qty: Dict[str, int], labor_usd: float = 150.0) -> tuple[float, str]:
    """
    Computes the total work order cost from ERP inventory unit prices
    multiplied by BOM quantities, plus a fixed labour charge.

    All pricing is sourced from inventory.unit_cost_usd — the agent never
    touches cost figures. The engine calls this after BOM lookup and before
    guardrail enforcement so the cost ceiling check uses real ERP prices.
    """
    if not parts_qty:
        return labor_usd, f"No parts | Labour: ${labor_usd:.2f} | Total: ${labor_usd:.2f}"

    conn = _get_connection()
    cur = conn.cursor()
    parts_total = 0.0
    lines: List[str] = []

    for part, qty in parts_qty.items():
        row = cur.execute(
            "SELECT unit_cost_usd FROM inventory WHERE part_number = ?", (part,)
        ).fetchone()
        unit = row["unit_cost_usd"] if row else 0.0
        line_cost = unit * qty
        parts_total += line_cost
        lines.append(f"{part} x{qty} @ ${unit:.2f} = ${line_cost:.2f}")

    conn.close()
    total = parts_total + labor_usd
    breakdown = " | ".join(lines) + f" | Labour: ${labor_usd:.2f} | Total: ${total:.2f}"
    return total, breakdown


def create_work_order(wo: WorkOrderRequest, run_id: str) -> str:
    """
    Persists an approved work order to the ERP work_orders table with OPEN status.
    Must be called only after all guardrails pass — never for BLOCKED or REJECTED orders.
    Writing OPEN status here is what causes subsequent dispatches on the same asset
    to be blocked by check_active_tickets, closing the duplicate-prevention loop.
    """
    ticket_id = f"WO-{run_id[:8].upper()}"
    created_utc = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT OR IGNORE INTO work_orders
        (ticket_id, asset_id, status, priority, fault_description,
         action_taken, technician, estimated_cost_usd, actual_cost_usd,
         parts_used, created_utc, closed_utc)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ticket_id, wo.asset_id, "OPEN", wo.priority,
            wo.fault_description, wo.recommended_action,
            None, wo.estimated_cost_usd, None,
            json.dumps(wo.required_parts),
            created_utc, None,
        ),
    )
    conn.commit()
    conn.close()
    return ticket_id


def run_all_checkpoints(
    asset_id: str,
    part_numbers: List[str],
    run_id: str,
) -> List[ValidationResult]:
    """Runs all tool checkpoints in sequence and returns their results."""
    return [
        check_asset_exists(asset_id, run_id),
        check_active_tickets(asset_id, run_id),
        check_inventory(part_numbers, asset_id, run_id),
    ]
