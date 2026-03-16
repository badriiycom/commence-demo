#!/usr/bin/env python3
"""
VA Demo Data Loader
===================
Generates synthetic veteran FHIR records using Synthea and uploads them
to a public FHIR R4 server for the Commence VA RCM demo.

Enriches Synthea output with:
  - PACT Act presumptive condition flags
  - VASRD diagnostic code mappings
  - MCCF / Non-MCCF billing authority classification
  - SC/SA indicators and veteran liability flags
  - ICD-10 diagnosis codes injected into EOBs for varied denial risk profiles

Uploads Patient resources first, captures Firely-assigned IDs, then
rewrites all references before uploading remaining resources. Handles
both Synthea reference formats:
  - urn:uuid:synthea-id  →  Patient/server-id
  - Patient/synthea-id   →  Patient/server-id

Resource filters applied to keep upload fast:
  - Only uploads Patient, Condition, ExplanationOfBenefit
  - Only uploads EOBs created after EOB_CUTOFF_DATE (last 6 months)
  - All other Synthea resources are skipped

Usage:
  python va_data_loader.py

Intended to run:
  - On a schedule via GitHub Actions (every 6 hours)
  - Manually the morning of the demo (May 19, 2026)

No authentication required. No VA data used. All records are synthetic.
"""

import requests
import json
import os
import subprocess
import logging
import time
import sys
import uuid
from pathlib import Path
from datetime import datetime

# ── Configuration ──────────────────────────────────────────────────────────────

FHIR_BASE         = "https://server.fire.ly/r4"
SYNTHEA_PATH      = "./synthea"
OUTPUT_PATH       = "./synthea/output/fhir"
VETERAN_COUNT     = 10           # Number of synthetic veterans to generate
RANDOM_SEED       = 42           # Fixed seed — overridden by GitHub Actions YAML
STATE             = "Arizona"
CITY              = "Phoenix"    # VISN 18 — relevant to demo location
RETRY_COUNT       = 3            # Attempts per resource before giving up
RETRY_DELAY       = 2            # Base seconds between retries
RATE_LIMIT_SEC    = 0.1          # Pause between uploads
EOB_CUTOFF_DATE   = "2025-09-01" # Only upload EOBs after this date (last 6 months)

# Only upload resource types needed by demo pages
UPLOAD_RESOURCE_TYPES = {
    "Patient",
    "Condition",
    "ExplanationOfBenefit",
}

LOG_FILE = f"./va_loader_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

# ── VA Reference Data ──────────────────────────────────────────────────────────

