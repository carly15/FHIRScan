#!/usr/bin/env bash
# Exportiert den DIZ-Bericht (oder eines der alten Comparison-Notebooks) als HTML.
#
# Verwendung:
#   ./export_report.sh --run             — diz_report.ipynb ausführen: HTML + Excel in results/<YYYYMMDD_HHMM>/
#   ./export_report.sh                   — diz_report.ipynb, gespeicherter Stand (nur HTML, kein Excel)
#   ./export_report.sh v2 [--run]        — diz_report_v2.ipynb: zusätzlich je Kategorie (Läufe mit FHIRScan v2)
#   ./export_report.sh katalog [--run]   — data_catalog.ipynb: Datenkatalog (nur lokal)
#   ./export_report.sh alt [--run]       — altes diz_comparison.ipynb
#   ./export_report.sh overview [--run]  — altes diz_comparison_overview.ipynb
#   ./export_report.sh all [--run]       — beide alten Reports, gemeinsam in results/<YYYYMMDD_HHMM>/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPORT_DIR="$SCRIPT_DIR/../results"
cd "$SCRIPT_DIR"   # Quarto löst Ressourcenpfade relativ zum Arbeitsverzeichnis auf
export FHIRSCAN_SKIP_CSV=1   # Notebooks schreiben beim Export keine CSV-Ordner nach results/

# ── DIZ-Bericht (Standard) bzw. v2 mit Aufschlüsselung nach Kategorie ─────
if [[ "${1:-}" != "alt" && "${1:-}" != "overview" && "${1:-}" != "all" ]]; then
    BERICHT="diz_report.ipynb"
    NAME="FHIRScan_DIZ_Bericht"
    if [[ "${1:-}" == "v2" ]]; then
        BERICHT="diz_report_v2.ipynb"
        NAME="FHIRScan_DIZ_Bericht_v2"
        shift
    fi
    if [[ "${1:-}" == "katalog" ]]; then
        BERICHT="data_catalog.ipynb"
        NAME="FHIRScan_Datenkatalog"
        shift
    fi
    STAMP="$(date +%Y%m%d_%H%M)"
    OUT="$REPORT_DIR/$STAMP"
    mkdir -p "$OUT"
    export FHIRSCAN_OUT_DIR="$OUT" FHIRSCAN_STAMP="$STAMP"   # Zielordner für die Excel-Tabellen
    if [[ "${1:-}" == "--run" ]]; then
        EXEC=(--execute --no-execute-daemon)
    else
        EXEC=(--no-execute)
    fi
    quarto render "$BERICHT" --to html "${EXEC[@]}" \
        --output "${NAME}_${STAMP}.html" --output-dir "$OUT"
    echo "Bericht gespeichert in: $OUT"
    exit 0
fi

# Beide Reports erzeugen und in einen Ordner mit dem späteren Zeitstempel verschieben
if [[ "${1:-}" == "all" ]]; then
    shift
    "$SCRIPT_DIR/export_report.sh" alt "$@"
    "$SCRIPT_DIR/export_report.sh" overview "$@"
    VERGLEICH="$(ls -t "$REPORT_DIR"/FHIRScan_DIZ_Vergleich_*.html | head -1)"
    UEBERSICHT="$(ls -t "$REPORT_DIR"/FHIRScan_DIZ_Uebersicht_*.html | head -1)"
    STAMP="$(basename "$VERGLEICH" .html | grep -o '[0-9]\{8\}_[0-9]\{4\}$')"
    STAMP2="$(basename "$UEBERSICHT" .html | grep -o '[0-9]\{8\}_[0-9]\{4\}$')"
    [[ "$STAMP2" > "$STAMP" ]] && STAMP="$STAMP2"
    mkdir -p "$REPORT_DIR/$STAMP"
    mv "$VERGLEICH" "$UEBERSICHT" "$REPORT_DIR/$STAMP/"
    echo "Beide Reports gespeichert in: $REPORT_DIR/$STAMP"
    exit 0
fi

