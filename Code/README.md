# FHIRScan — FHIR Dataset Profiler

A Python tool for analysing large-scale FHIR R4 datasets to understand structure, completeness, and value distributions — without requiring full data flattening.

Two ingestion modes are supported:

| Script | Input | Use case |
|--------|-------|----------|
| `profiler_file.py` | Local `.json` / `.ndjson` files | Offline analysis of FHIR exports and bulk data |
| `profiler_server.py` | FHIR R4 REST server (tested on Blaze) | Live server profiling via `$everything` |

Both scripts share the same statistics engine, relational analysis, and export logic.

---

## Choosing a Mode

### When to use `profiler_file.py`

Use the file-based profiler when you have a local FHIR export (e.g. a Synthea dataset, a MII bulk export, or any collection of FHIR JSON/NDJSON bundles). It profiles **everything in the files**, regardless of whether a resource is linked to a patient or not — including `Organization`, `Practitioner`, `Location`, `ValueSet`, `CodeSystem`, and standalone `Medication` entries.

### When to use `profiler_server.py`

Use the server-based profiler when you want to profile a live FHIR server without first exporting files. It fetches resources patient by patient via `GET /Patient/[id]/$everything` and requires no file system access.

### What you lose with the server profiler

The server profiler is **patient-centric by design**. The `$everything` operation only returns resources associated with a specific patient. Resources that exist on the server but are not linked to any patient are invisible:

| Resource type | Visible in server mode? |
|---------------|------------------------|
| Patient | Yes |
| Encounter, Observation, Condition, Procedure, MedicationStatement, etc. (patient-linked) | Yes |
| Organization | **No** |
| Practitioner / PractitionerRole | **No** |
| Location | **No** |
| ValueSet / CodeSystem | **No** |
| Medication (not linked to a patient) | **No** |

Practically, this means:

- You cannot assess the **field completeness of reference targets** — e.g. how complete `Organization` records are, even though patient resources reference them.
- Resources that exist on the server but are **incorrectly unlinked** (e.g. an Encounter with no `subject`) are counted separately in `01_summary.csv` as `unlinked_resources` but are not profiled at the field level.
- The **reference integrity analysis** can detect dangling references *from* patient resources to missing targets, but cannot tell you whether those targets are absent or merely unlinked.

If any of the above matters for your analysis, export the data first and use `profiler_file.py`.

---

## Features