# PACT Act presumptive conditions mapped to ICD-10 codes
PACT_ACT_CONDITIONS = {
    # Burn Pit / Airborne Hazard Presumptives
    "J44.0":   {"label": "COPD with acute lower respiratory infection",  "exposure": "Burn Pit"},
    "J44.1":   {"label": "COPD with acute exacerbation",                 "exposure": "Burn Pit"},
    "J44.9":   {"label": "COPD unspecified",                             "exposure": "Burn Pit"},
    "J45.20":  {"label": "Mild intermittent asthma uncomplicated",       "exposure": "Burn Pit"},
    "J45.40":  {"label": "Moderate persistent asthma uncomplicated",     "exposure": "Burn Pit"},
    "J45.41":  {"label": "Moderate persistent asthma with exacerbation", "exposure": "Burn Pit"},
    "J45.50":  {"label": "Severe persistent asthma uncomplicated",       "exposure": "Burn Pit"},
    "J68.0":   {"label": "Bronchitis due to solids and liquids",         "exposure": "Burn Pit"},
    "J70.2":   {"label": "Acute pulmonary manifestations from radiation","exposure": "Burn Pit"},
    "C34.10":  {"label": "Malignant neoplasm upper lobe bronchus",       "exposure": "Burn Pit"},
    "C34.90":  {"label": "Malignant neoplasm bronchus unspecified",      "exposure": "Burn Pit"},
    "C34.32":  {"label": "Malignant neoplasm lower lobe left bronchus",  "exposure": "Burn Pit"},
    # Agent Orange Presumptives
    "E11.9":   {"label": "Type 2 diabetes mellitus without compl.",      "exposure": "Agent Orange"},
    "E11.65":  {"label": "Type 2 diabetes with hyperglycemia",           "exposure": "Agent Orange"},
    "E11.40":  {"label": "Type 2 diabetes with neuropathy",              "exposure": "Agent Orange"},
    "E11.51":  {"label": "Type 2 diabetes with macular edema",           "exposure": "Agent Orange"},
    "E11.00":  {"label": "Type 2 diabetes with hyperosmolarity",         "exposure": "Agent Orange"},
    "C82.90":  {"label": "Follicular lymphoma unspecified",              "exposure": "Agent Orange"},
    "C91.10":  {"label": "Chronic lymphocytic leukemia",                 "exposure": "Agent Orange"},
    "C61":     {"label": "Malignant neoplasm prostate",                  "exposure": "Agent Orange"},
    "L40.0":   {"label": "Psoriasis vulgaris",                           "exposure": "Agent Orange"},
    # Camp Lejeune Water Contamination Presumptives
    "C67.9":   {"label": "Malignant neoplasm bladder unspecified",       "exposure": "Camp Lejeune"},
    "K29.70":  {"label": "Gastritis without bleeding",                   "exposure": "Camp Lejeune"},
    "N04.9":   {"label": "Nephrotic syndrome unspecified",               "exposure": "Camp Lejeune"},
    # Gulf War Presumptives
    "G89.21":  {"label": "Chronic pain due to trauma",                   "exposure": "Gulf War"},
    "G89.29":  {"label": "Other chronic pain",                           "exposure": "Gulf War"},
    "R53.82":  {"label": "Chronic fatigue unspecified",                  "exposure": "Gulf War"},
    "K92.1":   {"label": "Melena",                                       "exposure": "Gulf War"},
    # Combat / Service Related
    "F43.10":  {"label": "PTSD unspecified",                             "exposure": "Combat"},
    "F43.11":  {"label": "PTSD acute",                                   "exposure": "Combat"},
    "F43.12":  {"label": "PTSD chronic",                                 "exposure": "Combat"},
    "F43.0":   {"label": "Acute stress reaction",                        "exposure": "Combat"},
    "S09.90":  {"label": "Unspecified injury of head",                   "exposure": "Combat TBI"},
    "S06.0X0": {"label": "Concussion without loss of consciousness",     "exposure": "Combat TBI"},
    "S06.0X1": {"label": "Concussion with LOC less than 30 min",         "exposure": "Combat TBI"},
    "Z77.098": {"label": "Contact with other hazardous non-metals",      "exposure": "Burn Pit"},
}