# Notebook auswählen
if [[ "${1:-}" == "overview" ]]; then
    NOTEBOOK="$SCRIPT_DIR/diz_comparison_overview.ipynb"
    OUTFILE="FHIRScan_DIZ_Uebersicht_$(date +%Y%m%d_%H%M)"
    shift
else
    NOTEBOOK="$SCRIPT_DIR/diz_comparison.ipynb"
    OUTFILE="FHIRScan_DIZ_Vergleich_$(date +%Y%m%d_%H%M)"
    shift   # „alt“
fi

EXECUTE="${1:-}"
mkdir -p "$REPORT_DIR"

# ── Quarto (bevorzugt) ────────────────────────────────────────────────────
if command -v quarto &>/dev/null; then
    echo "Renderer: Quarto $(quarto --version)"
    if [[ "$EXECUTE" == "--run" ]]; then
        echo "Führe Notebook aus und exportiere..."
        quarto render "$NOTEBOOK" \
            --to html \
            --execute --no-execute-daemon \
            --output "${OUTFILE}.html" \
            --output-dir "$REPORT_DIR"
    else
        echo "Exportiere aktuellen Notebook-Stand..."
        quarto render "$NOTEBOOK" \
            --to html \
            --no-execute \
            --output "${OUTFILE}.html" \
            --output-dir "$REPORT_DIR"
    fi

    # Quarto's embed-resources inlines the full plotly.js (~5 MB) once per
    # chart instead of once per page. Strip all but the first copy.
    python3 - "$REPORT_DIR/${OUTFILE}.html" << 'PYEOF'
import re, sys

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    html = f.read()

pattern = re.compile(r'<script>/\*\*\s*\*\s*plotly\.js v[\d.]+.*?</script>', re.DOTALL)
matches = list(pattern.finditer(html))
if len(matches) > 1:
    keep = matches[0].group(0)
    html = pattern.sub(lambda m: keep if m.start() == matches[0].start() else "", html)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Plotly.js dedupliziert: {len(matches)} → 1 Kopie")
PYEOF

# ── nbconvert (Fallback bis Quarto installiert ist) ───────────────────────
else
    echo "Renderer: nbconvert (Fallback — 'brew install quarto' für besseres Output)"
    TMP_NOTEBOOK="$(mktemp).ipynb"

    # Plotly-JSON-Outputs mit HTML anreichern, damit nbconvert sie rendern kann.
    python3 - "$NOTEBOOK" "$TMP_NOTEBOOK" << 'PYEOF'
import sys, json
import plotly.graph_objects as go
import plotly.io as pio

src, dst = sys.argv[1], sys.argv[2]
with open(src) as f:
    nb = json.load(f)

first = True
for cell in nb['cells']:
    for out in cell.get('outputs', []):
        data = out.get('data', {})
        if 'application/vnd.plotly.v1+json' in data and 'text/html' not in data:
            fig = go.Figure(data['application/vnd.plotly.v1+json'])
            html = pio.to_html(fig, include_plotlyjs='cdn' if first else False,
                               full_html=False)
            data['text/html'] = html
            first = False

with open(dst, 'w') as f:
    json.dump(nb, f)
PYEOF

    TAGS="--TagRemovePreprocessor.enabled=True --TagRemovePreprocessor.remove_cell_tags=remove_cell"

    if [[ "$EXECUTE" == "--run" ]]; then
        echo "Führe Notebook aus und exportiere..."
        jupyter nbconvert \
            --to html --execute --no-input $TAGS \
            --output-dir "$REPORT_DIR" \
            --output "$OUTFILE" \
            "$NOTEBOOK"
    else
        echo "Exportiere aktuellen Notebook-Stand..."
        jupyter nbconvert \
            --to html --no-input $TAGS \
            --output-dir "$REPORT_DIR" \
            --output "$OUTFILE" \
            "$TMP_NOTEBOOK"
    fi

    rm -f "$TMP_NOTEBOOK"
fi

echo "Report gespeichert: $REPORT_DIR/${OUTFILE}.html"
