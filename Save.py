#!/usr/bin/env python3
"""
FHIR Dataset Profiler - Single File Version

A tool for analysing large-scale FHIR R4 datasets to understand
structure, completeness, and value distributions.

Usage:
    python fhir_profiler.py /path/to/fhir/data /path/to/output
"""

import sys
import os
import re
import time
import json
import csv
import math
import hashlib
import argparse
from pathlib import Path
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, List, Optional, Set, Tuple


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class ProfilerConfig:
    """Configuration for the FHIR dataset profiler."""

    input_dir: Path
    output_dir: Path
    top_values_limit: int = 20
    max_value_length: int = 200
    hll_precision: int = 14
    hll_threshold: int = 10000
    inline_extensions: bool = True
    file_patterns: tuple = ("*.json", "*.ndjson")
    verbose: bool = True

    def __post_init__(self):
        self.input_dir = Path(self.input_dir)
        self.output_dir = Path(self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)


# =============================================================================
# UTILITIES
# =============================================================================

def safe_json_load(file_path: Path) -> Generator[dict, None, None]:
    """Load JSON or NDJSON file, yielding parsed objects."""
    with open(file_path, 'r', encoding='utf-8') as f:
        if file_path.suffix.lower() == '.ndjson':
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError as e:
                        print(f"Warning: Invalid JSON at {file_path}:{line_num}: {e}")
        else:
            try:
                data = json.load(f)
                if isinstance(data, list):
                    yield from data
                else:
                    yield data
            except json.JSONDecodeError as e:
                print(f"Warning: Invalid JSON in {file_path}: {e}")