# VASRD diagnostic code mapping
VASRD_MAP = {
    "F43.10":  {"code": "9411", "name": "PTSD, combat",                   "max_rating": 100},
    "F43.11":  {"code": "9411", "name": "PTSD, combat, acute",            "max_rating": 100},
    "F43.12":  {"code": "9411", "name": "PTSD, combat, chronic",          "max_rating": 100},
    "F43.0":   {"code": "9411", "name": "Acute stress/PTSD",              "max_rating": 100},
    "F32.9":   {"code": "9434", "name": "Major depressive disorder",      "max_rating": 100},
    "F32.1":   {"code": "9434", "name": "Major depression moderate",      "max_rating": 100},
    "F41.1":   {"code": "9400", "name": "Generalized anxiety disorder",   "max_rating": 50},
    "F41.9":   {"code": "9400", "name": "Anxiety disorder unspecified",   "max_rating": 50},
    "J44.1":   {"code": "6604", "name": "Asthma/COPD",                    "max_rating": 100},
    "J44.0":   {"code": "6604", "name": "COPD with infection",            "max_rating": 100},
    "J44.9":   {"code": "6604", "name": "COPD unspecified",               "max_rating": 100},
    "J45.40":  {"code": "6602", "name": "Asthma moderate persistent",     "max_rating": 60},
    "J45.41":  {"code": "6602", "name": "Asthma with exacerbation",       "max_rating": 60},
    "S09.90":  {"code": "8045", "name": "TBI residuals",                  "max_rating": 100},
    "S06.0X0": {"code": "8045", "name": "TBI concussion residuals",       "max_rating": 100},
    "S06.0X1": {"code": "8045", "name": "TBI with LOC",                   "max_rating": 100},
    "M54.5":   {"code": "5295", "name": "Lumbosacral strain",             "max_rating": 40},
    "M54.50":  {"code": "5295", "name": "Low back pain unspecified",      "max_rating": 40},
    "M54.4":   {"code": "5293", "name": "Intervertebral disc syndrome",   "max_rating": 60},
    "M17.11":  {"code": "5257", "name": "Knee instability",               "max_rating": 30},
    "M17.12":  {"code": "5257", "name": "Knee instability bilateral",     "max_rating": 30},
    "M17.31":  {"code": "5257", "name": "Secondary knee OA",              "max_rating": 30},
    "M75.1":   {"code": "5201", "name": "Rotator cuff syndrome",          "max_rating": 40},
    "M79.3":   {"code": "5025", "name": "Fibromyalgia/panniculitis",      "max_rating": 40},
    "M47.812": {"code": "5242", "name": "Cervical spondylosis",           "max_rating": 30},
    "G43.909": {"code": "8100", "name": "Migraine",                       "max_rating": 50},
    "G43.919": {"code": "8100", "name": "Migraine with aura",             "max_rating": 50},
    "G43.019": {"code": "8100", "name": "Migraine intractable",           "max_rating": 50},
    "G54.2":   {"code": "8520", "name": "Radiculopathy cervical",         "max_rating": 40},
    "E11.9":   {"code": "7913", "name": "Diabetes mellitus type 2",       "max_rating": 100},
    "E11.65":  {"code": "7913", "name": "Diabetes type 2 hyperglycemia",  "max_rating": 100},
    "E11.40":  {"code": "7913", "name": "Diabetes type 2 neuropathy",     "max_rating": 100},
    "E10.9":   {"code": "7913", "name": "Diabetes mellitus type 1",       "max_rating": 100},
    "H91.90":  {"code": "6100", "name": "Hearing loss bilateral",         "max_rating": 100},
    "H90.3":   {"code": "6100", "name": "Sensorineural hearing loss",     "max_rating": 100},
    "H90.6":   {"code": "6100", "name": "Hearing loss bilateral mixed",   "max_rating": 100},
    "H83.01":  {"code": "6260", "name": "Tinnitus right ear",             "max_rating": 10},
    "H83.09":  {"code": "6260", "name": "Tinnitus unspecified",           "max_rating": 10},
    "C34.10":  {"code": "6819", "name": "Malignant neoplasm respiratory", "max_rating": 100},
    "C34.90":  {"code": "6819", "name": "Lung cancer unspecified",        "max_rating": 100},
    "C61":     {"code": "7528", "name": "Malignant neoplasm prostate",    "max_rating": 100},
    "I10":     {"code": "7101", "name": "Hypertensive vascular disease",  "max_rating": 60},
    "I25.10":  {"code": "7005", "name": "Ischemic heart disease",         "max_rating": 100},
    "I50.9":   {"code": "7007", "name": "Heart failure unspecified",      "max_rating": 100},
    "L40.0":   {"code": "7816", "name": "Psoriasis",                      "max_rating": 60},
    "F10.20":  {"code": "9201", "name": "Alcohol use disorder",           "max_rating": 70},
    "G89.21":  {"code": "8025", "name": "Chronic pain syndrome",          "max_rating": 50},
    "G35":     {"code": "8018", "name": "Multiple sclerosis",             "max_rating": 100},
}

# Billing authority references
MCCF_AUTHORITY     = "38 USC 1729"
NON_MCCF_AUTHORITY = "38 USC 1722A / PACT Act PL 117-168"

# ── Step 1: Generate Synthea Data ──────────────────────────────────────────────

