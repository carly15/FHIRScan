#!/usr/bin/env bash
# Exportiert diz_comparison.ipynb als HTML-Report (ohne Code-Zellen).
#
# Verwendung:
#   ./export_report.sh          — exportiert den aktuell gespeicherten Stand
#   ./export_report.sh --run    — führt das Notebook neu aus, dann exportiert

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPORT_DIR="$SCRIPT_DIR/../results"
NOTEBOOK="$SCRIPT_DIR/diz_comparison.ipynb"
OUTFILE="FHIRScan_DIZ_Vergleich_$(date +%Y%m%d_%H%M)"
TMP_NOTEBOOK="$(mktemp).ipynb"

mkdir -p "$REPORT_DIR"

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
            # Erstes Chart lädt Plotly.js per CDN, alle weiteren nutzen es direkt
            html = pio.to_html(fig, include_plotlyjs='cdn' if first else False,
                               full_html=False)
            data['text/html'] = html
            first = False

with open(dst, 'w') as f:
    json.dump(nb, f)
PYEOF

TAGS="--TagRemovePreprocessor.enabled=True --TagRemovePreprocessor.remove_cell_tags=remove_cell"

if [[ "${1:-}" == "--run" ]]; then
    echo "Führe Notebook aus und exportiere..."
    jupyter nbconvert \
        --to html \
        --execute \
        --no-input \
        $TAGS \
        --output-dir "$REPORT_DIR" \
        --output "$OUTFILE" \
        "$NOTEBOOK"
else
    echo "Exportiere aktuellen Notebook-Stand..."
    jupyter nbconvert \
        --to html \
        --no-input \
        $TAGS \
        --output-dir "$REPORT_DIR" \
        --output "$OUTFILE" \
        "$TMP_NOTEBOOK"
fi

rm -f "$TMP_NOTEBOOK"
echo "Report gespeichert: $REPORT_DIR/${OUTFILE}.html"
