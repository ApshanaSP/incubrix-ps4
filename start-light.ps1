# Light mode: the whole service with no Docker, no database server and no real accounts.
# One Python process: API + mock platforms in-process, SQLite and files under .\data.
#
#   powershell -ExecutionPolicy Bypass -File start-light.ps1          # service on http://localhost:8080/docs
#   powershell -ExecutionPolicy Bypass -File start-light.ps1 -Test    # run every test instead
param([switch]$Test, [int]$Port = 8080)

Set-Location $PSScriptRoot
$env:SIMULATION_MODE = "true"
$env:DATA_DIR = Join-Path $PSScriptRoot "data"
$env:PORT = "$Port"
# download links, feed URLs and evidence links must point at this port
if (-not $env:PUBLIC_BASE_URL) { $env:PUBLIC_BASE_URL = "http://localhost:$Port" }
$env:PYTHONDONTWRITEBYTECODE = "1"
# use the Windows certificate store, so HTTPS checks work behind antivirus that re-signs HTTPS (Avast and others)
if (-not $env:USE_SYSTEM_CERTS) { $env:USE_SYSTEM_CERTS = "true" }
# keep pip's cache next to the project instead of on C:
$env:PIP_CACHE_DIR = Join-Path $PSScriptRoot ".pip-cache"

python -c "import fastapi, uvicorn, httpx, yaml, multipart, dotenv, cryptography, bs4, langdetect, boto3" 2>$null
if ($LASTEXITCODE -ne 0) { python -m pip install -r requirements-dev.txt }

if ($Test) { python -m pytest -q -p no:cacheprovider } else { python -m app.serve }