def generate_synthea_data():
    """
    Run Synthea to generate a synthetic veteran population.
    Skipped automatically when running in GitHub Actions.
    """

    synthea_dir = Path(SYNTHEA_PATH)

    if not synthea_dir.exists():
        log.error(f"Synthea not found at {SYNTHEA_PATH}")
        return False

    run_script = synthea_dir / "run_synthea"
    if not run_script.exists():
        log.error("run_synthea script not found")
        return False

    output_dir = Path(OUTPUT_PATH)
    if output_dir.exists():
        removed = 0
        for f in output_dir.glob("*.json"):
            f.unlink()
            removed += 1
        log.info(f"Cleared {removed} previous Synthea output files")

    log.info(f"Generating {VETERAN_COUNT} veteran records for {CITY}, {STATE}...")

    cmd = [
        str(run_script),
        "-p", str(VETERAN_COUNT),
        "-s", str(RANDOM_SEED),
        "--exporter.fhir.export", "true",
        "--exporter.fhir.us_core_version", "4.0.0",
        "--exporter.hospital.fhir.export", "false",
        "--exporter.practitioner.fhir.export", "false",
        "--exporter.fhir.bulk_data", "false",
        STATE, CITY
    ]

    try:
        result = subprocess.run(
            cmd,
            cwd=str(synthea_dir),
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode != 0:
            log.error(f"Synthea exited with code {result.returncode}")
            log.error(result.stderr[-2000:])
            return False

        files = list(Path(OUTPUT_PATH).glob("*.json"))
        log.info(f"Synthea generated {len(files)} FHIR bundle files")
        return len(files) > 0

    except subprocess.TimeoutExpired:
        log.error("Synthea timed out after 5 minutes")
        return False
    except FileNotFoundError:
        log.error("Could not execute run_synthea — check permissions")
        return False

# ── Step 2: Enrich With VA-Specific Extensions ─────────────────────────────────

def classify_mccf_status(icd_codes):
    """Determine MCCF vs Non-MCCF billing classification."""

    pact_matches = [c for c in icd_codes if c in PACT_ACT_CONDITIONS]
    is_pact      = len(pact_matches) > 0

    if is_pact:
        exposure = PACT_ACT_CONDITIONS[pact_matches[0]]["exposure"]
        return {
            "mccfStatus":          "NON-MCCF",
            "billingAuthority":    NON_MCCF_AUTHORITY,
            "veteranLiability":    False,
            "requiresSCReview":    True,
            "classificationBasis": f"PACT Act presumptive — {exposure} exposure",
            "humanReviewRequired": True,
        }
    else:
        return {
            "mccfStatus":          "MCCF",
            "billingAuthority":    MCCF_AUTHORITY,
            "veteranLiability":    True,
            "requiresSCReview":    False,
            "classificationBasis": "Standard third-party OHI billing",
            "humanReviewRequired": False,
        }

def enrich_condition(resource):
    """Add PACT Act and VASRD extensions to a Condition resource."""

    icd_codes  = [
        c.get("code", "")
        for c in resource.get("code", {}).get("coding", [])
        if "icd" in c.get("system", "").lower()
    ]
    extensions = resource.get("extension", [])

    for code in icd_codes:
        if code in PACT_ACT_CONDITIONS:
            pact_info = PACT_ACT_CONDITIONS[code]
            extensions.append({
                "url": "http://va.gov/fhir/StructureDefinition/pact-act-presumptive",
                "extension": [
                    {"url": "isPACTActPresumptive",  "valueBoolean": True},
                    {"url": "presumptiveLabel",       "valueString":  pact_info["label"]},
                    {"url": "exposureCategory",       "valueString":  pact_info["exposure"]},
                    {"url": "statutoryAuthority",     "valueString":  "PL 117-168"},
                    {"url": "requiresSCVerification", "valueBoolean": True},
                    {"url": "humanReviewFlag",        "valueBoolean": True},
                ]
            })

        if code in VASRD_MAP:
            vasrd_info = VASRD_MAP[code]
            extensions.append({
                "url": "http://va.gov/fhir/StructureDefinition/vasrd-mapping",
                "extension": [
                    {"url": "vasrdDiagnosticCode", "valueString":  vasrd_info["code"]},
                    {"url": "conditionName",        "valueString":  vasrd_info["name"]},
                    {"url": "maximumRating",        "valueInteger": vasrd_info["max_rating"]},
                    {"url": "regulatoryReference",  "valueString":  "38 CFR Part 4"},
                ]
            })

    resource["extension"] = extensions
    return resource

def enrich_eob(resource, patient_conditions):
    """
    Add MCCF classification, denial risk context, and ensure
    ICD-10 diagnosis codes are present in EOB diagnosis array.

    Synthea EOBs frequently have empty or SNOMED-only diagnosis arrays.
    This function injects the patient's ICD-10 condition codes into
    the EOB diagnosis array so the Streamlit denials page
    get_icd10_category() function can determine the correct
    CMS denial risk profile — producing varied High/Medium/Low
    risk scores rather than everything defaulting to LOW RISK.
    """

    # ── Inject ICD-10 diagnoses if EOB diagnosis array is empty ──
    existing_dx = resource.get("diagnosis", [])
    existing_codes = [
        d.get("diagnosisCodeableConcept", {})
         .get("coding", [{}])[0]
         .get("code", "")
        for d in existing_dx
    ]

    # Check if any existing codes are ICD-10 (alpha first char)
    has_icd10 = any(
        c and c[0].isalpha()
        for c in existing_codes
        if c
    )

    if not has_icd10 and patient_conditions:
        # Inject up to 3 patient ICD-10 conditions into EOB diagnosis
        injected = []
        seen     = set()
        for code in patient_conditions:
            if code and code not in seen and len(injected) < 3:
                injected.append({
                    "sequence": len(existing_dx) + len(injected) + 1,
                    "diagnosisCodeableConcept": {
                        "coding": [{
                            "system":  "http://hl7.org/fhir/sid/icd-10-cm",
                            "code":    code,
                            "display": "Condition from patient record"
                        }]
                    },
                    "type": [{
                        "coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/ex-diagnosistype",
                            "code":   "principal"
                        }]
                    }]
                })
                seen.add(code)

        if injected:
            resource["diagnosis"] = existing_dx + injected
            log.info(f"    Injected {len(injected)} ICD-10 dx codes into EOB")

    # ── MCCF classification ───────────────────────────────────────
    eob_icd_codes = [
        d.get("diagnosisCodeableConcept", {})
         .get("coding", [{}])[0]
         .get("code", "")
        for d in resource.get("diagnosis", [])
    ]

    all_codes = list(set(eob_icd_codes + patient_conditions))
    mccf      = classify_mccf_status(all_codes)

    denial_factors = []
    if mccf["requiresSCReview"]:
        denial_factors.append("PACT Act SC status unverified")
    if any(c in PACT_ACT_CONDITIONS for c in all_codes):
        denial_factors.append("Presumptive condition — billing authority ambiguous")

    extensions = resource.get("extension", [])
    extensions.append({
        "url": "http://va.gov/fhir/StructureDefinition/va-billing-context",
        "extension": [
            {"url": "mccfStatus",          "valueString":  mccf["mccfStatus"]},
            {"url": "billingAuthority",     "valueString":  mccf["billingAuthority"]},
            {"url": "veteranLiability",     "valueBoolean": mccf["veteranLiability"]},
            {"url": "classificationBasis",  "valueString":  mccf["classificationBasis"]},
            {"url": "humanReviewRequired",  "valueBoolean": mccf["humanReviewRequired"]},
            {"url": "denialRiskFactors",    "valueString":  "; ".join(denial_factors) if denial_factors else "None identified"},
            {"url": "modelVersion",         "valueString":  "commence-va-demo-v1.0"},
            {"url": "modelDataSource",      "valueString":  "Synthea synthetic veteran population — no real VA data"},
            {"url": "scoringMethod",        "valueString":  "Simulated — architectural demonstration only"},
        ]
    })

    resource["extension"] = extensions
    return resource