def get_python_type(value: Any) -> str:
    """Get a readable type name for a value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


# =============================================================================
# FHIR R4B TYPE DETECTION
# =============================================================================
# Two-layer strategy:
#   Layer 1: Structural fingerprinting of the actual JSON value (bool/float/int/dict/str)
#   Layer 2: Field-name lookup — FHIR field names are normative per the R4B spec
# Reference: https://hl7.org/fhir/R4B/datatypes.html

# Compiled regex patterns for narrowing JSON string values to FHIR primitives
_RE_INSTANT  = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$')
_RE_DATETIME = re.compile(r'^\d{4}(?:-\d{2}(?:-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?)?)?$')
_RE_DATE     = re.compile(r'^\d{4}(?:-\d{2}(?:-\d{2})?)?$')
_RE_TIME     = re.compile(r'^\d{2}:\d{2}:\d{2}(?:\.\d+)?$')
_RE_OID      = re.compile(r'^urn:oid:[\d.]+$')
_RE_UUID     = re.compile(r'^urn:uuid:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
_RE_URI      = re.compile(r'^[a-zA-Z][a-zA-Z0-9+\-.]*:')
_RE_BASE64   = re.compile(r'^[A-Za-z0-9+/]*={0,2}$')
_RE_ID       = re.compile(r'^[A-Za-z0-9\-\.]{1,64}$')

# ContactPoint.system values (FHIR bound ValueSet — R4B §4.6.4)
_CONTACT_POINT_SYSTEMS: Set[str] = {'phone', 'fax', 'email', 'pager', 'url', 'sms', 'other'}

# Field names that reliably carry base64Binary data (Attachment.data, Attachment.hash)
_BASE64_FIELD_NAMES: Set[str] = {'data', 'hash'}

# Field names that reliably carry markdown content
_MARKDOWN_FIELD_NAMES: Set[str] = {'requirements', 'copyright', 'purpose', 'comment', 'description'}

# Direct mapping of FHIR R4B field names to their declared complex datatype.
# Field names are normative in the spec — more reliable than inspecting object keys.
_FHIR_FIELD_TYPES: Dict[str, str] = {
    'name':                 'HumanName',
    'address':              'Address',
    'telecom':              'ContactPoint',
    'identifier':           'Identifier',
    'period':               'Period',
    'effectivePeriod':      'Period',
    'billablePeriod':       'Period',
    'requestedPeriod':      'Period',
    'code':                 'CodeableConcept',
    'category':             'CodeableConcept',
    'type':                 'CodeableConcept',
    'reasonCode':           'CodeableConcept',
    'bodySite':             'CodeableConcept',
    'site':                 'CodeableConcept',
    'route':                'CodeableConcept',
    'method':               'CodeableConcept',
    'severity':             'CodeableConcept',
    'criticality':          'CodeableConcept',
    'interpretation':       'CodeableConcept',
    'dataAbsentReason':     'CodeableConcept',
    'admitSource':          'CodeableConcept',
    'dischargeDisposition': 'CodeableConcept',
    'specialArrangement':   'CodeableConcept',
    'specialCourtesy':      'CodeableConcept',
    'priority':             'CodeableConcept',
    'relationship':         'CodeableConcept',
    'complication':         'CodeableConcept',
    'outcome':              'CodeableConcept',
    'coding':               'Coding',
    'class':                'Coding',
    'subject':              'Reference',
    'patient':              'Reference',
    'encounter':            'Reference',
    'author':               'Reference',
    'recorder':             'Reference',
    'asserter':             'Reference',
    'requester':            'Reference',
    'performer':            'Reference',
    'location':             'Reference',
    'partOf':               'Reference',
    'basedOn':              'Reference',
    'focus':                'Reference',
    'hasMember':            'Reference',
    'derivedFrom':          'Reference',
    'managingOrganization': 'Reference',
    'generalPractitioner':  'Reference',
    'serviceProvider':      'Reference',
    'payor':                'Reference',
    'insurer':              'Reference',
    'quantity':             'Quantity',
    'doseQuantity':         'Quantity',
    'age':                  'Age',
    'duration':             'Duration',
    'note':                 'Annotation',
    'photo':                'Attachment',
    'timing':               'Timing',
    'net':                  'Money',
    'unitPrice':            'Money',
    'amount':               'Money',
    'signature':            'Signature',
    'text':                 'Narrative',
    'meta':                 'Meta',
}

# FHIR type name suffixes used in value[x] polymorphic fields.
# Ordered longest-first so 'CodeableConcept' is checked before shorter suffixes.
_FHIR_TYPE_SUFFIXES: List[Tuple[str, str]] = [
    ('CodeableConcept', 'CodeableConcept'),
    ('SampledData',     'SampledData'),
    ('ContactPoint',    'ContactPoint'),
    ('HumanName',       'HumanName'),
    ('Identifier',      'Identifier'),
    ('Attachment',      'Attachment'),
    ('Annotation',      'Annotation'),
    ('Reference',       'Reference'),
    ('Quantity',        'Quantity'),
    ('Duration',        'Duration'),
    ('Distance',        'Distance'),
    ('Signature',       'Signature'),
    ('Timing',          'Timing'),
    ('Address',         'Address'),
    ('Period',          'Period'),
    ('Range',           'Range'),
    ('Ratio',           'Ratio'),
    ('Money',           'Money'),
    ('Count',           'Count'),
    ('Age',             'Age'),
]


def _fingerprint_dict(d: dict) -> Optional[str]:
    """
    Layer 1a: Identify a FHIR complex type by the keys present in a dict value.

    Key combinations that uniquely fingerprint each type are checked in order
    from most-distinctive to least-distinctive to avoid false positives.
    Returns the FHIR type name, or None if no fingerprint matches conclusively.
    """
    keys = d.keys()

    # Reference: 'reference' string key is unique to this type
    if 'reference' in keys:
        return 'Reference'

    # CodeableConcept: always has a 'coding' sub-array
    if 'coding' in keys:
        return 'CodeableConcept'

    # Ratio: numerator + denominator (both Quantity-like objects)
    if 'numerator' in keys or 'denominator' in keys:
        return 'Ratio'

    # SampledData: origin (Quantity) + period (decimal) + data (string) — unique triple
    if 'origin' in keys and 'period' in keys and 'data' in keys:
        return 'SampledData'

    # Signature: type (Coding[]) + when (instant) + who (Reference)
    if 'type' in keys and 'when' in keys and 'who' in keys:
        return 'Signature'

    # Narrative: 'div' key is unique to Narrative
    if 'div' in keys:
        return 'Narrative'

    # Meta: versionId and lastUpdated are unique meta fields
    if 'versionId' in keys or 'lastUpdated' in keys:
        return 'Meta'

    # Period: start and/or end (dateTime strings); exclude Range keys to avoid collision
    if ('start' in keys or 'end' in keys) and 'low' not in keys and 'high' not in keys:
        return 'Period'

    # Range: low and/or high (SimpleQuantity objects)
    if 'low' in keys or 'high' in keys:
        return 'Range'

    # Coding: system + code, no 'coding' sub-key, no numeric 'value'
    if 'system' in keys and 'code' in keys and 'value' not in keys:
        return 'Coding'

    # Quantity and constrained types (Age, Duration, Count, Distance, Money):
    # numeric 'value' is the key differentiator from ContactPoint/Identifier
    if 'value' in keys and isinstance(d.get('value'), (int, float)):
        if 'currency' in keys:
            return 'Money'
        return 'Quantity'  # Age/Duration/Count/Distance share same keys; field name resolves them (Layer 2)

    # Annotation: text + time or author field
    if 'text' in keys and ('time' in keys or 'authorReference' in keys or 'authorString' in keys):
        return 'Annotation'

    # Attachment: contentType is unique; url+size also works
    if 'contentType' in keys or ('url' in keys and 'size' in keys):
        return 'Attachment'

    # Timing: 'repeat' BackboneElement is distinctive
    if 'repeat' in keys:
        return 'Timing'

    # HumanName: family and given are unique to HumanName
    if 'family' in keys or 'given' in keys:
        return 'HumanName'

    # Address: city / postalCode / country / line are unique to Address
    if 'city' in keys or 'postalCode' in keys or 'country' in keys or 'line' in keys:
        return 'Address'

    # ContactPoint vs Identifier: both have system + value (string)
    # Distinguish by checking system value against the ContactPoint bound ValueSet
    if 'system' in keys and 'value' in keys and isinstance(d.get('value'), str):
        if d.get('system') in _CONTACT_POINT_SYSTEMS:
            return 'ContactPoint'
        return 'Identifier'

    return None


def _narrow_string(s: str, field_name: str) -> str:
    """
    Layer 1b: Narrow a JSON string to the most specific FHIR primitive type.

    Checks in order of decreasing specificity so a more-specific pattern
    (e.g. instant) always takes precedence over a broader one (e.g. dateTime).
    """
    # xhtml: Narrative.div always starts with an XML tag
    if field_name == 'div' and s.startswith('<'):
        return 'xhtml'

    # base64Binary: field name is the most reliable signal
    if field_name in _BASE64_FIELD_NAMES and _RE_BASE64.fullmatch(s):
        return 'base64Binary'

    # uuid (urn:uuid:...) — must precede the generic uri check
    if _RE_UUID.fullmatch(s):
        return 'uuid'

    # oid (urn:oid:...) — must precede the generic uri check
    if _RE_OID.fullmatch(s):
        return 'oid'

    # instant: full datetime with mandatory timezone — most specific datetime form
    if _RE_INSTANT.fullmatch(s):
        return 'instant'

    # dateTime: has a time component (T separator) but no/optional timezone
    if 'T' in s and _RE_DATETIME.fullmatch(s):
        return 'dateTime'

    # date: date-only form (YYYY, YYYY-MM, YYYY-MM-DD) — no time component
    if _RE_DATE.fullmatch(s):
        return 'date'

    # time: HH:MM:SS[.sss]
    if _RE_TIME.fullmatch(s):
        return 'time'

    # canonical: versioned URL (url|version) — must precede the generic uri check
    if '|' in s and _RE_URI.match(s.split('|')[0]):
        return 'canonical'

    # uri / url: any scheme-based URI
    if _RE_URI.match(s):
        return 'uri'

    # markdown: field name signals intent; markdown syntax in value confirms it
    if field_name in _MARKDOWN_FIELD_NAMES and any(m in s for m in ('**', '\n#', '\n-', '\n*')):
        return 'markdown'

    # id: FHIR id type — only for field names that semantically carry an id value
    # (id regex overlaps with code; field name is the reliable disambiguator)
    if field_name in ('id', 'versionId', 'linkId', 'groupingId') and _RE_ID.fullmatch(s):
        return 'id'

    # code: no whitespace, reasonably short (catches FHIR code values like 'active', 'male')
    if ' ' not in s and len(s) <= 100:
        return 'code'

    return 'string'


def detect_fhir_type(value: Any, field_name: str) -> str:
    """
    Return the most specific FHIR R4B datatype name for a field value.

    Two-layer strategy:
    Layer 1 — Structural fingerprinting of the value itself:
        bool   → 'boolean'
        float  → 'decimal'
        int    → 'integer' (or 'positiveInt'/'unsignedInt' by field name)
        dict   → fingerprint key set → complex FHIR type, or fall through to Layer 2
        str    → regex narrowing → specific FHIR primitive

    Layer 2 — Field-name lookup (fallback when structure is ambiguous):
        Direct dict lookup in _FHIR_FIELD_TYPES
        value[x] suffix matching in _FHIR_TYPE_SUFFIXES

    Always returns a non-empty string; returns 'unknown' if nothing matches.
    """
    # Note: bool must be checked before int — bool is a subclass of int in Python
    if isinstance(value, bool):
        return 'boolean'

    if isinstance(value, float):
        return 'decimal'

    if isinstance(value, int):
        # Disambiguate integer subtypes by field-name convention
        if field_name in ('count', 'frequency', 'frequencyMax', 'durationMax', 'periodMax'):
            return 'positiveInt'
        if field_name in ('offset',):
            return 'unsignedInt'
        return 'integer'

    if isinstance(value, dict):
        result = _fingerprint_dict(value)
        if result:
            return result
        # Fall through to Layer 2 when fingerprint is inconclusive

    if isinstance(value, str):
        return _narrow_string(value, field_name)

    # Layer 2: field-name lookup
    if field_name in _FHIR_FIELD_TYPES:
        return _FHIR_FIELD_TYPES[field_name]

    for suffix, fhir_type in _FHIR_TYPE_SUFFIXES:
        if field_name.endswith(suffix) and len(field_name) > len(suffix):
            return fhir_type

    return 'unknown'


# =============================================================================
# HYPERLOGLOG IMPLEMENTATION
# =============================================================================

class HyperLogLog:
    """
    HyperLogLog cardinality estimator.

    Provides approximate unique counting with configurable precision.
    Memory usage: 2^p bytes
    Standard error: 1.04 / sqrt(2^p)
    """

    def __init__(self, precision: int = 14):
        if not 4 <= precision <= 16:
            raise ValueError("Precision must be between 4 and 16")

        self.p = precision          # bits used to select which register to update
        self.m = 1 << precision     # total number of registers (2^p)
        self.registers = bytearray(self.m)  # each register holds the max leading-zero rank seen for its bucket

        # alpha: bias-correction constant; exact values for small m, formula for larger m
        if self.m == 16:
            self.alpha = 0.673
        elif self.m == 32:
            self.alpha = 0.697
        elif self.m == 64:
            self.alpha = 0.709
        else:
            self.alpha = 0.7213 / (1 + 1.079 / self.m)

    def _hash(self, value: Any) -> int:
        # Produce a 64-bit integer hash from any value via SHA-256 (first 8 bytes)
        str_repr = str(value).encode('utf-8')
        hash_bytes = hashlib.sha256(str_repr).digest()[:8]
        return int.from_bytes(hash_bytes, 'big')

    def _leading_zeros(self, value: int, max_bits: int = 64) -> int:
        # Count leading zero bits within a max_bits-wide integer
        if value == 0:
            return max_bits
        return max_bits - value.bit_length()

    def add(self, value: Any) -> None:
        h = self._hash(value)
        register_idx = h >> (64 - self.p)              # top p bits → which register to update
        remaining = h & ((1 << (64 - self.p)) - 1)    # remaining bits used for rank computation
        rank = self._leading_zeros(remaining, 64 - self.p) + 1  # rank = leading zeros + 1
        self.registers[register_idx] = max(self.registers[register_idx], rank)  # keep max rank per register

    def count(self) -> int:
        # Core HLL estimate: harmonic mean of 2^(-register) scaled by alpha and m^2
        indicator = sum(2.0 ** (-r) for r in self.registers)
        estimate = self.alpha * self.m * self.m / indicator

        # Small range correction: switch to linear counting when many registers are still empty
        if estimate <= 2.5 * self.m:
            zeros = self.registers.count(0)  # registers that have never been updated
            if zeros > 0:
                estimate = self.m * math.log(self.m / zeros)
        # Large range correction: compensate for 64-bit hash space saturation
        elif estimate > (1 << 32) / 30:
            estimate = -(1 << 64) * math.log(1 - estimate / (1 << 64))

        return int(estimate)

    def merge(self, other: 'HyperLogLog') -> None:
        if self.p != other.p:
            raise ValueError("Cannot merge HyperLogLogs with different precision")
        # Take the element-wise max of both register arrays (equivalent to a union)
        for i in range(self.m):
            self.registers[i] = max(self.registers[i], other.registers[i])


# =============================================================================
# STATISTICS CLASSES
# =============================================================================

@dataclass
class FieldStatistics:
    """Statistics for a single field path."""

    path: str
    resource_count: int = 0           # how many resources contained this field (presence count)
    value_count: int = 0              # total values seen, including duplicates
    types_seen: Counter = field(default_factory=Counter)         # frequency of each Python type (string, integer, etc.)
    exact_values: Optional[Set[str]] = field(default_factory=set)  # distinct values stored exactly (before HLL threshold)
    value_counter: Counter = field(default_factory=Counter)      # frequency of each value string (for top-N output)
    hll: Optional[HyperLogLog] = None      # approximate unique counter, activated after threshold is exceeded
    hll_precision: int = 14
    hll_threshold: int = 10000
    top_n: int = 20
    max_value_length: int = 200
    using_hll: bool = False                # True once we've switched from exact set to HLL
    fhir_types: Counter = field(default_factory=Counter)  # detected FHIR complex type(s) when field is an object

    def add_value(self, value: Any, python_type: str) -> None:
        self.value_count += 1
        self.types_seen[python_type] += 1

        # Arrays and objects are counted for type tracking but their contents aren't stored as values
        if python_type in ('array', 'object'):
            return

        str_value = str(value)
        # Truncate very long values to avoid excessive memory use
        if len(str_value) > self.max_value_length:
            str_value = str_value[:self.max_value_length - 3] + "..."

        self.value_counter[str_value] += 1  # for top-N most frequent values

        if self.using_hll:
            self.hll.add(str_value)
        else:
            self.exact_values.add(str_value)
            # Once distinct values exceed the threshold, switch to approximate counting
            if len(self.exact_values) > self.hll_threshold:
                self._switch_to_hll()

    def _switch_to_hll(self) -> None:
        # Migrate all exact values into a HyperLogLog, then free the set
        self.hll = HyperLogLog(self.hll_precision)
        for val in self.exact_values:
            self.hll.add(val)
        self.exact_values = None  # release memory
        self.using_hll = True

    def mark_resource_presence(self) -> None:
        self.resource_count += 1

    @property
    def unique_count(self) -> int:
        if self.using_hll:
            return self.hll.count()
        return len(self.exact_values) if self.exact_values else 0

    @property
    def unique_count_approximate(self) -> bool:
        return self.using_hll

    @property
    def top_values(self) -> List[tuple]:
        return self.value_counter.most_common(self.top_n)

    @property
    def detected_python_types(self) -> List[str]:
        return [t for t, _ in self.types_seen.most_common()]

    @property
    def has_type_inconsistency(self) -> bool:
        non_null_types = [t for t in self.types_seen if t != 'null']
        return len(non_null_types) > 1

    @property
    def detected_fhir_type(self) -> str:
        """Most frequently detected FHIR complex datatype, or empty string if none."""
        if not self.fhir_types:
            return ''
        return self.fhir_types.most_common(1)[0][0]

    def merge(self, other: 'FieldStatistics') -> None:
        self.resource_count += other.resource_count
        self.value_count += other.value_count
        self.types_seen.update(other.types_seen)
        self.value_counter.update(other.value_counter)

        if self.using_hll and other.using_hll:
            self.hll.merge(other.hll)
        elif self.using_hll and not other.using_hll:
            for val in other.exact_values or []:
                self.hll.add(val)
        elif not self.using_hll and other.using_hll:
            self._switch_to_hll()
            self.hll.merge(other.hll)
        else:
            self.exact_values.update(other.exact_values or set())
            if len(self.exact_values) > self.hll_threshold:
                self._switch_to_hll()


@dataclass
class ResourceTypeStatistics:
    """Statistics for all fields within a resource type."""

    resource_type: str
    total_resources: int = 0
    field_stats: Dict[str, FieldStatistics] = field(default_factory=dict)
    hll_precision: int = 14
    hll_threshold: int = 10000
    top_n: int = 20
    max_value_length: int = 200

    def get_or_create_field(self, path: str) -> FieldStatistics:
        if path not in self.field_stats:
            self.field_stats[path] = FieldStatistics(
                path=path,
                hll_precision=self.hll_precision,
                hll_threshold=self.hll_threshold,
                top_n=self.top_n,
                max_value_length=self.max_value_length
            )
        return self.field_stats[path]

    def increment_resource_count(self) -> None:
        self.total_resources += 1

    def merge(self, other: 'ResourceTypeStatistics') -> None:
        self.total_resources += other.total_resources
        for path, other_field in other.field_stats.items():
            if path in self.field_stats:
                self.field_stats[path].merge(other_field)
            else:
                self.field_stats[path] = other_field


# =============================================================================
# FILE SCANNING
# =============================================================================

@dataclass
class ScanResult:
    """Result of directory scanning."""
    files: List[Path]
    total_size_bytes: int
    scan_errors: List[str]


def scan_directory(
        root_dir: Path,
        patterns: tuple = ("*.json", "*.ndjson"),
        verbose: bool = True
) -> ScanResult:
    """Recursively scan directory for JSON/NDJSON files."""
    files = []
    total_size = 0
    errors = []

    root_dir = Path(root_dir)

    if not root_dir.exists():
        errors.append(f"Directory does not exist: {root_dir}")
        return ScanResult(files=[], total_size_bytes=0, scan_errors=errors)

    if verbose:
        print(f"Scanning directory: {root_dir}")

    for pattern in patterns:
        for file_path in root_dir.rglob(pattern):
            try:
                if file_path.is_file():
                    files.append(file_path)
                    total_size += file_path.stat().st_size
            except (PermissionError, OSError) as e:
                errors.append(f"Cannot access {file_path}: {e}")

    files.sort()

    if verbose:
        size_mb = total_size / (1024 * 1024)
        print(f"Found {len(files)} files ({size_mb:.2f} MB)")
        if errors:
            print(f"Scan errors: {len(errors)}")

    return ScanResult(files=files, total_size_bytes=total_size, scan_errors=errors)


# =============================================================================
# BUNDLE PARSING
# =============================================================================

def extract_resources_from_bundle(bundle: Dict[str, Any]) -> Generator[Dict[str, Any], None, None]:
    """Extract resources from a FHIR Bundle."""
    resource_type = bundle.get('resourceType')

    if resource_type == 'Bundle':
        entries = bundle.get('entry', [])
        for entry in entries:
            resource = entry.get('resource')
            if resource and isinstance(resource, dict):
                yield resource
    elif resource_type:
        yield bundle


def extract_resources_from_file(file_path: Path) -> Tuple[List[Dict], int, List[str]]:
    """Extract all resources from a JSON/NDJSON file."""
    resources = []
    bundle_count = 0
    errors = []

    try:
        for obj in safe_json_load(file_path):
            if not isinstance(obj, dict):
                errors.append(f"Non-object JSON element in {file_path}")
                continue

            resource_type = obj.get('resourceType')
            if resource_type == 'Bundle':
                bundle_count += 1

            for resource in extract_resources_from_bundle(obj):
                resources.append(resource)

    except Exception as e:
        errors.append(f"Error processing {file_path}: {e}")

    return resources, bundle_count, errors


# =============================================================================
# PATH TRAVERSAL
# =============================================================================

def traverse_resource(
        resource: Dict[str, Any],
        inline_extensions: bool = True,
        max_depth: int = 50
) -> Generator[Tuple[str, Any, str, bool], None, None]:
    """
    Recursively traverse a FHIR resource and yield (path, value, type, is_first) tuples.
    """
    resource_type = resource.get('resourceType', 'Unknown')
    # Tracks every path seen in this resource; used to emit is_first=True only on first encounter
    paths_in_resource: Set[str] = set()

    def _traverse(obj: Any, current_path: str, depth: int = 0):
        if depth > max_depth:
            return

        python_type = get_python_type(obj)

        if isinstance(obj, dict):
            # Yield the object itself before recursing into its fields
            yield (current_path, obj, 'object', current_path not in paths_in_resource)
            paths_in_resource.add(current_path)

            # Flatten extensions: use their URL as part of the path instead of a numeric index
            if inline_extensions and 'extension' in obj:
                for ext in obj.get('extension', []):
                    if isinstance(ext, dict) and 'url' in ext:
                        url = ext['url']
                        ext_path = f"{current_path}.extension[{url}]" if current_path else f"extension[{url}]"

                        for key, value in ext.items():
                            if key != 'url':
                                child_path = f"{ext_path}.{key}"
                                yield from _traverse(value, child_path, depth + 1)

            for key, value in obj.items():
                if inline_extensions and key == 'extension':
                    continue  # already handled above

                child_path = f"{current_path}.{key}" if current_path else key
                yield from _traverse(value, child_path, depth + 1)

        elif isinstance(obj, list):
            # All array items share the same path with a '[]' suffix (type-agnostic)
            array_path = current_path + '[]'
            yield (array_path, obj, 'array', array_path not in paths_in_resource)
            paths_in_resource.add(array_path)

            for item in obj:
                yield from _traverse(item, array_path, depth + 1)

        else:
            # Scalar value (string, number, boolean, null)
            is_first = current_path not in paths_in_resource  # True only on first occurrence in this resource
            paths_in_resource.add(current_path)
            yield (current_path, obj, python_type, is_first)

    yield from _traverse(resource, resource_type)


# =============================================================================
# AGGREGATOR
# =============================================================================

@dataclass
class AggregationState:
    """Current state of aggregation across all files."""
    resource_types: Dict[str, ResourceTypeStatistics] = field(default_factory=dict)
    files_processed: int = 0
    total_resources: int = 0
    errors: List[str] = field(default_factory=list)
    config: Optional[ProfilerConfig] = None

    def get_or_create_resource_type(self, resource_type: str) -> ResourceTypeStatistics:
        if resource_type not in self.resource_types:
            self.resource_types[resource_type] = ResourceTypeStatistics(
                resource_type=resource_type,
                hll_precision=self.config.hll_precision if self.config else 14,
                hll_threshold=self.config.hll_threshold if self.config else 10000,
                top_n=self.config.top_values_limit if self.config else 20,
                max_value_length=self.config.max_value_length if self.config else 200
            )
        return self.resource_types[resource_type]


class Aggregator:
    """Main aggregation engine for FHIR dataset profiling."""

    def __init__(self, config: ProfilerConfig):
        self.config = config
        self.state = AggregationState(config=config)

    def process_resource(self, resource: Dict[str, Any]) -> None:
        resource_type = resource.get('resourceType')

        if not resource_type:
            self.state.errors.append("Resource without resourceType encountered")
            return

        type_stats = self.state.get_or_create_resource_type(resource_type)
        type_stats.increment_resource_count()
        self.state.total_resources += 1

        paths_seen_in_resource = set()

        for path, value, python_type, is_first_in_resource in traverse_resource(
                resource,
                inline_extensions=self.config.inline_extensions
        ):
            field_stats = type_stats.get_or_create_field(path)

            if is_first_in_resource and path not in paths_seen_in_resource:
                field_stats.mark_resource_presence()
                paths_seen_in_resource.add(path)

            # Extract the bare field name from the path for FHIR type detection.
            # e.g. 'Patient.name[]' → 'name', 'Observation.valueQuantity' → 'valueQuantity'
            field_name = path.rstrip('[]').rsplit('.', 1)[-1]

            if python_type not in ('array', 'object'):
                field_stats.add_value(value, python_type)
                # Layer 1 (string narrowing) gives specific primitive types for scalars
                field_stats.fhir_types[detect_fhir_type(value, field_name)] += 1
            elif python_type == 'object':
                field_stats.types_seen[python_type] += 1
                field_stats.value_count += 1
                # Layer 1 (dict fingerprinting) + Layer 2 (field name) for complex types
                field_stats.fhir_types[detect_fhir_type(value, field_name)] += 1
            else:  # array
                field_stats.types_seen[python_type] += 1
                field_stats.value_count += 1

    def process_resources(self, resources: List[Dict[str, Any]]) -> None:
        for resource in resources:
            try:
                self.process_resource(resource)
            except Exception as e:
                resource_type = resource.get('resourceType', 'Unknown')
                resource_id = resource.get('id', 'no-id')
                self.state.errors.append(f"Error processing {resource_type}/{resource_id}: {e}")

    def mark_file_processed(self) -> None:
        self.state.files_processed += 1

    def get_summary(self) -> Dict[str, Any]:
        return {
            'files_processed': self.state.files_processed,
            'total_resources': self.state.total_resources,
            'resource_types': list(self.state.resource_types.keys()),
            'resource_type_counts': {
                rt: stats.total_resources
                for rt, stats in self.state.resource_types.items()
            },
            'error_count': len(self.state.errors)
        }

    def get_results(self) -> Dict[str, ResourceTypeStatistics]:
        return self.state.resource_types


# =============================================================================
# EXPORT
# =============================================================================

def format_top_values(top_values: List[tuple], as_json: bool = True) -> str:
    """Format top values for CSV output."""
    if not top_values:
        return ""

    if as_json:
        return json.dumps(top_values, ensure_ascii=False)
    else:
        return "; ".join(f"{val} ({count})" for val, count in top_values)


def export_resource_type_csv(
        resource_type: str,
        stats: ResourceTypeStatistics,
        output_dir: Path
) -> Path:
    """Export statistics for a single resource type to CSV."""
    # Sanitize resource type for filename
    safe_name = "".join(c if c.isalnum() or c in '-_' else '_' for c in resource_type)
    output_file = output_dir / f"{safe_name}_profile.csv"

    columns = [
        'field_path',
        'resources_with_field',
        'total_resources',
        'presence_rate',
        'missing_rate',
        'value_count',
        'unique_count',
        'unique_count_approximate',
        'detected_python_types',
        'detected_fhir_type',
        'type_inconsistency',
        'top_values'
    ]

    sorted_fields = sorted(stats.field_stats.items(), key=lambda x: x[0])

    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()

        for path, field_stats in sorted_fields:
            presence_rate = (
                field_stats.resource_count / stats.total_resources
                if stats.total_resources > 0 else 0
            )

            row = {
                'field_path': path,
                'resources_with_field': field_stats.resource_count,
                'total_resources': stats.total_resources,
                'presence_rate': f"{presence_rate:.2f}",
                'missing_rate': f"{1 - presence_rate:.2f}",
                'value_count': field_stats.value_count,
                'unique_count': field_stats.unique_count,
                'unique_count_approximate': field_stats.unique_count_approximate,
                'detected_python_types': ', '.join(field_stats.detected_python_types),
                'detected_fhir_type': field_stats.detected_fhir_type,
                'type_inconsistency': field_stats.has_type_inconsistency,
                'top_values': format_top_values(field_stats.top_values, as_json=True)
            }

            writer.writerow(row)

    return output_file


def export_all_results(
        results: Dict[str, ResourceTypeStatistics],
        output_dir: Path
) -> Dict[str, Path]:
    """Export all resource type statistics to CSV files."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    output_files = {}

    for resource_type, stats in results.items():
        output_file = export_resource_type_csv(resource_type, stats, output_dir)
        output_files[resource_type] = output_file
        print(f"  Exported {resource_type}: {len(stats.field_stats)} fields -> {output_file.name}")

    return output_files


