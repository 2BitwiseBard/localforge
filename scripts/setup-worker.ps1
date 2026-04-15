# LocalForge Windows Worker Bootstrap
# Run as the current user (NOT Administrator unless installing NSSM into Program Files).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File setup-worker.ps1 -Hub http://ai-hub:8100 -Token <enrollment-token>
#
# What this does:
#   1. Checks / installs Python 3.11+ via winget
#   2. Creates a venv at $env:LOCALAPPDATA\LocalForge\venv
#   3. pip-installs localforge (editable from git for now — switch to PyPI once published)
#   4. Calls POST /api/mesh/register, stores the returned worker API key 0600-equivalent
#   5. Downloads NSSM, registers "LocalForgeWorker" Windows service
#   6. Starts the service
#
# Firewall note: the worker listens on :8200 and should only be reachable over
# the Tailscale interface. Add a rule restricting inbound to the Tailscale NIC.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Hub,

    [Parameter(Mandatory = $true)]
    [string]$Token,

    [int]$Port = 8200,

    [string]$InstallDir = "$env:LOCALAPPDATA\LocalForge",

    [string]$GitRepo = "https://github.com/bitwisebard/localforge"
)

$ErrorActionPreference = "Stop"

function Write-Step { param([string]$Msg) Write-Host "[+] $Msg" -ForegroundColor Cyan }
function Write-Warn { param([string]$Msg) Write-Host "[!] $Msg" -ForegroundColor Yellow }
function Write-Err  { param([string]$Msg) Write-Host "[x] $Msg" -ForegroundColor Red }

Write-Host "=== LocalForge Windows Worker Setup ===" -ForegroundColor Green
Write-Host "Hub:        $Hub"
Write-Host "Port:       $Port"
Write-Host "Install to: $InstallDir"
Write-Host ""

# --- 1. Python ------------------------------------------------------------
Write-Step "Checking Python 3.11+"
$pythonExe = $null
foreach ($candidate in @("python", "python3", "py")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($ver -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            if ($major -ge 3 -and $minor -ge 11) { $pythonExe = $candidate; break }
        }
    } catch { }
}
if (-not $pythonExe) {
    Write-Warn "Python 3.11+ not found. Attempting winget install..."
    winget install --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    $pythonExe = "python"
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
}
Write-Host "    Python: $(& $pythonExe --version)"

# --- 2. Venv + install ----------------------------------------------------
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$venv = Join-Path $InstallDir "venv"
if (-not (Test-Path $venv)) {
    Write-Step "Creating venv at $venv"
    & $pythonExe -m venv $venv
}
$venvPy = Join-Path $venv "Scripts\python.exe"
$venvPip = Join-Path $venv "Scripts\pip.exe"

Write-Step "Installing localforge[worker] into venv"
& $venvPip install --upgrade pip | Out-Null
# Until localforge is on PyPI, install directly from the repo
& $venvPip install "git+$GitRepo#egg=localforge[worker]"

# --- 3. Hardware detect + register ---------------------------------------
Write-Step "Detecting hardware"
$hwJson = & $venvPy -c "import json; from localforge.workers.detect import detect; print(json.dumps(detect().to_dict()))"
$hw = $hwJson | ConvertFrom-Json
Write-Host "    Platform: $($hw.platform)"
Write-Host "    GPU:      $($hw.gpu_name) ($($hw.gpu_type))"
Write-Host "    VRAM:     $($hw.vram_mb) MB"
Write-Host "    RAM:      $($hw.ram_mb) MB"
Write-Host "    Tier:     $($hw.tier)"

Write-Step "Registering worker with hub"
$registerBody = @{
    enrollment_token = $Token
    hostname         = $env:COMPUTERNAME
    platform         = "win32"
    hardware         = $hw
} | ConvertTo-Json -Depth 5

try {
    $registerResp = Invoke-RestMethod -Uri "$Hub/api/mesh/register" -Method POST `
                                      -Body $registerBody -ContentType "application/json"
} catch {
    Write-Err "Registration failed: $_"
    exit 1
}

$workerKey = $registerResp.api_key
$workerId  = $registerResp.worker_id
Write-Host "    Registered as: $workerId"

# --- 4. Persist env file (ACL restricted) --------------------------------
$envFile = Join-Path $InstallDir "env.ps1"
@"
`$env:LOCALFORGE_HUB_URL = '$Hub'
`$env:LOCALFORGE_API_KEY = '$workerKey'
`$env:LOCALFORGE_WORKER_PORT = '$Port'
"@ | Set-Content -Path $envFile -Encoding UTF8

Write-Step "Restricting env file ACL to current user"
$acl = Get-Acl $envFile
$acl.SetAccessRuleProtection($true, $false)   # disable inheritance
$acl.Access | ForEach-Object { $acl.RemoveAccessRule($_) } | Out-Null
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    "$env:USERDOMAIN\$env:USERNAME", "FullControl", "Allow"
)
$acl.AddAccessRule($rule)
Set-Acl -Path $envFile -AclObject $acl

# --- 5. NSSM -------------------------------------------------------------
$nssmExe = Join-Path $InstallDir "nssm.exe"
if (-not (Test-Path $nssmExe)) {
    Write-Step "Downloading NSSM"
    $tmp = Join-Path $env:TEMP "nssm.zip"
    Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile $tmp
    Expand-Archive -Path $tmp -DestinationPath $env:TEMP -Force
    $arch = if ([Environment]::Is64BitOperatingSystem) { "win64" } else { "win32" }
    Copy-Item (Join-Path $env:TEMP "nssm-2.24\$arch\nssm.exe") $nssmExe
    Remove-Item $tmp
}

$serviceName = "LocalForgeWorker"
$existing = & $nssmExe status $serviceName 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Step "Reconfiguring existing $serviceName service"
    & $nssmExe stop $serviceName | Out-Null
} else {
    Write-Step "Installing $serviceName service"
    & $nssmExe install $serviceName $venvPy "-m" "localforge.workers.device_worker" "--port" "$Port" "--hub" "$Hub"
}

& $nssmExe set $serviceName AppEnvironmentExtra "LOCALFORGE_API_KEY=$workerKey" "LOCALFORGE_HUB_URL=$Hub"
& $nssmExe set $serviceName Start SERVICE_AUTO_START
& $nssmExe set $serviceName AppStdout (Join-Path $InstallDir "worker.out.log")
& $nssmExe set $serviceName AppStderr (Join-Path $InstallDir "worker.err.log")
& $nssmExe set $serviceName AppRotateFiles 1
& $nssmExe set $serviceName AppRotateBytes 10485760   # 10 MB

Write-Step "Starting $serviceName"
& $nssmExe start $serviceName | Out-Null

Start-Sleep -Seconds 2
$status = & $nssmExe status $serviceName
Write-Host "    Service status: $status"

# --- 6. Firewall hint ----------------------------------------------------
Write-Host ""
Write-Warn "FIREWALL: Ensure inbound TCP $Port is ONLY allowed on the Tailscale interface."
Write-Host "  New-NetFirewallRule -DisplayName 'LocalForge Worker' -Direction Inbound ```
             -Action Allow -Protocol TCP -LocalPort $Port ```
             -InterfaceAlias 'Tailscale'"

Write-Host ""
Write-Host "=== Setup Complete ===" -ForegroundColor Green
Write-Host "Logs:   $InstallDir\worker.*.log"
Write-Host "Stop:   $nssmExe stop $serviceName"
Write-Host "Start:  $nssmExe start $serviceName"
Write-Host "Status: curl http://localhost:$Port/health"
Write-Host "Hub:    $Hub/api/mesh/status (should show $workerId within 30s)"
