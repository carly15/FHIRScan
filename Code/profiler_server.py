#!/usr/bin/env python3
"""
FHIRscan — FHIR Server Profiler
================================

Verbindet sich mit einem FHIR R4 Server (getestet auf Blaze) und erstellt
ein umfassendes statistisches Profil über alle Patientenressourcen.

Funktionsweise
--------------
1. **Verbindungscheck**: Prüft den Server via GET /metadata und zeigt
   Softwarename und FHIR-Version.
2. **Patientenliste**: Liest alle Patienten-IDs paginiert via GET /Patient.
3. **Ressourcenabruf**: Ruft für jeden Patienten alle verknüpften Ressourcen
   via GET /Patient/[id]/$everything ab (paginiert, mit Retry-Logik).
4. **Feldtraversierung**: Traversiert jeden Ressourceneintrag rekursiv und
   generiert vollständige Feldpfade (z. B. `subject.reference`, `code.coding[].system`).
5. **Statistiken**: Akkumuliert je Feldpfad und Ressourcentyp:
   - Vorkommen und Vollständigkeit (presence rate)
   - Datentypen (FHIR R4B-spezifisch: CodeableConcept, Reference, Period, ...)
   - Werteverteilung (Top-N-Werte, Kardinalität via HyperLogLog)
   - Numerische Kennzahlen (Min, Max, Mittelwert, Standardabweichung)
6. **Relationale Analyse**:
   - Kardinalitäten (Ressourcen je Patient / Encounter)
   - Referenzintegrität (dangling references)
   - Zeitliche Verteilung der Ressourcen
   - Strukturtiefe und Komplexitätsmetriken
   - Data Quality Score je Ressourcentyp
7. **Export**: Schreibt die Ergebnisse als CSV-Dateien in das Ausgabeverzeichnis.

Einschränkungen
---------------
Nur patientenzentrierte Ressourcen werden erfasst.  Der `$everything`-Endpunkt
liefert ausschließlich Ressourcen, die einem Patienten zugeordnet sind.
Standalone-Ressourcen wie Organization, Practitioner, Location, ValueSet,
CodeSystem oder patientenunverknüpfte Medication-Einträge sind für den Profiler
unsichtbar.  Diese Einschränkung ist bewusst akzeptiert — eine vollständige
Serverabfrage würde erheblich mehr Anfragen und Verarbeitungszeit erfordern.

Verwendung
----------
    python profiler_server.py <server_url> <output_dir> [Optionen]

Optionen
--------
    --token TOKEN         Bearer-Token für Authentifizierung
    --limit N             Nur die ersten N Patienten verarbeiten (Testlauf)
    --request-delay SEC   Pause zwischen Patienten in Sekunden (Server schonen)
    --max-retries N       Wiederholungsversuche bei Serverfehlern (Standard: 3)
    --page-size N         Ressourcen pro Seite (Standard: 100)
    --skip-types TYPEN    Kommagetrennte Ressourcentypen ausschließen (z. B. Binary)
    --run-name NAME       Eigener Name für den Ausgabeordner (Standard: Zeitstempel)
    --no-relations        Relationale Analyse deaktivieren
    --quiet               Keine Fortschrittsausgabe

Contents
--------
To jump to a section, search (Ctrl/Cmd+F) for the heading text shown below.
Named classes and functions are also listed in your IDE's Outline/Structure panel.

    profiler_server.py
    ├── CONFIGURATION ──────── ProfilerConfig
    ├── UTILITIES ──────────── get_python_type · _check_output_writable
    ├── FHIR R4B TYPE DETECTION  detect_fhir_type · parse_fhir_reference
    │                            parse_fhir_datetime
    ├── HYPERLOGLOG ────────── HyperLogLog
    ├── FIELD STATISTICS ───── FieldStatistics · ResourceTypeStatistics
    ├── RELATIONAL STATISTICS  CardinalityStats · ReferenceInfo · TemporalInfo
    │                          RelationalAnalyzer
    ├── SERVER CONNECTION ──── make_session · check_server · _get_with_retry
    │                          _follow_pages · fetch_patient_count
    │                          fetch_patient_ids · fetch_patient_resources
    ├── PATH TRAVERSAL ─────── traverse_resource
    ├── AGGREGATOR ─────────── Aggregator
    ├── EXPORT FUNCTIONS ───── export_resource_type_csv · export_cardinality_csv
    │                          export_data_quality_csv · export_summary_csv
    │                          export_summary_details_csv · export_all_results
    ├── MAIN PROFILER ──────── run_profiler
    └── CLI ENTRY POINT ────── main()
"""

import sys
import time
import tracemalloc
import json
import csv
import math
import hashlib
import argparse
import re
import requests
from pathlib import Path
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, List, Optional, Set, Tuple
from datetime import datetime
import statistics
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

_BERLIN_TZ = ZoneInfo('Europe/Berlin')


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class ProfilerConfig:
    """Configuration for the FHIR server profiler."""

    server_base_url: str       # e.g. "http://localhost:8080/fhir"
    output_dir: Path
    auth_token: Optional[str] = None   # Bearer token; None for unauthenticated local servers
    page_size: int = 100               # resources per page for Patient and $everything queries
    patient_limit: Optional[int] = None  # stop after N patients (None = all); for test runs
    request_delay: float = 0.0          # seconds to wait between patients; use to avoid overloading the server
    max_retries: int = 3                 # retry attempts for transient HTTP errors (429, 5xx)
    top_values_limit: int = 20
    max_value_length: int = 200
    hll_precision: int = 14
    hll_threshold: int = 10000
    inline_extensions: bool = True
    verbose: bool = True
    run_name: Optional[str] = None          # custom output subfolder name; defaults to timestamp
    skip_types: Set[str] = field(default_factory=set)  # resource types to exclude from profiling

    # Relational analysis settings
    analyze_relations: bool = True
    primary_anchors: tuple = ("Patient", "Encounter")  # Main entities to pivot around

    def __post_init__(self):
        self.server_base_url = self.server_base_url.rstrip('/')
        self.output_dir = Path(self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)


# =============================================================================
# UTILITIES
# =============================================================================