def export_summary_csv(
        results: Dict[str, ResourceTypeStatistics],
        output_dir: Path
) -> Path:
    """Export a summary CSV with one row per resource type."""
    output_file = output_dir / "_summary.csv"

    columns = [
        'resource_type',
        'total_resources',
        'total_fields',
        'fields_always_present',
        'fields_sometimes_present',
        'fields_with_type_inconsistency'
    ]

    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()

        for resource_type, stats in sorted(results.items()):
            always_present = sum(
                1 for fs in stats.field_stats.values()
                if fs.resource_count == stats.total_resources and stats.total_resources > 0
            )
            sometimes_present = sum(
                1 for fs in stats.field_stats.values()
                if 0 < fs.resource_count < stats.total_resources
            )
            type_inconsistent = sum(
                1 for fs in stats.field_stats.values()
                if fs.has_type_inconsistency
            )

            row = {
                'resource_type': resource_type,
                'total_resources': stats.total_resources,
                'total_fields': len(stats.field_stats),
                'fields_always_present': always_present,
                'fields_sometimes_present': sometimes_present,
                'fields_with_type_inconsistency': type_inconsistent
            }

            writer.writerow(row)

    print(f"  Exported summary: {len(results)} resource types -> {output_file.name}")
    return output_file


# =============================================================================
# MAIN PROFILER
# =============================================================================

