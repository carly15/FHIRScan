# FHIRScan — FHIR Dataset Profiler

A Python tool for analysing large-scale FHIR R4 datasets to understand structure, completeness, and value distributions — without requiring full data flattening.

Two ingestion modes are supported:

| Script | Input | Use case |
|--------|-------|----------|
| `profiler_file.py` | Local `.json` / `.ndjson` files | Offline analysis of FHIR exports |
| `profiler_server.py` | FHIR R4 REST server (tested on Blaze) | Live server profiling via `$everything` |

Both scripts share the same statistics, relational analysis, and export logic.

---

## Features

- **Path-based profiling** — analyses nested FHIR structures without flattening to rows
- **Per-resource-type statistics** — keeps each resource type separate
- **FHIR R4B type detection** — two-layer detection (structural fingerprinting + field-name lookup)
- **HyperLogLog cardinality estimation** — memory-efficient unique value counting for high-cardinality fields
- **Extension handling** — inline extension URLs in field paths for MII profile support
- **Relational analysis** — reference integrity, cardinalities, temporal depth, and data quality scoring
- **CSV export** — one file per resource type, easy to open in Excel or pandas
- **Server mode** — connection check, automatic retry with backoff, configurable rate limiting, RAM monitoring

---

## Requirements

- Python 3.8+
- `requests` — required for `profiler_server.py` only (`pip install requests`)

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
```

| Flag | Description | Default |
|------|-------------|---------|
| `--top-n N` | Most frequent values to track per field | 20 |
| `--no-relations` | Skip relational analysis | off |
| `--no-inline-extensions` | Don't flatten extension URLs into paths | off |
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
```

| Flag | Description | Default |
|------|-------------|---------|
| `--token TOKEN` | Bearer token for authentication | none |
| `--limit N` | Stop after N patients (for test runs) | all |
| `--request-delay SEC` | Pause between patients to avoid overloading server | 0 |
| `--max-retries N` | Retry attempts for transient server errors (429, 5xx) | 3 |
| `--page-size N` | Resources per page for pagination | 100 |
| `--top-n N` | Most frequent values to track per field | 20 |
| `--no-relations` | Skip relational analysis | off |
| `--quiet` | Suppress progress output | off |

The server profiler checks the connection via `/metadata` before processing and prints server name, version, and FHIR version. Progress is reported every 10 patients with throughput, ETA, and peak RAM.

---

## Output

One timestamped subfolder per run under `<output_dir>/`, containing:

| File | Content |
|------|---------|
| `<ResourceType>_fields.csv` | Field-level statistics per resource type |
| `_summary.csv` | One row per resource type (counts, field completeness) |
| `_summary_details.csv` | Field-level presence categories (always/sometimes/never) |
| `_cardinality.csv` | Resources per patient / per encounter |
| `_data_quality.csv` | Patient and encounter linkage rates |
| `_reference_integrity.txt` | Orphan reference analysis |
| `_temporal_analysis.txt` | Date ranges and patient record spans |
| `_structural_analysis.txt` | Nesting depth and array size statistics |
| `_errors.txt` | Parse or fetch errors (only if errors occurred) |

---

## Pipeline Overview

```
File mode                          Server mode
─────────────────                  ─────────────────────────────────
Local JSON / NDJSON                GET /Patient  →  patient IDs
       │                                  │
       ▼                                  ▼
Extract resources              GET /Patient/[id]/$everything
       │                                  │
       └──────────────┬───────────────────┘
                      │
                      ▼
           Traverse resource tree
           Generate field paths
                      │
                      ▼
           Accumulate statistics
           (field stats + relational)
                      │
                      ▼
               Export CSVs
```

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

## Relational Analysis Dimensions

1. **Cardinality per Anchor** — resources per Patient, resources per Encounter, Encounters per Patient
2. **Reference Integrity** — orphan resources, missing references, reference target distribution
3. **Temporal Depth** — date ranges per Patient, Encounter duration, time between related events
4. **Structural Depth** — path depth per resource type, array cardinality, extension usage patterns
5. **Completeness Patterns** — field presence rates, data density scoring, sparse vs complete records

---

## Test Data

Developed and tested with the MII FHIR test data from the Medizininformatik-Initiative:
[kerndatensatz-testdaten](https://github.com/medizininformatik-initiative/kerndatensatz-testdaten/tree/master)