def get_python_type(value: Any) -> str:
    """Get a readable type name for a value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dictionary"
    return type(value).__name__


def _check_output_writable(output_dir: Path) -> bool:
    """Verify output_dir is writable before starting a long run."""
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        test_file = output_dir / '.write_test'
        test_file.touch()
        test_file.unlink()
        return True
    except (PermissionError, OSError) as e:
        print(f"Error: output directory is not writable: {output_dir}\n  {e}")
        return False


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
_RE_URI      = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.-]*:')
_RE_BASE64   = re.compile(r'^[A-Za-z0-9+/]*={0,2}$')
_RE_ID       = re.compile(r'^[A-Za-z0-9.-]{1,64}$')

# ContactPoint.system values (FHIR bound ValueSet — R4B §4.6.4)
_CONTACT_POINT_SYSTEMS: Set[str] = {'phone', 'fax', 'email', 'pager', 'url', 'sms', 'other'}

# Field names that reliably carry base64Binary data (Attachment.data, Attachment.hash)
_BASE64_FIELD_NAMES: Set[str] = {'data', 'hash'}

# Field names that reliably carry markdown content
_MARKDOWN_FIELD_NAMES: Set[str] = {'requirements', 'copyright', 'purpose', 'comment', 'description'}

# Direct mapping of FHIR R4B field names to their declared complex datatype.
# Field names are normative in the spec — more reliable than inspecting object keys.
# Reference: https://hl7.org/fhir/R4B/datatypes.html
_FHIR_FIELD_TYPES: Dict[str, str] = {
    # HumanName
    'name':                 'HumanName',
    # Address
    'address':              'Address',
    # ContactPoint
    'telecom':              'ContactPoint',
    # Identifier
    'identifier':           'Identifier',
    # Period
    'period':               'Period',
    'effectivePeriod':      'Period',
    'billablePeriod':       'Period',
    'requestedPeriod':      'Period',
    # CodeableConcept — field names that are universally CodeableConcept across resources
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
    # Coding (Encounter.class and a few others are Coding, not CodeableConcept)
    'coding':               'Coding',
    'class':                'Coding',
    # Reference — common subject/context fields
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
    # Quantity
    'quantity':             'Quantity',
    'doseQuantity':         'Quantity',
    # Age (constrained Quantity)
    'age':                  'Age',
    # Duration (constrained Quantity)
    'duration':             'Duration',
    # Annotation
    'note':                 'Annotation',
    # Attachment
    'photo':                'Attachment',
    # Timing
    'timing':               'Timing',
    # Money
    'net':                  'Money',
    'unitPrice':            'Money',
    'amount':               'Money',
    # Signature
    'signature':            'Signature',
    # Special resource-level types
    'text':                 'Narrative',
    'meta':                 'Meta',
}

# FHIR type name suffixes used in value[x] polymorphic fields.
# e.g. valueQuantity → Quantity, onsetAge → Age, scheduledTiming → Timing.
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
        if field_name == 'offset':
            return 'unsignedInt'
        return 'integer'

    if isinstance(value, dict):
        fhir_type = _fingerprint_dict(value)
        if fhir_type:
            return fhir_type
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


def parse_fhir_reference(reference: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Parse a FHIR reference string into (resourceType, id).

    Handles formats:
    - "Patient/123"
    - "urn:uuid:abc-def"
    - "https://example.org/fhir/Patient/123"
    """
    if not reference:
        return None, None

    # Handle relative references: "Patient/123"
    if '/' in reference and not reference.startswith('http'):
        parts = reference.split('/')
        if len(parts) >= 2:
            # Take last two parts (handles "Patient/123" and longer paths)
            return parts[-2], parts[-1]

    # Handle URN references: "urn:uuid:abc-def"
    if reference.startswith('urn:'):
        return 'urn', reference

    # Handle absolute URLs
    if reference.startswith('http'):
        # Try to extract ResourceType/id from URL
        match = re.search(r'/([A-Z][a-zA-Z]+)/([^/]+)$', reference)
        if match:
            return match.group(1), match.group(2)

    return None, reference


def parse_fhir_datetime(value: str) -> Optional[datetime]:
    """Parse a FHIR datetime string and return it normalized to Europe/Berlin.

    Naive datetimes (no timezone in the string) are assumed to already be Berlin
    local time and are stamped accordingly.  Timezone-aware datetimes are converted
    to Berlin time.  This ensures all datetimes are comparable without TypeError.
    """
    if not value or not isinstance(value, str):
        return None

    formats = [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d",
        "%Y-%m",
        "%Y"
    ]

    # Strip the colon from timezone offsets (e.g. "+05:30" → "+0530") for strptime compatibility
    value_clean = re.sub(r'(\+|-)(\d{2}):(\d{2})$', r'\1\2\3', value)

    for fmt in formats:
        try:
            dt = datetime.strptime(value_clean, fmt)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=_BERLIN_TZ)   # assume Berlin local time
            return dt.astimezone(_BERLIN_TZ)            # convert any other TZ to Berlin
        except ValueError:
            continue

    return None


# =============================================================================
# HYPERLOGLOG IMPLEMENTATION
# =============================================================================

class HyperLogLog:
    """HyperLogLog cardinality estimator."""

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

    @staticmethod
    def _hash(value: Any) -> int:
        # Produce a 64-bit integer hash from any value via SHA-256 (first 8 bytes)
        str_repr = str(value).encode('utf-8')
        hash_bytes = hashlib.sha256(str_repr).digest()[:8]
        return int.from_bytes(hash_bytes, 'big')

    @staticmethod
    def _leading_zeros(value: int, max_bits: int = 64) -> int:
        # Count leading zero bits within a max_bits-wide integer
        # Used to compute the "rank" (position of the first 1-bit)
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
# FIELD STATISTICS
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
    fhir_types: Counter = field(default_factory=Counter)  # detected FHIR complex type(s) when field is an object

    # Numeric statistics accumulated via Welford's online algorithm (no list of values stored)
    numeric_count: int = 0
    numeric_min: Optional[float] = None
    numeric_max: Optional[float] = None
    _numeric_mean: float = field(default=0.0, init=False, repr=False)
    _numeric_M2: float = field(default=0.0, init=False, repr=False)

    def add_value(self, value: Any, python_type: str) -> None:
        self.value_count += 1
        self.types_seen[python_type] += 1

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            fv = float(value)
            self.numeric_count += 1
            if self.numeric_min is None or fv < self.numeric_min:
                self.numeric_min = fv
            if self.numeric_max is None or fv > self.numeric_max:
                self.numeric_max = fv
            delta = fv - self._numeric_mean
            self._numeric_mean += delta / self.numeric_count
            self._numeric_M2 += delta * (fv - self._numeric_mean)

        str_value = str(value)
        # Truncate very long values to avoid excessive memory use
        if len(str_value) > self.max_value_length:
            str_value = str_value[:self.max_value_length - 3] + "..."

        self.value_counter[str_value] += 1  # for top-N most frequent values

        if self.hll is not None:
            self.hll.add(str_value)
        else:
            assert self.exact_values is not None  # exact_values is cleared only when hll is set
            self.exact_values.add(str_value)
            # Once distinct values exceed the threshold, switch to approximate counting
            if len(self.exact_values) > self.hll_threshold:
                self._switch_to_hll()

    def _switch_to_hll(self) -> None:
        # Migrate all exact values into a HyperLogLog, then free the set
        assert self.exact_values is not None
        self.hll = HyperLogLog(self.hll_precision)
        for val in self.exact_values:
            self.hll.add(val)
        self.exact_values = None  # release memory

    def mark_resource_presence(self) -> None:
        self.resource_count += 1

    @property
    def unique_count(self) -> int:
        if self.hll is not None:
            return self.hll.count()
        return len(self.exact_values) if self.exact_values is not None else 0

    @property
    def unique_count_approximate(self) -> bool:
        return self.hll is not None

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

    @property
    def numeric_mean(self) -> Optional[float]:
        return round(self._numeric_mean, 6) if self.numeric_count > 0 else None

    @property
    def numeric_stdev(self) -> Optional[float]:
        if self.numeric_count < 2:
            return None
        return round(math.sqrt(self._numeric_M2 / (self.numeric_count - 1)), 6)


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


