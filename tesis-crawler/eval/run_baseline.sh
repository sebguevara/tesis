#!/usr/bin/env bash
# Helper para correr el eval baseline contra el backend local.
# Asume que:
#   - El backend está corriendo en http://127.0.0.1:8000
#   - El crawl ya terminó (med.unne.edu.ar)
#   - .env tiene OPENAI_API_KEY y WIDGET_DEV_API_KEY

set -euo pipefail

cd "$(dirname "$0")"

# Carga vars relevantes desde ../.env
ENV_FILE="../.env"
if [[ -f "$ENV_FILE" ]]; then
    export OPENAI_API_KEY=$(grep -E "^OPENAI_API_KEY=" "$ENV_FILE" | cut -d= -f2- | tr -d '"')
    export API_KEY=$(grep -E "^WIDGET_DEV_API_KEY=" "$ENV_FILE" | cut -d= -f2- | tr -d '"')
fi

export BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"

if [[ -z "${SOURCE_ID:-}" ]]; then
    SOURCE_ID=$(curl -s -m 10 "$BASE_URL/api/sources/lookup?domain=med.unne.edu.ar" \
        | python -c "import json,sys; print(json.load(sys.stdin)['source_id'])")
fi
export SOURCE_ID

LABEL="${1:-00_baseline}"
RESULTS="results/${LABEL}.json"
SCORED="results/scored_${LABEL}.json"

mkdir -p results

echo "BASE_URL=$BASE_URL"
echo "SOURCE_ID=$SOURCE_ID"
echo "LABEL=$LABEL"
echo "→ run_eval"
python run_eval.py --eval-set eval_set.json --output "$RESULTS"

echo "→ score_eval"
python score_eval.py --results "$RESULTS" --output "$SCORED"

echo "Listo. Resultados en $SCORED"
