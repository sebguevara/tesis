# Evalúa un sitio de punta a punta: genera preguntas desde su corpus (si no
# existen), las corre contra la API local y las puntúa con el juez.
#   .\eval\run_site.ps1 -Domain arq.unne.edu.ar -Tag 06_generic [-EvalSet eval\eval_set.json]
param(
    [Parameter(Mandatory = $true)][string]$Domain,
    [Parameter(Mandatory = $true)][string]$Tag,
    [string]$EvalSet = "",
    [int]$N = 60
)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$env:PYTHONIOENCODING = "utf-8"; $env:PYTHONUTF8 = "1"; $env:PYTHONPATH = "."
Get-Content .env | Where-Object { $_ -match '^(OPENAI_API_KEY|DB_URL)=' } | ForEach-Object {
    $k, $v = $_ -split '=', 2; Set-Item "env:$k" $v.Trim('"')
}
$sourceId = (docker exec tesis_db psql -U tesis -d tesis -At -c "select source_id from sources where domain='$Domain'").Trim()
if (-not $sourceId) { throw "No hay source para $Domain" }
$short = $Domain.Split('.')[0]
if (-not $EvalSet) {
    $EvalSet = "eval\sets\$short.json"
    if (-not (Test-Path $EvalSet)) { .\.venv\Scripts\python.exe eval/gen_eval_set.py --domain $Domain --n $N --output $EvalSet }
}
$run = "eval\results\${short}_$Tag.json"
.\.venv\Scripts\python.exe eval/run_eval.py --eval-set $EvalSet --output $run --source-id $sourceId --api-key pfc_sk_local_demo_univ_2026_001 --timeout 90
.\.venv\Scripts\python.exe eval/score_eval.py --results $run --output "eval\results\scored_${short}_$Tag.json"