def enrich_bundle(bundle):
    """
    Walk a Synthea FHIR bundle and enrich all relevant resources
    with VA-specific billing, PACT Act, VASRD, and denial risk context.
    """

    # First pass — collect patient ICD-10 condition codes
    patient_conditions = []
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") == "Condition":
            codes = [
                c.get("code", "")
                for c in resource.get("code", {}).get("coding", [])
                if "icd" in c.get("system", "").lower()
            ]
            patient_conditions.extend(codes)

    # Second pass — enrich resources
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        rtype    = resource.get("resourceType")

        if rtype == "Condition":
            entry["resource"] = enrich_condition(resource)
        elif rtype == "ExplanationOfBenefit":
            entry["resource"] = enrich_eob(resource, patient_conditions)

    return bundle

# ── Step 3: Upload With Referential Integrity ──────────────────────────────────

def upload_resource(resource, rtype):
    """
    Upload a single FHIR resource via POST.
    Returns server-assigned ID on success, None on failure.
    """

    url = f"{FHIR_BASE}/{rtype}"

    for attempt in range(1, RETRY_COUNT + 1):
        try:
            response = requests.post(
                url,
                json=resource,
                headers={"Content-Type": "application/fhir+json"},
                timeout=30
            )

            if response.status_code in [200, 201]:
                server_id = response.json().get("id")
                log.info(f"    → {rtype}/{server_id}")
                return server_id
            else:
                log.warning(
                    f"  {rtype} attempt {attempt}/{RETRY_COUNT} "
                    f"HTTP {response.status_code}"
                )
                if attempt < RETRY_COUNT:
                    time.sleep(RETRY_DELAY ** attempt)

        except requests.exceptions.Timeout:
            log.warning(f"  {rtype} timeout attempt {attempt}/{RETRY_COUNT}")
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY ** attempt)

        except requests.exceptions.ConnectionError as e:
            log.error(f"Connection error: {e}")
            return None

    return None

