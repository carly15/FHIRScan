# FHIRScan — FHIR Dataset Profiler

A Python tool for analysing large-scale FHIR R4 datasets to understand structure, completeness, and value distributions — without requiring full data flattening.

---

## Features

- **Recursive directory scanning** — Finds all `.json` and `.ndjson` files in nested folder structures
- **Bundle-aware parsing** — Extracts resources from FHIR Bundles automatically
- **Path-based profiling** — Analyses nested FHIR structures without flattening to rows
- **Per-resource-type statistics** — Keeps each resource type separate
- **HyperLogLog cardinality estimation** — Memory-efficient unique value counting for high-cardinality fields
- **Extension handling** — Inline extension URLs in field paths for MII profile support
- **Relational analysis** — Reference integrity, cardinalities, temporal depth, and data quality scoring
- **CSV export** — One file per resource type, easy to open in Excel or pandas

---

## Requirements

- Python 3.8+
- No external dependencies (standard library only)

---

## Installation

No installation required. Clone the repo and run directly:

```bash
git clone https://github.com/carly15/FHIRScan.git
cd FHIRScan
```

---

## Usage

```bash
# Basic usage
python main.py <input_dir> <output_dir>

# With options
python main.py /data/mii-fhir-export ./profiles --top-values 50

# Disable relational analysis
python main.py /data/fhir ./output --no-relations

# Quiet mode (no progress output)
python main.py /data/fhir ./output --quiet
```

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `--top-values N` | Number of most frequent values per field | 20 |
| `--no-relations` | Disable relational analysis | off |
| `--quiet` | Suppress progress output | off |

---

## Pipeline Overview

```
┌──────────────┐
│  JSON Files  │
│  (Bundles)   │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   Extract    │
│  Resources   │──────► Group by resourceType
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  Recursive   │
│   Traverse   │──────► Generate field paths
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  Accumulate  │
│  Statistics  │──────► Per path, per resource type
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   Export     │
│   CSVs       │
└──────────────┘
```

---

## Project Structure

```
FHIRScan/
├── main.py                  # Entry point and full profiling logic
├── .gitignore
└── README.md
```

---

## Relational Profiling Dimensions

The relational analysis module covers five dimensions:

1. **Cardinality per Anchor** — Resources per Patient, resources per Encounter, Encounters per Patient
2. **Reference Integrity** — Orphan resources, missing references, reference target distribution
3. **Temporal Depth** — Date ranges per Patient, Encounter duration, time between related events
4. **Structural Depth** — Path depth per resource type, array cardinality, extension usage patterns
5. **Completeness Patterns** — Field co-occurrence, data density scoring, sparse vs complete records