def run_profiler(
        input_dir: Path,
        output_dir: Path,
        config: Optional[ProfilerConfig] = None,
        verbose: bool = True
) -> dict:
    """Run the FHIR dataset profiler."""
    start_time = time.time()

    if config is None:
        config = ProfilerConfig(
            input_dir=input_dir,
            output_dir=output_dir,
            verbose=verbose
        )

    if verbose:
        print("=" * 60)
        print("FHIR Dataset Profiler")
        print("=" * 60)

    # Scan for files
    scan_result = scan_directory(
        config.input_dir,
        patterns=config.file_patterns,
        verbose=verbose
    )

    if not scan_result.files:
        print("No JSON files found!")
        return {'error': 'No files found'}

    # Initialize aggregator
    aggregator = Aggregator(config)

    # Process files
    if verbose:
        print(f"\nProcessing {len(scan_result.files)} files...")

    extraction_errors = []
    total_files = len(scan_result.files)

    for idx, file_path in enumerate(scan_result.files, 1):
        try:
            resources, bundle_count, errors = extract_resources_from_file(file_path)
            extraction_errors.extend(errors)

            aggregator.process_resources(resources)
            aggregator.mark_file_processed()

            if verbose and (idx % 50 == 0 or idx == total_files):
                summary = aggregator.get_summary()
                print(f"  [{idx}/{total_files}] Resources: {summary['total_resources']:,}")

        except Exception as e:
            extraction_errors.append(f"Fatal error processing {file_path}: {e}")

    # Get final results
    results = aggregator.get_results()
    summary = aggregator.get_summary()

    if verbose:
        print(f"\n" + "-" * 40)
        print("Processing Complete!")
        print(f"  Files processed: {summary['files_processed']:,}")
        print(f"  Total resources: {summary['total_resources']:,}")
        print(f"  Resource types:  {len(summary['resource_types'])}")
        print(f"  Errors:          {summary['error_count'] + len(extraction_errors)}")
        print("-" * 40)

    # Export results
    if verbose:
        print(f"\nExporting results to: {config.output_dir}")

    output_files = export_all_results(results, config.output_dir)
    summary_file = export_summary_csv(results, config.output_dir)

    # Calculate elapsed time
    elapsed = time.time() - start_time

    # Write errors to file if any
    all_errors = aggregator.state.errors + extraction_errors + scan_result.scan_errors
    if all_errors:
        error_file = config.output_dir / "_errors.txt"
        with open(error_file, 'w', encoding='utf-8') as f:
            for error in all_errors:
                f.write(f"{error}\n")
        if verbose:
            print(f"\nErrors written to: {error_file}")

    if verbose:
        print(f"\nDone! Elapsed time: {elapsed:.2f} seconds")

    return {
        'files_processed': summary['files_processed'],
        'total_resources': summary['total_resources'],
        'resource_types': summary['resource_types'],
        'resource_type_counts': summary['resource_type_counts'],
        'output_files': output_files,
        'summary_file': summary_file,
        'elapsed_seconds': elapsed,
        'error_count': len(all_errors)
    }


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