# =============================================================================
# RELATIONAL STATISTICS
# =============================================================================

@dataclass
class CardinalityStats:
    """Statistics for cardinality distribution."""
    counts: List[int] = field(default_factory=list)

    def add(self, count: int) -> None:
        self.counts.append(count)

    @property
    def total_anchors(self) -> int:
        return len(self.counts)

    @property
    def total_resources(self) -> int:
        return sum(self.counts)

    @property
    def min_count(self) -> int:
        return min(self.counts) if self.counts else 0

    @property
    def max_count(self) -> int:
        return max(self.counts) if self.counts else 0

    @property
    def mean_count(self) -> float:
        return statistics.mean(self.counts) if self.counts else 0.0

    @property
    def median_count(self) -> float:
        return statistics.median(self.counts) if self.counts else 0.0

    @property
    def std_dev(self) -> float:
        return statistics.stdev(self.counts) if len(self.counts) > 1 else 0.0

    def percentile(self, p: int) -> float:
        """Calculate a single percentile (0–100)."""
        if not self.counts:
            return 0.0
        sorted_counts = sorted(self.counts)
        idx = int(len(sorted_counts) * p / 100)
        # Clamp index to last valid position (guards against p=100 going out of bounds)
        return sorted_counts[min(idx, len(sorted_counts) - 1)]

    def percentiles(self, *ps: int) -> Dict[int, float]:
        """Calculate multiple percentiles in one sort — use when calling several at once."""
        if not self.counts:
            return {p: 0.0 for p in ps}
        sorted_counts = sorted(self.counts)
        n = len(sorted_counts)
        return {p: sorted_counts[min(int(n * p / 100), n - 1)] for p in ps}

    def to_dict(self) -> Dict[str, Any]:
        pcts = self.percentiles(25, 75, 90, 99)
        return {
            'total_anchors': self.total_anchors,
            'total_resources': self.total_resources,
            'min': self.min_count,
            'max': self.max_count,
            'mean': round(self.mean_count, 2),
            'median': self.median_count,
            'std_dev': round(self.std_dev, 2),
            'p25': pcts[25],
            'p75': pcts[75],
            'p90': pcts[90],
            'p99': pcts[99],
        }


@dataclass
class ReferenceInfo:
    """Information about a reference from one resource to another."""
    source_type: str
    source_id: str
    reference_path: str
    target_type: Optional[str]
    target_id: Optional[str]
    raw_reference: str


@dataclass
class TemporalInfo:
    """Temporal information extracted from a resource."""
    resource_type: str
    resource_id: str
    patient_id: Optional[str]
    encounter_id: Optional[str]
    effective_date: Optional[datetime]
    period_start: Optional[datetime]
    period_end: Optional[datetime]


@dataclass
class RelationalAnalyzer:
    """Analyzes relationships between FHIR resources."""

    # All resource IDs seen; used to check whether a referenced target actually exists
    known_ids: Dict[str, Set[str]] = field(default_factory=lambda: defaultdict(set))

    # Every reference object found across all resources (used for integrity checks)
    references: List[ReferenceInfo] = field(default_factory=list)

    # How many resources of each type link to each patient/encounter ID
    cardinality_by_patient: Dict[str, Dict[str, int]] = field(default_factory=lambda: defaultdict(Counter))
    cardinality_by_encounter: Dict[str, Dict[str, int]] = field(default_factory=lambda: defaultdict(Counter))

    # Maps Encounter ID → Patient ID (built while processing Encounter resources)
    encounter_to_patient: Dict[str, str] = field(default_factory=dict)

    # Temporal data
    temporal_data: List[TemporalInfo] = field(default_factory=list)

    # Maximum nesting depth per resource (one entry per resource processed)
    max_depths: Dict[str, List[int]] = field(default_factory=lambda: defaultdict(list))
    # Sizes of every array encountered, keyed by resource type
    array_sizes: Dict[str, List[int]] = field(default_factory=lambda: defaultdict(list))

    # Counts for patient/encounter linkage quality metrics
    resources_with_patient_ref: Dict[str, int] = field(default_factory=Counter)
    resources_without_patient_ref: Dict[str, int] = field(default_factory=Counter)
    resources_with_encounter_ref: Dict[str, int] = field(default_factory=Counter)

    def register_resource(self, resource: Dict[str, Any]) -> None:
        """Register a resource's existence."""
        resource_type = resource.get('resourceType')
        resource_id = resource.get('id')

        if resource_type and resource_id:
            self.known_ids[resource_type].add(resource_id)

    def extract_temporal(self, resource: Dict[str, Any]) -> None:
        """Extract temporal information from a resource."""
        resource_type = resource.get('resourceType', 'Unknown')
        resource_id = resource.get('id', 'unknown')

        # Find patient reference
        patient_id = None
        encounter_id = None

        subject = resource.get('subject', {})
        if isinstance(subject, dict) and subject.get('reference'):
            t, i = parse_fhir_reference(subject['reference'])
            if t == 'Patient':
                patient_id = i

        encounter = resource.get('encounter', {})
        if isinstance(encounter, dict) and encounter.get('reference'):
            t, i = parse_fhir_reference(encounter['reference'])
            if t == 'Encounter':
                encounter_id = i

        # Extract dates
        effective_date = None
        period_start = None
        period_end = None

        # Try various date fields
        for date_field in ['effectiveDateTime', 'issued', 'authoredOn', 'recordedDate', 'date']:
            if date_field in resource:
                effective_date = parse_fhir_datetime(resource[date_field])
                if effective_date:
                    break

        # Try period
        period = resource.get('period', {}) or resource.get('effectivePeriod', {})
        if isinstance(period, dict):
            period_start = parse_fhir_datetime(period.get('start'))
            period_end = parse_fhir_datetime(period.get('end'))

        if effective_date or period_start or period_end:
            self.temporal_data.append(TemporalInfo(
                resource_type=resource_type,
                resource_id=resource_id,
                patient_id=patient_id,
                encounter_id=encounter_id,
                effective_date=effective_date,
                period_start=period_start,
                period_end=period_end
            ))


    def compute_cardinality_stats(self) -> Dict[str, Dict[str, 'CardinalityStats']]:
        """Compute cardinality statistics per resource type per anchor."""
        results: Dict[str, Dict[str, CardinalityStats]] = {}

        def _build_stats(counts_by_anchor: Dict[str, int]) -> CardinalityStats:
            stats = CardinalityStats()
            for count in counts_by_anchor.values():
                stats.add(count)
            return stats

        for resource_type, patient_counts in self.cardinality_by_patient.items():
            results.setdefault(resource_type, {})['per_patient'] = _build_stats(patient_counts)

        for resource_type, encounter_counts in self.cardinality_by_encounter.items():
            results.setdefault(resource_type, {})['per_encounter'] = _build_stats(encounter_counts)

        # Encounters per Patient (derived from the encounter→patient mapping)
        encounters_per_patient: Counter = Counter()
        for patient_id in self.encounter_to_patient.values():
            encounters_per_patient[patient_id] += 1

        if encounters_per_patient:
            results.setdefault('Encounter', {})['per_patient'] = _build_stats(dict(encounters_per_patient))

        return results

    def compute_reference_integrity(self) -> Dict[str, Any]:
        """Compute reference integrity statistics."""
        total_refs = len(self.references)
        orphan_refs = []
        refs_by_type = defaultdict(lambda: {'total': 0, 'valid': 0, 'orphan': 0, 'unknown_type': 0})

        for ref in self.references:
            key = f"{ref.source_type}.{ref.reference_path} -> {ref.target_type or 'unknown'}"
            refs_by_type[key]['total'] += 1

            if ref.target_type is None:
                refs_by_type[key]['unknown_type'] += 1
            elif ref.target_id and ref.target_id in self.known_ids.get(ref.target_type, set()):
                # Target ID was seen in the dataset → valid reference
                refs_by_type[key]['valid'] += 1
            else:
                # Target not found in known_ids → orphan (dangling reference)
                refs_by_type[key]['orphan'] += 1
                if len(orphan_refs) < 100:  # Cap examples
                    orphan_refs.append({
                        'source': f"{ref.source_type}/{ref.source_id}",
                        'path': ref.reference_path,
                        'target': ref.raw_reference
                    })

        return {
            'total_references': total_refs,
            'references_by_path': dict(refs_by_type),
            'orphan_examples': orphan_refs[:20]
        }

    def compute_temporal_stats(self) -> Dict[str, Any]:
        """Compute temporal statistics."""
        # Date ranges per patient
        patient_date_ranges = defaultdict(list)
        resource_type_dates = defaultdict(list)

        for temp in self.temporal_data:
            effective = temp.effective_date or temp.period_start
            if effective:
                resource_type_dates[temp.resource_type].append(effective)
                if temp.patient_id:
                    patient_date_ranges[temp.patient_id].append(effective)

        # Calculate per-patient spans
        patient_spans = []
        for patient_id, dates in patient_date_ranges.items():
            if len(dates) >= 2:
                min_date = min(dates)
                max_date = max(dates)
                span_days = (max_date - min_date).days
                patient_spans.append(span_days)

        # Calculate per-resource-type date ranges
        type_ranges = {}
        for rt, dates in resource_type_dates.items():
            if dates:
                type_ranges[rt] = {
                    'count': len(dates),
                    'earliest': min(dates).isoformat(),
                    'latest': max(dates).isoformat()
                }

        return {
            'resources_with_dates': len(self.temporal_data),
            'patients_with_date_data': len(patient_date_ranges),
            'patient_record_span_days': {
                'min': min(patient_spans) if patient_spans else 0,
                'max': max(patient_spans) if patient_spans else 0,
                'mean': round(statistics.mean(patient_spans), 1) if patient_spans else 0,
                'median': statistics.median(patient_spans) if patient_spans else 0
            },
            'date_ranges_by_type': type_ranges
        }

    def compute_structural_stats(self) -> Dict[str, Any]:
        """Compute structural depth statistics."""
        depth_stats = {}
        for rt, depths in self.max_depths.items():
            if depths:
                depth_stats[rt] = {
                    'min_depth': min(depths),
                    'max_depth': max(depths),
                    'mean_depth': round(statistics.mean(depths), 2),
                    'median_depth': statistics.median(depths)
                }

        array_stats = {}
        for key, sizes in self.array_sizes.items():
            if sizes:
                array_stats[key] = {
                    'count': len(sizes),
                    'min_size': min(sizes),
                    'max_size': max(sizes),
                    'mean_size': round(statistics.mean(sizes), 2),
                    'total_elements': sum(sizes)
                }

        return {
            'depth_by_type': depth_stats,
            'array_statistics': array_stats
        }

    def compute_data_quality_scores(self) -> Dict[str, Any]:
        """Compute data quality metrics."""
        quality_by_type = {}

        all_types = set(self.resources_with_patient_ref.keys()) | set(self.resources_without_patient_ref.keys())

        for rt in all_types:
            with_patient = self.resources_with_patient_ref.get(rt, 0)
            without_patient = self.resources_without_patient_ref.get(rt, 0)
            with_encounter = self.resources_with_encounter_ref.get(rt, 0)
            total = with_patient + without_patient

            if total > 0:
                quality_by_type[rt] = {
                    'total_resources': total,
                    'with_patient_reference': with_patient,
                    'without_patient_reference': without_patient,
                    'patient_linkage_rate': round(with_patient / total, 4),
                    'with_encounter_reference': with_encounter,
                    'encounter_linkage_rate': round(with_encounter / total, 4) if total > 0 else 0
                }

        return quality_by_type