def upload_patients_first(bundle):
    """
    Upload Patient resources first and capture
    Synthea ID → server ID mapping for referential integrity.
    """
    id_map = {}

    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") != "Patient":
            continue

        synthea_id = resource.get("id")
        if not synthea_id:
            continue

        server_id = upload_resource(resource, "Patient")

        if server_id:
            id_map[synthea_id] = server_id
            log.info(f"  Patient {synthea_id} → server ID {server_id}")
        else:
            log.warning(f"  Patient {synthea_id} failed to upload")

        time.sleep(RATE_LIMIT_SEC)

    log.info(f"  Captured {len(id_map)} patient ID mappings")
    return id_map

def rewrite_references(resource, id_map):
    """
    Recursively rewrite Patient references throughout a resource.

    Handles both Synthea reference formats:
      urn:uuid:synthea-id  →  Patient/server-id
      Patient/synthea-id   →  Patient/server-id
    """
    if isinstance(resource, dict):
        for key, value in resource.items():
            if key == "reference" and isinstance(value, str):
                for synthea_id, server_id in id_map.items():
                    if synthea_id in value:
                        if value.startswith("urn:uuid:"):
                            resource[key] = f"Patient/{server_id}"
                        else:
                            resource[key] = value.replace(synthea_id, server_id)
                        break
            else:
                rewrite_references(value, id_map)
    elif isinstance(resource, list):
        for item in resource:
            rewrite_references(item, id_map)
    return resource