def main():
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description='Profile FHIR R4 datasets for structure and completeness analysis.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python fhir_profiler.py /data/fhir/export ./output
    python fhir_profiler.py --top-n 50 /data/fhir ./output
        """
    )

    parser.add_argument(
        'input_dir',
        type=Path,
        help='Directory containing FHIR JSON/NDJSON files (searched recursively)'
    )

    parser.add_argument(
        'output_dir',
        type=Path,
        help='Directory to write output CSV files'
    )

    parser.add_argument(
        '--top-n',
        type=int,
        default=20,
        help='Number of top values to track per field (default: 20)'
    )

    parser.add_argument(
        '--hll-precision',
        type=int,
        default=14,
        choices=range(4, 17),
        metavar='[4-16]',
        help='HyperLogLog precision (default: 14)'
    )

    parser.add_argument(
        '--hll-threshold',
        type=int,
        default=10000,
        help='Switch to HyperLogLog when unique count exceeds this (default: 10000)'
    )

    parser.add_argument(
        '--no-inline-extensions',
        action='store_true',
        help='Do not inline extension URLs in field paths'
    )

    parser.add_argument(
        '--quiet', '-q',
        action='store_true',
        help='Suppress progress output'
    )

    args = parser.parse_args()

    config = ProfilerConfig(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        top_values_limit=args.top_n,
        hll_precision=args.hll_precision,
        hll_threshold=args.hll_threshold,
        inline_extensions=not args.no_inline_extensions,
        verbose=not args.quiet
    )

    result = run_profiler(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        config=config,
        verbose=not args.quiet
    )

    sys.exit(0 if result.get('error') is None else 1)


if __name__ == '__main__':
    # For quick testing - hardcode your paths here
    from pathlib import Path

    INPUT_DIR = Path("/Users/carlabhc/Documents/Test FHIR Data")  # ← Change this when necessary
    OUTPUT_DIR = Path("/Users/carlabhc/Documents/Python Projects/FHIR_Dataset_Profiling/Output")  # ← Change this when necessary

    result = run_profiler(
        input_dir=INPUT_DIR,
        output_dir=OUTPUT_DIR,
        verbose=True
    )

    print(f"\nResults: {result}")