# =============================================================================
# SERVER CONNECTION
# =============================================================================

def make_session(auth_token: Optional[str] = None) -> requests.Session:
    """Create an HTTP session with FHIR JSON headers and optional Bearer auth."""
    session = requests.Session()
    session.headers['Accept'] = 'application/fhir+json'
    session.headers['Content-Type'] = 'application/fhir+json'
    if auth_token:
        session.headers['Authorization'] = f'Bearer {auth_token}'
    return session


def check_server(session: requests.Session, base_url: str, verbose: bool = True) -> bool:
    """Verify the server is reachable and speaks FHIR R4. Returns False on failure."""
    if verbose:
        print(f"Checking server: {base_url}/metadata")
    try:
        resp = session.get(f"{base_url}/metadata", timeout=15)
        resp.raise_for_status()
        cap = resp.json()
        fhir_version = cap.get('fhirVersion', 'unknown')
        if not fhir_version.startswith('4.'):
            print(f"  Warning: server reports FHIR {fhir_version}, expected R4 (4.x)")
        if verbose:
            sw = cap.get('software', {})
            print(f"  OK — {sw.get('name', 'Unknown')} {sw.get('version', '')} "
                  f"(FHIR {fhir_version})")
        return True
    except requests.exceptions.ConnectionError:
        print(f"  Error: cannot reach {base_url} — is the server running?")
    except requests.exceptions.Timeout:
        print(f"  Error: server timed out on /metadata")
    except requests.exceptions.HTTPError as e:
        print(f"  Error: server returned HTTP {e.response.status_code} for /metadata")
    except Exception as e:
        print(f"  Error: {e}")
    return False


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _get_with_retry(
        session: requests.Session,
        url: str,
        max_retries: int = 3,
        timeout: int = 60
) -> requests.Response:
    """GET with exponential backoff for transient server errors (429, 5xx)."""
    for attempt in range(max_retries + 1):
        response = session.get(url, timeout=timeout)
        if response.status_code not in _RETRYABLE_STATUS:
            response.raise_for_status()
            return response
        if attempt == max_retries:
            response.raise_for_status()
        wait = float(response.headers.get('Retry-After', 2 ** attempt))
        print(f"  [retry {attempt + 1}/{max_retries}] HTTP {response.status_code} — "
              f"waiting {wait:.0f}s before retrying...")
        time.sleep(wait)
    return response  # unreachable


def _follow_pages(
        session: requests.Session,
        url: str,
        max_retries: int = 3
) -> Generator[Dict, None, None]:
    """Fetch a paginated FHIR Bundle sequence, yielding one Bundle per page."""
    while url:
        response = _get_with_retry(session, url, max_retries=max_retries)
        bundle = response.json()
        yield bundle
        url = next(
            (link['url'] for link in bundle.get('link', []) if link.get('relation') == 'next'),
            None
        )


