#!/usr/bin/env bash
# Exportiert ein Comparison-Notebook als HTML-Report.
# Nutzt Quarto wenn verfügbar, sonst nbconvert als Fallback.
#
# Verwendung:
#   ./export_report.sh                   — diz_comparison.ipynb, aktueller Stand
#   ./export_report.sh --run             — diz_comparison.ipynb, neu ausführen
#   ./export_report.sh overview          — diz_comparison_overview.ipynb
#   ./export_report.sh overview --run    — diz_comparison_overview.ipynb, neu ausführen

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPORT_DIR="$SCRIPT_DIR/../results"

# Notebook auswählen
if [[ "${1:-}" == "overview" ]]; then
    NOTEBOOK="$SCRIPT_DIR/diz_comparison_overview.ipynb"
    OUTFILE="FHIRScan_DIZ_Uebersicht_$(date +%Y%m%d_%H%M)"
    shift
else
    NOTEBOOK="$SCRIPT_DIR/diz_comparison.ipynb"
    OUTFILE="FHIRScan_DIZ_Vergleich_$(date +%Y%m%d_%H%M)"
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