def upload_bundle(filepath):
    """
    Load, enrich, and upload relevant resources from a Synthea bundle.

    Filters:
      1. Only Patient, Condition, ExplanationOfBenefit
      2. EOBs only if created after EOB_CUTOFF_DATE

    Upload order:
      1. Patients first — captures ID mapping
      2. Conditions and recent EOBs — with references rewritten
    """

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            bundle = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        log.error(f"Could not read {filepath.name}: {e}")
        return False

    if not bundle.get("entry"):
        log.warning(f"Skipping {filepath.name} — no entries")
        return True

    bundle  = enrich_bundle(bundle)
    entries = bundle.get("entry", [])

    # Check if bundle has any relevant resources
    relevant = [
        e for e in entries
        if e.get("resource", {}).get("resourceType") in UPLOAD_RESOURCE_TYPES
    ]
    if not relevant:
        log.info(f"  Skipping {filepath.name} — no relevant resource types")
        return True

    # Step A — Upload patients first, capture ID mapping
    id_map = upload_patients_first(bundle)

    # Step B — Upload Conditions and recent EOBs
    success = 0
    failed  = 0
    skipped = 0

    for entry in entries:
        resource = entry.get("resource", {})
        rtype    = resource.get("resourceType")

        if not rtype:
            skipped += 1
            continue

        # Skip patients — already uploaded in Step A
        if rtype == "Patient":
            continue

        # Skip resource types not needed by demo pages
        if rtype not in UPLOAD_RESOURCE_TYPES:
            skipped += 1
            continue

        # For EOBs skip anything older than cutoff date
        if rtype == "ExplanationOfBenefit":
            created = resource.get("created", "")
            if created and created < EOB_CUTOFF_DATE:
                skipped += 1
                continue

        # Rewrite patient references — handles urn:uuid and Patient/ formats
        if id_map:
            resource = rewrite_references(resource, id_map)

        server_id = upload_resource(resource, rtype)
        if server_id is not None:
            success += 1
        else:
            failed += 1

        time.sleep(RATE_LIMIT_SEC)

    log.info(
        f"  {filepath.name} — "
        f"{success} uploaded, {failed} failed, {skipped} skipped"
    )
    return failed == 0

def upload_all_bundles():
    """Upload filtered resources from all Synthea FHIR bundle files."""

    output_dir = Path(OUTPUT_PATH)

    if not output_dir.exists():
        log.error(f"Output directory not found: {OUTPUT_PATH}")
        return 0, 0

    fhir_files = sorted(output_dir.glob("*.json"))

    if not fhir_files:
        log.error(f"No FHIR JSON files found in {OUTPUT_PATH}")
        return 0, 0

    log.info(f"Uploading {len(fhir_files)} bundles to {FHIR_BASE}")
    log.info(f"Resource filter  : {', '.join(sorted(UPLOAD_RESOURCE_TYPES))}")
    log.info(f"EOB cutoff date  : {EOB_CUTOFF_DATE} (last 6 months only)")
    log.info(f"ICD-10 injection : Enabled — varied denial risk profiles")
    log.info("-" * 60)

    success = 0
    failed  = 0

    for filepath in fhir_files:
        if upload_bundle(filepath):
            success += 1
        else:
            failed += 1

    return success, failed

# ── Step 4: Verify ─────────────────────────────────────────────────────────────