def fetch_patient_count(session: requests.Session, base_url: str) -> Optional[int]:
    """Return the total number of patients on the server, or None if unavailable."""
    try:
        resp = session.get(f"{base_url}/Patient?_summary=count", timeout=30)
        resp.raise_for_status()
        return resp.json().get('total')
    except Exception:
        return None


def fetch_patient_ids(
        session: requests.Session,
        base_url: str,
        page_size: int = 100,
        max_retries: int = 3
) -> Generator[str, None, None]:
    """Stream patient IDs from GET /Patient, following pagination."""
    url = f"{base_url}/Patient?_count={page_size}&_elements=id"
    for bundle in _follow_pages(session, url, max_retries=max_retries):
        for entry in bundle.get('entry', []):
            pid = entry.get('resource', {}).get('id')
            if pid:
                yield pid


def fetch_patient_resources(
        session: requests.Session,
        base_url: str,
        patient_id: str,
        page_size: int = 100,
        errors: Optional[List[str]] = None,
        max_retries: int = 3
) -> Generator[Dict, None, None]:
    """Stream all resources for a patient via $everything, following pagination."""
    url = f"{base_url}/Patient/{patient_id}/$everything?_count={page_size}"
    try:
        for bundle in _follow_pages(session, url, max_retries=max_retries):
            for entry in bundle.get('entry', []):
                resource = entry.get('resource')
                if resource and isinstance(resource, dict):
                    yield resource
    except Exception as e:
        if errors is not None:
            errors.append(f"Error fetching Patient/{patient_id}: {e}")


# =============================================================================
# PATH TRAVERSAL
# =============================================================================

def traverse_resource(
        resource: Dict[str, Any],
        inline_extensions: bool = True,
        max_depth: int = 50
) -> Generator[Tuple[str, Any, str, bool, int], None, None]:
    """Recursively traverse a FHIR resource, yielding (path, value, type, is_first, depth)."""
    resource_type = resource.get('resourceType', 'Unknown')
    # Tracks every path seen in this resource; used to emit is_first=True only on first encounter
    paths_in_resource: Set[str] = set()

    def _traverse(obj: Any, current_path: str, depth: int = 0):
        if depth > max_depth:
            return

        if isinstance(obj, dict):
            # Yield the object itself before recursing into its fields
            yield (current_path, obj, 'object', current_path not in paths_in_resource, depth)
            paths_in_resource.add(current_path)

            if inline_extensions and 'extension' in obj:
                # Flatten extensions: use their URL as part of the path instead of a numeric index
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
            yield (array_path, obj, 'array', array_path not in paths_in_resource, depth)
            paths_in_resource.add(array_path)

            for item in obj:
                yield from _traverse(item, array_path, depth + 1)

        else:
            # Scalar value (string, number, boolean, null)
            is_first = current_path not in paths_in_resource  # True only on first occurrence in this resource
            paths_in_resource.add(current_path)
            yield (current_path, obj, get_python_type(obj), is_first, depth)

    yield from _traverse(resource, resource_type)


# =============================================================================
# AGGREGATOR
# =============================================================================

class Aggregator:
    """Main aggregation engine."""

    def __init__(self, config: ProfilerConfig):
        self.config = config
        self.resource_types: Dict[str, ResourceTypeStatistics] = {}
        self.files_processed: int = 0
        self.total_resources: int = 0
        self.errors: List[str] = []
        self.relational = RelationalAnalyzer()
        # Tracks (resourceType, id) pairs already processed to prevent double-counting
        # resources that appear in multiple patient bundles (e.g. shared Medication records)
        self.seen_resource_ids: Set[Tuple[str, str]] = set()
        self.dedup_counts: Counter = Counter()  # per-type count of skipped duplicates

    def _get_or_create_resource_type(self, resource_type: str) -> ResourceTypeStatistics:
        if resource_type not in self.resource_types:
            self.resource_types[resource_type] = ResourceTypeStatistics(
                resource_type=resource_type,
                hll_precision=self.config.hll_precision,
                hll_threshold=self.config.hll_threshold,
                top_n=self.config.top_values_limit,
                max_value_length=self.config.max_value_length
            )
        return self.resource_types[resource_type]

    def process_resource(self, resource: Dict[str, Any]) -> None:
        resource_type = resource.get('resourceType')

        if not resource_type:
            self.errors.append("Resource without resourceType encountered")
            return

        if resource_type in self.config.skip_types:
            return

        # Deduplicate by (resourceType, id): the same resource can appear in multiple
        # patient bundles (e.g. a shared Medication record). Without this check it
        # would be counted once per bundle, inflating totals.
        resource_id = resource.get('id')
        if resource_id:
            dedup_key = (resource_type, resource_id)
            if dedup_key in self.seen_resource_ids:
                self.dedup_counts[resource_type] += 1
                return
            self.seen_resource_ids.add(dedup_key)

        type_stats = self._get_or_create_resource_type(resource_type)
        type_stats.increment_resource_count()
        self.total_resources += 1

        paths_seen_in_resource: Set[str] = set()
        analyze = self.config.analyze_relations
        # Relational state accumulated across the single traversal pass
        local_patient_ref: Optional[str] = None
        local_encounter_ref: Optional[str] = None
        resource_max_depth: int = 0
        # prefix length used to strip "ResourceType." from paths for reference_path storage
        rt_prefix_len = len(resource_type) + 1

        for path, value, python_type, is_first_in_resource, depth in traverse_resource(
                resource,
                inline_extensions=self.config.inline_extensions
        ):
            # ── field-level profiling ─────────────────────────────────────────
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

            if not analyze:
                continue

            # ── relational analysis (merged — no second/third traversal) ──────
            resource_max_depth = max(resource_max_depth, depth)

            if python_type == 'array':
                self.relational.array_sizes[f"{resource_type}_arrays"].append(len(value))

            elif python_type == 'object' and 'reference' in value:
                ref_value = value['reference']
                target_type, target_id = parse_fhir_reference(ref_value)
                # Strip the leading "ResourceType." prefix to match the original path format
                ref_path = path[rt_prefix_len:] if len(path) > rt_prefix_len else ''
                self.relational.references.append(ReferenceInfo(
                    source_type=resource_type,
                    source_id=resource_id or 'unknown',
                    reference_path=ref_path,
                    target_type=target_type,
                    target_id=target_id,
                    raw_reference=ref_value
                ))
                if target_type == 'Patient':
                    local_patient_ref = target_id
                elif target_type == 'Encounter':
                    local_encounter_ref = target_id

        # ── post-traversal relational updates ─────────────────────────────────
        if analyze:
            self.relational.register_resource(resource)
            self.relational.extract_temporal(resource)
            self.relational.max_depths[resource_type].append(resource_max_depth)

            if local_patient_ref:
                self.relational.cardinality_by_patient[resource_type][local_patient_ref] += 1
                self.relational.resources_with_patient_ref[resource_type] += 1
            else:
                self.relational.resources_without_patient_ref[resource_type] += 1

            if local_encounter_ref:
                self.relational.cardinality_by_encounter[resource_type][local_encounter_ref] += 1
                self.relational.resources_with_encounter_ref[resource_type] += 1

            if resource_type == 'Encounter' and local_patient_ref and resource_id:
                self.relational.encounter_to_patient[resource_id] = local_patient_ref

    def process_resources(self, resources: List[Dict[str, Any]]) -> None:
        for resource in resources:
            try:
                self.process_resource(resource)
            except Exception as e:
                resource_type = resource.get('resourceType', 'Unknown')
                resource_id = resource.get('id', 'no-id')
                self.errors.append(f"Error processing {resource_type}/{resource_id}: {e}")

    def mark_file_processed(self) -> None:
        self.files_processed += 1

    def get_summary(self) -> Dict[str, Any]:
        return {
            'files_processed': self.files_processed,
            'total_resources': self.total_resources,
            'resource_types': list(self.resource_types.keys()),
            'resource_type_counts': {
                rt: stats.total_resources
                for rt, stats in self.resource_types.items()
            },
            'error_count': len(self.errors)
        }

    def get_results(self) -> Dict[str, ResourceTypeStatistics]:
        return self.resource_types

    def get_relational_results(self) -> RelationalAnalyzer:
        return self.relational

    def get_dedup_counts(self) -> Counter:
        return self.dedup_counts


