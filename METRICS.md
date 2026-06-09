# Output Metrics Reference

This document explains every metric produced by the profiler scripts (`profiler_server.py`, `profiler_file.py`). Use it as a companion when reading the CSV output files or the DIZ comparison notebook.

---

## `01_summary.csv` — one row per resource type

High-level counts and field-coverage totals for each FHIR resource type found in the dataset.

| Column | What it means |
|---|---|
| `total_resources` | Raw count of all resources of this type fetched from the server / file |
| `deduplicated_resources` | Resources removed as exact duplicates (same ID seen more than once) |
| `patient_linked_resources` | Resources that contain a direct `subject` / `patient` reference to a Patient resource |
| `unlinked_resources` | Resources with **no** patient reference — potential orphans in the dataset |
| `patient_linkage_rate` | `patient_linked / total` — fraction traceable to a patient (0–1) |
| `avg_resources_per_patient` | Total linked resources ÷ number of unique patients |
| `total_fields` | Number of distinct field paths observed across all resources of this type |
| `fields_always_present` | Fields present in **every** resource of this type (`presence_rate = 1.0`) |
| `fields_sometimes_present` | Fields present in **some but not all** resources (`0 < presence_rate < 1.0`) |
| `fields_with_type_inconsistency` | Fields where different resources stored different data types (e.g. string vs. object) |

---

## `02_summary_details.csv` — one row per field per resource type

Breaks down the summary field counts into individual field paths with presence categories.

| Column | What it means |
|---|---|
| `field_path` | Dot-notation path to the field, e.g. `code.coding.code` |
| `presence_category` | `always_present` / `sometimes_present` / `never_present` |
| `presence_rate` | Fraction of resources that contain this field (see note below) |
| `resources_with_field` | Absolute count of resources containing this field |
| `total_resources` | Total resources of this type (denominator for `presence_rate`) |
| `has_type_inconsistency` | `True` if the same field path held different Python types across resources |
| `detected_python_types` | Comma-separated list of observed Python types (e.g. `str, dict`) |

---

## `{ResourceType}_fields.csv` — one file per resource type, one row per field

The most detailed output. Contains value statistics for every observed field path.

| Column | What it means |
|---|---|
| `field_path` | Dot-notation path to the field |
| `resources_with_field` | Number of resources that contained this field |
| `total_resources` | Total resources of this type |
| `presence_rate` | `resources_with_field / total_resources` (see note below) |
| `missing_rate` | `1 - presence_rate` |
| `value_count` | **Total values seen including duplicates.** For repeated elements (e.g. `coding` arrays) this can exceed `resources_with_field` |
| `unique_count` | Number of **distinct** values observed |
| `unique_count_approximate` | `True` if the distinct count is a HyperLogLog estimate rather than an exact count (triggered above ~1 000 distinct values) |
| `detected_python_types` | Observed Python types for this field |
| `detected_fhir_type` | Inferred FHIR type (e.g. `CodeableConcept`, `Reference`, `Quantity`, `dateTime`) |
| `has_type_inconsistency` | `True` if multiple different types were observed for this field |
| `numeric_count` | Number of values that could be parsed as a number |
| `numeric_min` | Minimum numeric value |
| `numeric_max` | Maximum numeric value |
| `numeric_mean` | Mean of all numeric values |
| `numeric_stdev` | Standard deviation of numeric values |
| `top_values` | Up to 20 most frequent values with occurrence counts, as JSON |

---

## `03_cardinality.csv` — distribution of resources per patient / encounter

Answers: "How many resources of type X does each patient (or encounter) typically have?"

| Column | What it means |
|---|---|
| `resource_type` | The FHIR resource type |
| `anchor_type` | `per_patient` or `per_encounter` |
| `anchors_with_resource` | Number of patients (or encounters) that have **at least one** resource of this type |
| `total_anchors_in_dataset` | Total patients (or encounters) in the dataset |
| `coverage_rate` | `anchors_with_resource / total_anchors` — e.g. `0.72` means 72 % of patients have at least one Condition |
| `total_resources` | Total resource count (same as `01_summary.csv`) |
| `min` | Minimum count seen for any single anchor |
| `max` | Maximum count seen for any single anchor |
| `mean` | Mean count across all anchors that have at least one resource |
| `median` | Median count |
| `std_dev` | Standard deviation |
| `p25, p75, p90, p99` | Percentiles of the count distribution — useful for spotting outliers |

---

## `04_data_quality.csv` — reference linkage health

Checks whether resources are properly linked to patients and encounters.

| Column | What it means |
|---|---|
| `total_resources` | Total resources of this type |
| `with_patient_reference` | Resources that carry a patient reference |
| `without_patient_reference` | Resources with no patient reference |
| `patient_linkage_rate` | `with_patient_reference / total` |
| `with_encounter_reference` | Resources that also reference an Encounter |
| `encounter_linkage_rate` | `with_encounter_reference / total` |

---

## Key distinctions

**`presence_rate` vs. `coverage_rate`**

- `presence_rate` (field-level) answers: "Does this JSON key exist in the resource?" — it measures **structural completeness** of individual fields within a resource.
- `coverage_rate` (cardinality file) answers: "Does this patient have any resource of this type at all?" — it measures **population coverage** across the dataset.

**`value_count` vs. `unique_count`**

- `value_count` counts every occurrence of a value including repeats. For an array field with 3 items in each resource, it will be 3× the resource count.
- `unique_count` is the cardinality of distinct values — relevant for coded fields like `code.coding.code` to see how many different codes are actually used.

**`presence_rate = 1.0` does not mean the value is meaningful**

A field can be present in every resource but still contain a placeholder or default value. Use `top_values` and `unique_count` to assess whether the content is substantively populated.

## License

Copyright (C) 2026 Carla Schlüter, Bavarian Health Cloud GmbH

This project is licensed under the MIT License — see the [LICENSE](../LICENSE) file for details.

For questions about usage or collaboration: carla.schlueter@bavarian-health-cloud.de