def verify_upload():
    """
    Confirm resources are accessible and verify
    patient-condition linkage is working correctly.
    """

    log.info("-" * 60)
    log.info("Verifying data accessibility on FHIR server...")

    all_ok     = True
    patient_id = None

    for rtype in ["Patient", "Condition", "ExplanationOfBenefit"]:
        try:
            r = requests.get(
                f"{FHIR_BASE}/{rtype}?_count=1&_summary=count",
                timeout=15
            )
            total  = r.json().get("total", "unknown")
            status = "✓" if isinstance(total, int) and total > 0 else "⚠"
            log.info(f"  {status}  {rtype:<30} {total:>6} records")
            if total == 0:
                all_ok = False

            if rtype == "Patient" and isinstance(total, int) and total > 0:
                pr      = requests.get(f"{FHIR_BASE}/Patient?_count=1", timeout=15)
                entries = pr.json().get("entry", [])
                if entries:
                    patient_id = entries[0].get("resource", {}).get("id")

        except Exception as e:
            log.warning(f"  ✗  {rtype:<30} verification failed — {e}")
            all_ok = False

    # Verify patient-condition linkage
    if patient_id:
        log.info("-" * 60)
        log.info(f"Verifying linkage for Patient/{patient_id}...")
        try:
            cr         = requests.get(
                f"{FHIR_BASE}/Condition",
                params={"patient": patient_id, "_count": 5},
                timeout=15
            )
            cond_count = cr.json().get("total", 0)
            if isinstance(cond_count, int) and cond_count > 0:
                log.info(
                    f"  ✓  Patient/{patient_id} has {cond_count} "
                    f"linked conditions — referential integrity OK"
                )
            else:
                log.warning(
                    f"  ⚠  Patient/{patient_id} returned 0 conditions "
                    f"— referential integrity may be broken"
                )
                all_ok = False
        except Exception as e:
            log.warning(f"  ✗  Linkage check failed — {e}")

    # Verify EOB has diagnosis codes
    log.info("Verifying EOB diagnosis injection...")
    try:
        er      = requests.get(f"{FHIR_BASE}/ExplanationOfBenefit?_count=1", timeout=15)
        entries = er.json().get("entry", [])
        if entries:
            eob  = entries[0].get("resource", {})
            dxs  = eob.get("diagnosis", [])
            if dxs:
                code = dxs[0].get("diagnosisCodeableConcept", {}).get("coding", [{}])[0].get("code", "")
                log.info(f"  ✓  EOB has diagnosis codes — primary: {code} — varied risk profiles active")
            else:
                log.warning("  ⚠  EOB has no diagnosis codes — denials page may default to LOW RISK")
    except Exception as e:
        log.warning(f"  ✗  EOB diagnosis check failed — {e}")

    return all_ok

# ── Main ───────────────────────────────────────────────────────────────────────

def main():

    in_ci = os.environ.get("GITHUB_ACTIONS") == "true"

    log.info("=" * 60)
    log.info("Commence VA RCM Demo — Data Loader")
    log.info(f"Target FHIR server : {FHIR_BASE}")
    log.info(f"Veteran population : {VETERAN_COUNT} records")
    log.info(f"Geography          : {CITY}, {STATE} (VISN 18)")
    log.info(f"Upload method      : Individual POST — patients first")
    log.info(f"Resource filter    : {', '.join(sorted(UPLOAD_RESOURCE_TYPES))}")
    log.info(f"EOB cutoff date    : {EOB_CUTOFF_DATE} (last 6 months)")
    log.info(f"ICD-10 injection   : Enabled — varied denial risk profiles")
    log.info(f"Reference rewrite  : urn:uuid and Patient/ formats handled")
    log.info(f"Environment        : {'GitHub Actions CI' if in_ci else 'Local'}")
    log.info(f"Started            : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 60)

    # Step 1 — Generate
    if not in_ci:
        log.info("Step 1 — Generating Synthea veteran population...")
        if not generate_synthea_data():
            log.error("Synthea generation failed — aborting")
            sys.exit(1)
    else:
        log.info("Step 1 — Skipping generation (run by GitHub Actions workflow)")
        files = list(Path(OUTPUT_PATH).glob("*.json"))
        log.info(f"         Found {len(files)} pre-generated bundle files")

    # Step 2 — Upload
    log.info("Step 2 — Uploading filtered resources to FHIR server...")
    success, failed = upload_all_bundles()

    log.info("-" * 60)
    log.info(f"Upload complete — {success} bundles succeeded, {failed} failed")

    if failed > 0:
        log.warning(f"{failed} bundles had failures — check log for details")

    # Step 3 — Verify
    log.info("Step 3 — Verifying upload and referential integrity...")
    ok = verify_upload()

    log.info("=" * 60)
    if ok and failed == 0:
        log.info("✓ All done — demo data is live with referential integrity confirmed")
    elif ok:
        log.info("⚠ Done with some failures — data partially available")
    else:
        log.info("✗ Verification failed — check log for details")
    log.info(f"Log file: {LOG_FILE}")
    log.info("=" * 60)

    sys.exit(0 if (ok and failed == 0) else 1)


if __name__ == "__main__":
    main()