# =============================================================================
# EXPORT FUNCTIONS
# =============================================================================

def format_top_values(top_values: List[tuple], as_json: bool = True) -> str:
    if not top_values:
        return ""
    if as_json:
        return json.dumps(top_values, ensure_ascii=False)
    return "; ".join(f"{val} ({count})" for val, count in top_values)


def export_resource_type_csv(
        resource_type: str,
        stats: ResourceTypeStatistics,
        output_dir: Path
) -> Path:
    """Export field statistics for a single resource type."""
    safe_name = "".join(c if c.isalnum() or c in '-_' else '_' for c in resource_type)
    output_file = output_dir / f"{safe_name}_fields.csv"

    columns = [
        'field_path', 'resources_with_field', 'total_resources',
        'presence_rate', 'missing_rate', 'value_count', 'unique_count',
        'unique_count_approximate', 'detected_python_types', 'detected_fhir_type',
        'type_inconsistency',
        'numeric_count', 'numeric_min', 'numeric_max', 'numeric_mean', 'numeric_stdev',
        'top_values'
    ]

    sorted_fields = sorted(stats.field_stats.items(), key=lambda x: x[0])

    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()

        for path, field_stats in sorted_fields:
            presence_rate = field_stats.resource_count / stats.total_resources if stats.total_resources > 0 else 0

            writer.writerow({
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
                'numeric_count': field_stats.numeric_count or '',
                'numeric_min': field_stats.numeric_min if field_stats.numeric_min is not None else '',
                'numeric_max': field_stats.numeric_max if field_stats.numeric_max is not None else '',
                'numeric_mean': field_stats.numeric_mean if field_stats.numeric_mean is not None else '',
                'numeric_stdev': field_stats.numeric_stdev if field_stats.numeric_stdev is not None else '',
                'top_values': format_top_values(field_stats.top_values)
            })

    return output_file


def export_cardinality_csv(
        cardinality_stats: Dict[str, Dict[str, CardinalityStats]],
        relational: 'RelationalAnalyzer',
        output_dir: Path
) -> Path:
    """Export cardinality analysis with patient/encounter coverage."""
    output_file = output_dir / "_cardinality.csv"

    # Pre-compute totals for coverage ratios
    total_patients = len(relational.known_ids.get('Patient', set()))
    total_encounters = len(relational.known_ids.get('Encounter', set()))

    columns = [
        'resource_type', 'anchor_type',
        # How many unique anchors (patients or encounters) have ≥1 resource of this type
        'anchors_with_resource', 'total_anchors_in_dataset', 'coverage_rate',
        # Distribution of resource counts per anchor
        'total_resources', 'min', 'max', 'mean', 'median', 'std_dev',
        'p25', 'p75', 'p90', 'p99'
    ]

    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()

        for resource_type in sorted(cardinality_stats.keys()):
            anchors = cardinality_stats[resource_type]
            for anchor_type in sorted(anchors.keys()):
                stats = anchors[anchor_type]

                # anchors_with_resource = unique patients/encounters that have ≥1 resource
                # stats.total_anchors counts only those with at least one (built from cardinality dicts)
                anchors_with_resource = stats.total_anchors
                if anchor_type == 'per_patient':
                    total_in_dataset = total_patients
                elif anchor_type == 'per_encounter':
                    total_in_dataset = total_encounters
                else:
                    total_in_dataset = anchors_with_resource  # unknown anchor type

                coverage_rate = (
                    anchors_with_resource / total_in_dataset
                    if total_in_dataset > 0 else 0.0
                )

                # Compute all percentiles in one sort pass
                pcts = stats.percentiles(25, 75, 90, 99)

                # Build the row explicitly — avoids any key collision from to_dict()
                # coverage_rate is formatted as a fixed 4-decimal string so spreadsheets
                # with European locale don't misread e.g. "0.377" as the integer 377
                writer.writerow({
                    'resource_type': resource_type,
                    'anchor_type': anchor_type,
                    'anchors_with_resource': anchors_with_resource,
                    'total_anchors_in_dataset': total_in_dataset,
                    'coverage_rate': f"{coverage_rate:.4f}",
                    'total_resources': stats.total_resources,
                    'min': stats.min_count,
                    'max': stats.max_count,
                    'mean': round(stats.mean_count, 2),
                    'median': stats.median_count,
                    'std_dev': round(stats.std_dev, 2),
                    'p25': pcts[25],
                    'p75': pcts[75],
                    'p90': pcts[90],
                    'p99': pcts[99],
                })

    return output_file


def export_data_quality_csv(
        quality_stats: Dict[str, Any],
        output_dir: Path
) -> Path:
    """Export data quality metrics."""
    output_file = output_dir / "_data_quality.csv"

    columns = [
        'resource_type', 'total_resources', 'with_patient_reference',
        'without_patient_reference', 'patient_linkage_rate',
        'with_encounter_reference', 'encounter_linkage_rate'
    ]

    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()

        for resource_type in sorted(quality_stats.keys()):
            row = {'resource_type': resource_type}
            row.update(quality_stats[resource_type])
            writer.writerow(row)

    return output_file


