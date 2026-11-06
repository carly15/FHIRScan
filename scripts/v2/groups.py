# Copyright (C) 2026 Carla Schlüter, Bavarian Health Cloud GmbH
# SPDX-License-Identifier: MIT
"""
FHIRScan v2 — grouping of resources (e.g. Observation by category).

Observation, Procedure etc. are used for very different things. Comparing the whole resource type
hides that, so v2 profiles every resource type once per group. The group is read from ONE fixed field
per resource type (SPLIT_FIELDS), the same for every site, so the results stay comparable. The group
VALUES are discovered during the scan: a new site with new categories simply produces new groups.

Rules
-----
- Group key = the codes of the category (all codings of one CodeableConcept, sorted, joined with " + "),
  e.g. "http://loinc.org|26436-6 + http://terminology.hl7.org/CodeSystem/observation-category|laboratory".
- No value in the split field -> group NO_GROUP.
- At most MAX_GROUPS groups per resource type; further ones are collected in OTHER_GROUP.
- A resource with several groups is counted in the first one (sorted). Its other groups are only tallied
  (multi_group). This keeps the type totals exact: the type-level statistics are the merge of its groups.
"""
from collections import Counter
from typing import Any, Dict, List, Optional

# resource type -> field (directly in the resource) whose codes define the group
SPLIT_FIELDS: Dict[str, str] = {
    'Observation': 'category', 'Procedure': 'category', 'Condition': 'category',
    'DiagnosticReport': 'category', 'CarePlan': 'category', 'Consent': 'category',
    'MedicationRequest': 'category', 'MedicationAdministration': 'category', 'ServiceRequest': 'category',
    'Encounter': 'class',
    'List': 'code',
}
MAX_GROUPS = 20
NO_GROUP = '(ohne)'
OTHER_GROUP = '(andere)'
GROUP_SEP = '\x1f'   # joins resource type and group in the relational counters (never appears in codes)


def _coding_key(coding: Any) -> Optional[str]:
    if not isinstance(coding, dict) or not coding.get('code'):
        return None
    # Full system URL: short forms are ambiguous (some systems end in a file name). The report shortens them.
    system = coding.get('system')
    return f"{system}|{coding['code']}" if system else str(coding['code'])


def _concept_key(value: Any) -> Optional[str]:
    """Key of one CodeableConcept (coding list) or one Coding (Encounter.class)."""
    if not isinstance(value, dict):
        return None
    codings = value.get('coding') if isinstance(value.get('coding'), list) else [value]
    keys = sorted({k for k in map(_coding_key, codings) if k})
    if keys:
        return ' + '.join(keys)
    return f"Text: {str(value['text'])[:60]}" if value.get('text') else None


def group_keys(resource: Dict[str, Any], split_field: str) -> List[str]:
    """Sorted group keys of a resource (one per category entry); [NO_GROUP] if it has none."""
    value = resource.get(split_field)
    items = value if isinstance(value, list) else [value]
    keys = sorted({k for k in map(_concept_key, items) if k})
    return keys or [NO_GROUP]


def assign_group(known: Dict[str, int], key: str, max_groups: int = MAX_GROUPS) -> str:
    """Group a resource is counted in. `known` holds the groups of this resource type seen so far."""
    if key == NO_GROUP or key in known:
        return key
    if sum(1 for g in known if g not in (NO_GROUP, OTHER_GROUP)) < max_groups:
        return key
    return OTHER_GROUP


def merge_field_stats(dst, src) -> None:
    """Add the statistics of `src` to `dst` (both FieldStatistics). Counts, numeric statistics and types are
    exact; top values and distinct counts are exact until a field switches to HyperLogLog, then approximate."""
    dst.resource_count += src.resource_count
    dst.value_count += src.value_count
    dst.types_seen.update(src.types_seen)
    dst.fhir_types.update(src.fhir_types)

    if src.numeric_count:                       # parallel Welford (Chan et al.)
        n1, n2 = dst.numeric_count, src.numeric_count
        n = n1 + n2
        delta = src._numeric_mean - dst._numeric_mean
        dst._numeric_M2 += src._numeric_M2 + delta * delta * n1 * n2 / n
        dst._numeric_mean += delta * n2 / n
        dst.numeric_count = n
        dst.numeric_min = src.numeric_min if dst.numeric_min is None else min(dst.numeric_min, src.numeric_min)
        dst.numeric_max = src.numeric_max if dst.numeric_max is None else max(dst.numeric_max, src.numeric_max)

    if dst.hll is None and src.hll is None:
        dst.value_counter.update(src.value_counter)
        dst.exact_values |= src.exact_values
        if len(dst.exact_values) > dst.hll_threshold:
            dst._switch_to_hll()
        return
    if dst.hll is None:                         # dst exact, src approximate
        dst._switch_to_hll()
    if src.hll is not None:
        dst.hll.merge(src.hll)
        extra = Counter(dict(src._hll_sample or []))
    else:
        for value in src.exact_values:
            dst.hll.add(value)
        extra = src.value_counter
    dst._hll_sample = (Counter(dict(dst._hll_sample or [])) + extra).most_common(dst.top_n)


def merge_type_stats(dst, src) -> None:
    """Add the statistics of `src` to `dst` (both ResourceTypeStatistics)."""
    dst.total_resources += src.total_resources
    for path, fs in src.field_stats.items():
        merge_field_stats(dst.get_or_create_field(path), fs)