- **Path-based profiling** — traverses nested FHIR structures and generates full field paths (e.g. `Observation.code.coding[].system`)
- **Per-resource-type statistics** — each resource type gets its own output file
- **FHIR R4B type detection** — two-layer strategy: structural dict fingerprinting + normative field-name lookup
- **Numeric statistics** — min, max, mean, and standard deviation for every integer and float field (Welford's online algorithm — no values stored)
- **HyperLogLog cardinality estimation** — memory-efficient unique-value counting; switches automatically at a configurable threshold
- **Extension inlining** — extension URLs are embedded in field paths rather than indexed, supporting MII profiles
- **Relational analysis** — reference integrity, cardinalities per patient/encounter, temporal distribution, structural depth, and data quality scoring
- **Patient linkage tracking** — every resource type is classified as patient-linked or unlinked; `01_summary.csv` reports both counts
- **Deduplication tracking** — shared resources appearing in multiple patient bundles are counted once; the number of skipped duplicates is reported
- **Resource type filter** — exclude specific types (e.g. `Binary`) to save time and memory
- **Write-permission check** — verifies the output directory is writable before starting a long run
- **Timestamped output folders** — each run writes to its own subfolder; optionally name it with `--run-name`
- **Server mode extras** — pre-flight connection check via `/metadata`, automatic retry with exponential backoff, configurable rate limiting, and peak RAM monitoring

---

## Requirements

- Python 3.9+
- `requests` — required for `profiler_server.py` only (`pip install requests`)

> On Python 3.8 (end-of-life), install `backports.zoneinfo` as well (`pip install backports.zoneinfo`).

---

## Installation

```bash
git clone https://github.com/carly15/FHIRScan.git
cd FHIRScan/Code
pip install requests   # only needed for profiler_server.py
```

---

## Usage

### File-based profiler

```bash
# Basic
python profiler_file.py <input_dir> <output_dir>

# With options
python profiler_file.py /data/fhir-export ./output --top-n 50
python profiler_file.py /data/fhir ./output --no-relations --quiet
python profiler_file.py /data/fhir ./output --skip-types Binary --run-name baseline
```

| Flag | Description | Default |
|------|-------------|---------|
| `--top-n N` | Most frequent values to track per field | 20 |
| `--skip-types TYPES` | Comma-separated resource types to exclude (e.g. `Binary,DocumentReference`) | none |
| `--no-relations` | Skip relational analysis | off |
| `--no-inline-extensions` | Don't flatten extension URLs into paths | off |
| `--run-name NAME` | Custom output subfolder name instead of timestamp | timestamp |
| `--quiet` | Suppress progress output | off |

---

### Server-based profiler

```bash
# Basic — profiles all patients on the server
python profiler_server.py <server_url> <output_dir>

# Test run — first 100 patients only, gentle on the server
python profiler_server.py http://localhost:8080/fhir ./output --limit 100 --request-delay 0.5

# With authentication
python profiler_server.py http://myserver/fhir ./output --token <bearer_token>

# Skip large binary resources
python profiler_server.py http://localhost:8080/fhir ./output --skip-types Binary
```

| Flag | Description | Default |
|------|-------------|---------|
| `--token TOKEN` | Bearer token for authentication | none |
| `--limit N` | Stop after N patients (for test runs) | all |
| `--request-delay SEC` | Pause between patients to avoid overloading the server | 0 |
| `--max-retries N` | Retry attempts for transient server errors (429, 5xx) | 3 |
| `--page-size N` | Resources per page for pagination | 100 |
| `--top-n N` | Most frequent values to track per field | 20 |
| `--skip-types TYPES` | Comma-separated resource types to exclude (e.g. `Binary`) | none |
| `--dedup-types TYPES` | Resource types that may appear in multiple patient bundles and need deduplication | `Medication,Location` |
| `--integrity-types TYPES` | Resource types tracked for reference integrity checks but not deduplicated (common reference targets) | `Patient,Encounter,Practitioner,…` |
| `--no-relations` | Skip relational analysis | off |
| `--run-name NAME` | Custom output subfolder name instead of timestamp | timestamp |
| `--quiet` | Suppress progress output | off |

The server profiler verifies the connection via `/metadata` before processing and prints server name and FHIR version. Progress is reported every 500 patients with throughput, ETA, and peak RAM usage.

---

## Output

Each run writes to its own subfolder under `<output_dir>/` (named by timestamp or `--run-name`):

| File | Content |
|------|---------|
| `01_summary.csv` | One row per resource type — counts, linkage, averages, field completeness |
| `02_summary_details.csv` | One row per field per resource type — presence category and type consistency |
| `03_cardinality.csv` | Distribution of resources per patient and per encounter (min/max/mean/percentiles) |
| `04_data_quality.csv` | Patient and encounter linkage rates per resource type |
| `05_reference_integrity.txt` | Orphan reference analysis — dangling references by source and target type |
| `06_temporal_analysis.txt` | Date ranges per resource type and patient record span statistics |
| `07_structural_analysis.txt` | Nesting depth and array size statistics per resource type |
| `<ResourceType>_fields.csv` | Field-level statistics per resource type — see column details below |
| `99_errors.txt` | Parse or fetch errors (only written if errors occurred) |

### `<ResourceType>_fields.csv` columns

| Column | Description |
|--------|-------------|
| `field_path` | Full dot-notation path (e.g. `Observation.code.coding[].system`) |
| `resources_with_field` | How many resources contained this field at least once |
| `total_resources` | Total resources of this type |
| `presence_rate` | `resources_with_field / total_resources` |
| `missing_rate` | `1 - presence_rate` |
| `value_count` | Total values seen (including repeated occurrences) |
| `unique_count` | Distinct values (exact below threshold, HLL estimate above) |
| `unique_count_approximate` | `True` if HyperLogLog estimate was used |
| `detected_python_types` | Python types observed (string, integer, boolean, …) |
| `detected_fhir_type` | Most frequent FHIR R4B datatype (CodeableConcept, Reference, Period, …) |
| `type_inconsistency` | `True` if more than one non-null Python type was seen for this field |
| `numeric_count` | Number of numeric values seen (blank for non-numeric fields) |
| `numeric_min` | Minimum value |
| `numeric_max` | Maximum value |
| `numeric_mean` | Mean (Welford's online algorithm) |
| `numeric_stdev` | Sample standard deviation |
| `top_values` | JSON array of `[value, count]` pairs for the most frequent values; for high-cardinality fields (unique values > HLL threshold) a sample from the first observed values is shown instead |

### `01_summary.csv` columns

| Column | Description |
|--------|-------------|
| `resource_type` | FHIR resource type name |
| `total_resources` | All resources of this type (after deduplication) |
| `deduplicated_resources` | Resources skipped because the same (type, id) was already seen in another bundle |
| `patient_linked_resources` | Resources with a direct reference to a Patient (Patient type itself counts as fully linked) |
| `unlinked_resources` | `total - linked` — these exist in the data but have no patient association |
| `patient_linkage_rate` | `linked / total` |
| `avg_resources_per_patient` | `linked / unique_patients` — based on linked resources only |
| `total_fields` | Number of distinct field paths observed |
| `fields_always_present` | Fields present in every resource of this type |
| `fields_sometimes_present` | Fields present in some but not all resources |
| `fields_with_type_inconsistency` | Fields where more than one non-null Python type was observed |

---

## Pipeline Overview

```
File mode                          Server mode
─────────────────                  ─────────────────────────────────
Local JSON / NDJSON                GET /Patient  →  patient IDs
       │                                  │
       ▼                                  ▼
Extract resources              GET /Patient/[id]/$everything
(all types, incl. standalone)    (patient-linked resources only)
       │                                  │
       └──────────────┬───────────────────┘
                      │
                      ▼
           Deduplicate by (resourceType, id)
           Skip configured --skip-types
                      │
                      ▼
           Traverse resource tree
           Generate field paths
                      │
                      ▼
           Accumulate statistics
           · field presence + types
           · numeric min/max/mean/stdev
           · HyperLogLog cardinality
           · relational linkage
                      │
                      ▼
               Export CSVs
```

---

## Relational Analysis Dimensions

1. **Cardinality per Anchor** — resources per Patient, resources per Encounter, Encounters per Patient; only patient-linked resources are counted
2. **Reference Integrity** — orphan references, reference target distribution, unknown target types
3. **Temporal Depth** — date ranges per resource type, patient record span (earliest to latest dated event)
4. **Structural Depth** — path nesting depth per resource type, array cardinality statistics
5. **Completeness Patterns** — field presence rates and type consistency per field path

---

## Project Structure

```
FHIRScan/
├── Code/
│   ├── profiler_file.py      # File-based profiler (JSON / NDJSON)
│   ├── profiler_server.py    # Server-based profiler (FHIR REST API)
│   ├── profiler_legacy.py    # Deprecated — use profiler_file.py
│   ├── diz_comparison.ipynb  # DIZ comparison notebook
│   └── README.md
└── .gitignore
```

---

## Test Data

Developed and tested with the MII FHIR test data from the Medizininformatik-Initiative:
[kerndatensatz-testdaten](https://github.com/medizininformatik-initiative/kerndatensatz-testdaten/tree/master)