def _format_as_text(data: Any, indent: int = 0) -> str:
    """Recursively format a dict or list as readable plain text."""
    prefix = "  " * indent
    lines = []
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.append(_format_as_text(value, indent + 1))
            else:
                lines.append(f"{prefix}{key}: {value}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                lines.append(_format_as_text(item, indent))
                lines.append("")  # blank line between list entries
            else:
                lines.append(f"{prefix}- {item}")
    else:
        lines.append(f"{prefix}{data}")
    return "\n".join(lines)


def _export_analysis_txt(title: str, data: Any, filename: str, output_dir: Path) -> Path:
    """Write a titled plain-text analysis report."""
    output_file = output_dir / filename
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(f"{title}\n")
        f.write("=" * 40 + "\n\n")
        f.write(_format_as_text(data))
        f.write("\n")
    return output_file


def export_summary_csv(
        results: Dict[str, ResourceTypeStatistics],
        output_dir: Path,
        relational: 'RelationalAnalyzer',
        dedup_counts: Counter,
        unique_patients: int = 0
) -> Path:
    """Export summary of all resource types."""
    output_file = output_dir / "_summary.csv"

    columns = [
        'resource_type', 'total_resources', 'deduplicated_resources',
        'patient_linked_resources', 'unlinked_resources', 'patient_linkage_rate',
        'avg_resources_per_patient',
        'total_fields', 'fields_always_present', 'fields_sometimes_present',
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

            # Patient resources ARE the anchor — treat them as fully linked
            if resource_type == 'Patient':
                linked = stats.total_resources
            else:
                linked = relational.resources_with_patient_ref.get(resource_type, 0)
            unlinked = stats.total_resources - linked
            linkage_rate = f"{linked / stats.total_resources:.4f}" if stats.total_resources > 0 else ''
            avg = round(linked / unique_patients, 2) if unique_patients > 0 else ''

            writer.writerow({
                'resource_type': resource_type,
                'total_resources': stats.total_resources,
                'deduplicated_resources': dedup_counts.get(resource_type, 0) or '',
                'patient_linked_resources': linked,
                'unlinked_resources': unlinked,
                'patient_linkage_rate': linkage_rate,
                'avg_resources_per_patient': avg,
                'total_fields': len(stats.field_stats),
                'fields_always_present': always_present,
                'fields_sometimes_present': sometimes_present,
                'fields_with_type_inconsistency': type_inconsistent
            })

    return output_file


def export_summary_details_csv(
        results: Dict[str, ResourceTypeStatistics],
        output_dir: Path
) -> Path:
    """
    Export a detailed field-level breakdown for the summary categories.

    One row per field per resource type, with its presence category and
    type inconsistency flag — so the user can filter and follow up on
    always-present, sometimes-present, or type-inconsistent fields by name.
    """
    output_file = output_dir / "_summary_details.csv"

    columns = [
        'resource_type',
        'field_path',
        'presence_category',    # always_present / sometimes_present / never_present
        'presence_rate',        # fraction of resources that contain this field
        'resources_with_field',
        'total_resources',
        'has_type_inconsistency',
        'detected_python_types',       # comma-separated list of observed types
    ]

    # Category order for sorting within each resource type
    category_order = {'always_present': 0, 'sometimes_present': 1, 'never_present': 2}

    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()

        for resource_type, stats in sorted(results.items()):
            rows = []
            for path, fs in stats.field_stats.items():
                # Determine presence category
                if stats.total_resources > 0 and fs.resource_count == stats.total_resources:
                    category = 'always_present'
                elif fs.resource_count > 0:
                    category = 'sometimes_present'
                else:
                    category = 'never_present'

                presence_rate = (
                    fs.resource_count / stats.total_resources
                    if stats.total_resources > 0 else 0.0
                )

                rows.append({
                    'resource_type': resource_type,
                    'field_path': path,
                    'presence_category': category,
                    'presence_rate': f"{presence_rate:.4f}",
                    'resources_with_field': fs.resource_count,
                    'total_resources': stats.total_resources,
                    'has_type_inconsistency': fs.has_type_inconsistency,
                    'detected_python_types': ', '.join(fs.detected_python_types),
                })

            # Sort: category order first, then field path alphabetically
            rows.sort(key=lambda r: (category_order[r['presence_category']], r['field_path']))
            writer.writerows(rows)

    return output_file


def export_all_results(
        results: Dict[str, ResourceTypeStatistics],
        relational: RelationalAnalyzer,
        dedup_counts: Counter,
        output_dir: Path,
        verbose: bool = True
) -> Dict[str, Path]:
    """Export all analysis results."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_files = {}

    # Field-level profiles
    if verbose:
        print("\nExporting field profiles...")
    for resource_type, stats in results.items():
        output_file = export_resource_type_csv(resource_type, stats, output_dir)
        output_files[resource_type] = output_file
        if verbose:
            print(f"  {resource_type}: {len(stats.field_stats)} fields")

    # Summary (counts) + detailed breakdown (field names per category)
    unique_patients = len(relational.known_ids.get('Patient', set()))
    output_files['_summary'] = export_summary_csv(results, output_dir, relational, dedup_counts, unique_patients)
    output_files['_summary_details'] = export_summary_details_csv(results, output_dir)
    if verbose:
        total_fields = sum(len(s.field_stats) for s in results.values())
        print(f"  Summary details: {total_fields} fields across {len(results)} resource types")

    # Cardinality analysis
    if verbose:
        print("\nExporting relational analysis...")

    cardinality = relational.compute_cardinality_stats()
    output_files['_cardinality'] = export_cardinality_csv(cardinality, relational, output_dir)
    if verbose:
        print(f"  Cardinality: {len(cardinality)} resource types")

    # Data quality
    quality = relational.compute_data_quality_scores()
    output_files['_data_quality'] = export_data_quality_csv(quality, output_dir)
    if verbose:
        print(f"  Data quality: {len(quality)} resource types")

    # Reference integrity
    integrity = relational.compute_reference_integrity()
    output_files['_reference_integrity'] = _export_analysis_txt(
        "REFERENCE INTEGRITY ANALYSIS", integrity, "_reference_integrity.txt", output_dir
    )
    if verbose:
        print(f"  Reference integrity: {integrity['total_references']} references analysed")

    # Temporal analysis
    temporal = relational.compute_temporal_stats()
    output_files['_temporal'] = _export_analysis_txt(
        "TEMPORAL ANALYSIS", temporal, "_temporal_analysis.txt", output_dir
    )
    if verbose:
        print(f"  Temporal: {temporal['resources_with_dates']} dated resources")

    # Structural analysis
    structural = relational.compute_structural_stats()
    output_files['_structural'] = _export_analysis_txt(
        "STRUCTURAL DEPTH ANALYSIS", structural, "_structural_analysis.txt", output_dir
    )
    if verbose:
        print(f"  Structural depth: {len(structural['depth_by_type'])} resource types")

    return output_files


# =============================================================================
# MAIN PROFILER
# =============================================================================

def run_profiler(config: ProfilerConfig) -> dict:
    """Run the FHIR dataset profiler with relational analysis."""
    start_time = time.time()
    verbose = config.verbose

    folder_name = config.run_name if config.run_name else datetime.now().strftime("%Y%m%d_%H_%M")
    timestamped_output_dir = config.output_dir / folder_name

    if not _check_output_writable(config.output_dir):
        return {'error': 'Output directory not writable'}

    timestamped_output_dir.mkdir(parents=True, exist_ok=True)

    if verbose:
        print("=" * 70)
        print("FHIR Server Profiler - Enhanced with Relational Analysis")
        print(f"Server: {config.server_base_url}")
        print("=" * 70)

    # Verify server is reachable before doing anything else
    session = make_session(config.auth_token)
    if not check_server(session, config.server_base_url, verbose):
        return {'error': 'Server unreachable'}

    fetch_errors: List[str] = []
    total_patients = fetch_patient_count(session, config.server_base_url)
    effective_limit = config.patient_limit or total_patients  # None if both unknown

    if verbose:
        count_str = f"{total_patients:,}" if total_patients is not None else "unknown"
        limit_str = f" (limited to first {config.patient_limit:,})" if config.patient_limit else ""
        print(f"\nPatients on server: {count_str}{limit_str}")
        if config.request_delay > 0:
            print(f"Request delay: {config.request_delay}s between patients")

    if total_patients == 0:
        print("No patients found on server!")
        return {'error': 'No patients found'}

    aggregator = Aggregator(config)
    if not tracemalloc.is_tracing():
        tracemalloc.start()

    if verbose:
        print(f"\nFetching and processing patients...")

    for idx, patient_id in enumerate(
            fetch_patient_ids(session, config.server_base_url, config.page_size, config.max_retries), 1):
        try:
            for resource in fetch_patient_resources(
                    session, config.server_base_url, patient_id,
                    config.page_size, fetch_errors, config.max_retries):
                aggregator.process_resource(resource)
            aggregator.mark_file_processed()

            if verbose and (idx % 10 == 0 or idx == effective_limit):
                elapsed_so_far = time.time() - start_time
                pat_per_sec = idx / elapsed_so_far if elapsed_so_far > 0 else 0
                eta_sec = (effective_limit - idx) / pat_per_sec if (pat_per_sec > 0 and effective_limit) else 0
                _, peak_bytes = tracemalloc.get_traced_memory()
                summary = aggregator.get_summary()
                print(f"  [{idx}/{effective_limit or '?'}] "
                      f"Resources: {summary['total_resources']:,} | "
                      f"{pat_per_sec:.1f} pat/s | "
                      f"ETA: {eta_sec / 60:.1f} min | "
                      f"Peak RAM: {peak_bytes / 1024 / 1024:.0f} MB")

            if config.patient_limit and idx >= config.patient_limit:
                if verbose:
                    print(f"\n  Patient limit of {config.patient_limit:,} reached — stopping.")
                break

            if config.request_delay > 0:
                time.sleep(config.request_delay)

        except Exception as e:
            fetch_errors.append(f"Fatal error processing Patient/{patient_id}: {e}")

    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    peak_mb = peak_bytes / 1024 / 1024

    # Get results
    results = aggregator.get_results()
    relational = aggregator.get_relational_results()
    dedup_counts = aggregator.get_dedup_counts()
    summary = aggregator.get_summary()

    if verbose:
        print(f"\n" + "-" * 50)
        print("Processing Complete!")
        print(f"  Patients processed: {summary['files_processed']:,}")
        print(f"  Total resources:    {summary['total_resources']:,}")
        print(f"  Resource types:     {len(summary['resource_types'])}")
        print(f"  Unique patients:    {len(relational.known_ids.get('Patient', set())):,}")
        print(f"  Unique encounters:  {len(relational.known_ids.get('Encounter', set())):,}")
        print(f"  Peak RAM:           {peak_mb:.0f} MB")
        print("-" * 50)

    # Export all results
    if verbose:
        print(f"\nExporting results to: {timestamped_output_dir}")

    output_files = export_all_results(results, relational, dedup_counts, timestamped_output_dir, verbose)

    # Write errors
    all_errors = aggregator.errors + fetch_errors
    if all_errors:
        error_file = timestamped_output_dir / "_errors.txt"
        with open(error_file, 'w', encoding='utf-8') as f:
            for error in all_errors:
                f.write(f"{error}\n")
        if verbose:
            print(f"\n  {len(all_errors)} error(s) written to: {error_file.name}")

    elapsed = time.time() - start_time

    if verbose:
        print(f"\nDone! Elapsed: {elapsed:.2f}s | Peak RAM: {peak_mb:.0f} MB")
        print(f"\nOutput files:")
        for name, path in sorted(output_files.items()):
            print(f"  {path.name}")

    return {
        'patients_processed': summary['files_processed'],
        'total_resources': summary['total_resources'],
        'resource_types': summary['resource_types'],
        'unique_patients': len(relational.known_ids.get('Patient', set())),
        'unique_encounters': len(relational.known_ids.get('Encounter', set())),
        'peak_ram_mb': round(peak_mb, 1),
        'output_files': output_files,
        'elapsed_seconds': elapsed,
        'error_count': len(all_errors)
    }


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

def \
        main():
    parser = argparse.ArgumentParser(
        description='Profile a FHIR R4 server via $everything for all patients.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python profiler_server.py http://localhost:8080/fhir ./output
    python profiler_server.py --token mytoken http://localhost:8080/fhir ./output
    python profiler_server.py --no-relations http://localhost:8080/fhir ./output
        """
    )

    parser.add_argument('server_url', type=str, help='Base URL of the FHIR server')
    parser.add_argument('output_dir', type=Path, help='Directory for output files')
    parser.add_argument('--token', type=str, default=None, help='Bearer token for authentication')
    parser.add_argument('--limit', type=int, default=None, help='Stop after N patients (for test runs)')
    parser.add_argument('--request-delay', type=float, default=0.0, help='Seconds to wait between patients (default: 0)')
    parser.add_argument('--max-retries', type=int, default=3, help='Retries for transient server errors (default: 3)')
    parser.add_argument('--page-size', type=int, default=100, help='Resources per page (default: 100)')
    parser.add_argument('--top-n', type=int, default=20, help='Top values to track (default: 20)')
    parser.add_argument('--hll-precision', type=int, default=14, help='HyperLogLog precision (default: 14)')
    parser.add_argument('--hll-threshold', type=int, default=10000, help='HLL switch threshold (default: 10000)')
    parser.add_argument('--no-inline-extensions', action='store_true', help='Don\'t inline extension URLs')
    parser.add_argument('--no-relations', action='store_true', help='Skip relational analysis')
    parser.add_argument('--skip-types', type=str, default='',
                        help='Comma-separated resource types to exclude (e.g. Binary,DocumentReference)')
    parser.add_argument('--run-name', type=str, default=None,
                        help='Custom name for the output subfolder (default: timestamp)')
    parser.add_argument('--quiet', '-q', action='store_true', help='Suppress output')

    args = parser.parse_args()

    config = ProfilerConfig(
        server_base_url=args.server_url,
        output_dir=args.output_dir,
        auth_token=args.token,
        patient_limit=args.limit,
        request_delay=args.request_delay,
        max_retries=args.max_retries,
        page_size=args.page_size,
        top_values_limit=args.top_n,
        hll_precision=args.hll_precision,
        hll_threshold=args.hll_threshold,
        inline_extensions=not args.no_inline_extensions,
        analyze_relations=not args.no_relations,
        skip_types={t.strip() for t in args.skip_types.split(',') if t.strip()},
        run_name=args.run_name,
        verbose=not args.quiet
    )

    outcome = run_profiler(config)
    sys.exit(0 if outcome.get('error') is None else 1)


if __name__ == '__main__':
    # For quick testing - hardcode your values here
    SERVER_URL = "http://localhost:8080/fhir"  # ← Change this when necessary
    OUTPUT_DIR = Path("/Users/carlabhc/Documents/Python Projects/FHIR_Dataset_Profiling/Output")  # ← Change this when necessary

    result = run_profiler(
        ProfilerConfig(server_base_url=SERVER_URL, output_dir=OUTPUT_DIR, verbose=True)
    )

    print(f"\nResults: {result}")
