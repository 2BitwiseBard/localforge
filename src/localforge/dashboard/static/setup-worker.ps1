# LocalForge Windows Worker Bootstrap
# Run as the current user (NOT Administrator unless installing NSSM into Program Files).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File setup-worker.ps1 -Hub http://ai-hub:8100 -Token <enrollment-token>
#
# What this does:
#   1. Checks / installs Python 3.11+ via winget
#   2. Creates a venv at $env:LOCALAPPDATA\LocalForge\venv
#   3. pip-installs localforge[worker] from the public git repo
#   4. Detects hardware + calls POST /api/mesh/register (enrollment token)
#   5. Downloads llama-server.exe (llama.cpp latest) + a VRAM-sized default
#      GGUF so the worker can host chat on its own GPU (-SkipModel to skip;
#      -Model <path> to use an existing GGUF)
#   6. Writes ACL-restricted env file, registers "LocalForgeWorker" NSSM service
#   7. Starts the service (service auto-starts llama-server when --model set)
#
# Firewall note: the worker listens on :8200 and should only be reachable over
# the Tailscale interface. Add a rule restricting inbound to the Tailscale NIC.

[CmdletBinding()]
param(
    # When served via /api/mesh/install-script, the hub & token placeholders
    # below are substituted server-side so the one-liner can stay
    # `iwr URL | iex` with no args. Env-var fallbacks help manual runs.
    [string]$Hub = "%%LOCALFORGE_HUB_URL%%",
    [string]$Token = "%%LOCALFORGE_ENROLLMENT_TOKEN%%",
    [int]$Port = 8200,
    [int]$LlamaPort = 5050,
    [string]$InstallDir = "$env:LOCALAPPDATA\LocalForge",
    [string]$GitRepo = "https://github.com/2BitwiseBard/localforge",
    # Local inference stack: downloads llama-server.exe + a right-sized default
    # GGUF so the worker can host chat/completions on its own GPU. Precedence
    # (first non-empty wins): -Model (existing GGUF path), -ModelId (catalog id
    # from src/localforge/models_catalog.py), -ModelTier (tiny/small/medium/
    # large/xl -> TIER_DEFAULTS), else pick_for_vram() chooses the largest model
    # whose min_vram fits. -SkipModel downgrades to task-receiver only.
    [string]$Model = "",
    [string]$ModelId = "",
    [ValidateSet("", "tiny", "small", "medium", "large", "xl")]
    [string]$ModelTier = "",
    [switch]$SkipModel,
    [ValidateSet("auto", "vulkan", "cuda", "cpu")]
    [string]$LlamaBackend = "auto"
)

# If placeholders weren't substituted (manual run), try env-var fallback.
if ($Hub -like '%%*%%') { $Hub = $env:LOCALFORGE_HUB_URL }
if ($Token -like '%%*%%') { $Token = $env:LOCALFORGE_ENROLLMENT_TOKEN }

$ErrorActionPreference = "Stop"

# Force TLS 1.2 globally -- PowerShell 5.1 on older Windows 10 defaults to
# TLS 1.0/1.1 which most HTTPS endpoints (including Tailscale certs) reject.
# Must happen early, before any Invoke-RestMethod or Invoke-WebRequest call.
try {
    [Net.ServicePointManager]::SecurityProtocol = `
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

# Elevated windows spawned via Start-Process -Verb RunAs close IMMEDIATELY
# on `exit N` (which bypasses PowerShell's `trap` handler). Three defenses so
# the user can always see what happened:
#   1. Log under $env:ProgramData (not $env:TEMP) -- when UAC prompts for
#      admin credentials rather than consent, the elevated shell runs under
#      a DIFFERENT user whose $env:TEMP is elsewhere, so the parent can't
#      find the log. ProgramData is the same absolute path for all users.
#   2. Write a sentinel file BEFORE Start-Transcript so the parent can
#      distinguish "script never ran" from "script ran but transcript failed".
#   3. Register a PowerShell.Exiting engine event -- unlike `trap`, this
#      DOES fire on `exit N`, giving us a reliable pause.
$script:LogDir = Join-Path $env:ProgramData "LocalForge"
$script:TranscriptPath = Join-Path $script:LogDir "setup.log"
$script:SentinelPath = Join-Path $script:LogDir "setup.started"
try {
    New-Item -ItemType Directory -Force -Path $script:LogDir -ErrorAction Stop | Out-Null
} catch { }
try {
    "started $(Get-Date -Format o) as $env:USERDOMAIN\$env:USERNAME pid=$PID" |
        Out-File -FilePath $script:SentinelPath -Encoding UTF8 -ErrorAction Stop
} catch { }
try {
    if (Test-Path $script:TranscriptPath) { Remove-Item -Force $script:TranscriptPath }
    Start-Transcript -Path $script:TranscriptPath -Force | Out-Null
} catch {
    Write-Host "Warning: Start-Transcript failed ($_). Continuing without log." -ForegroundColor Yellow
}

Write-Host "Log: $script:TranscriptPath" -ForegroundColor DarkGray

# Validate required params. Supports either -Hub/-Token args or env vars
# (LOCALFORGE_HUB_URL / LOCALFORGE_ENROLLMENT_TOKEN) so the script works
# both when run via args and when piped through iex.
if ([string]::IsNullOrWhiteSpace($Hub)) {
    Write-Host "Error: -Hub not provided and LOCALFORGE_HUB_URL env var not set." -ForegroundColor Red
    exit 1
}
if ([string]::IsNullOrWhiteSpace($Token)) {
    Write-Host "Error: -Token not provided and LOCALFORGE_ENROLLMENT_TOKEN env var not set." -ForegroundColor Red
    exit 1
}

function Write-Step { param([string]$Msg) Write-Host "[+] $Msg" -ForegroundColor Cyan }
function Write-Warn { param([string]$Msg) Write-Host "[!] $Msg" -ForegroundColor Yellow }
function Write-Err  { param([string]$Msg) Write-Host "[x] $Msg" -ForegroundColor Red }

# nssm.cc + github.com are old-school HTTP and sometimes trip TLS negotiation
# on stock PowerShell 5. Force TLS 1.2, use -UseBasicParsing (avoids spawning
# the IE engine), and fall back to curl.exe if IWR still fails. Shared by the
# llama-server, model, and NSSM download steps so they all benefit.
function Invoke-Download {
    param([string]$Url, [string]$OutFile, [int]$TimeoutSec = 300)
    try {
        [Net.ServicePointManager]::SecurityProtocol = `
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch { }
    try {
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing -TimeoutSec $TimeoutSec
        return $true
    } catch {
        Write-Warn "Invoke-WebRequest failed: $($_.Exception.Message). Trying curl.exe."
    }
    $curl = (Get-Command curl.exe -ErrorAction SilentlyContinue)
    if ($curl) {
        & curl.exe -fsSL --retry 3 --retry-delay 2 -o $OutFile $Url
        if ($LASTEXITCODE -eq 0 -and (Test-Path $OutFile)) { return $true }
        Write-Warn "curl.exe also failed (exit $LASTEXITCODE)."
    }
    return $false
}

# Keep the (likely elevated) console window open at the end so users can read
# success/error output before it closes. `trap` does NOT fire on `exit N`,
# so we use PowerShell.Exiting (runs on ANY exit path) as the primary hook,
# and leave `trap` as a belt-and-suspenders for terminating errors.
$script:pauseOnExit = $true
function Invoke-PauseOnExit {
    if (-not $script:pauseOnExit) { return }
    try { Stop-Transcript | Out-Null } catch { }
    try {
        Write-Host ""
        Write-Host "Full log saved to: $script:TranscriptPath" -ForegroundColor Cyan
        Read-Host "Press Enter to close"
    } catch { }
}
try {
    Register-EngineEvent -SourceIdentifier PowerShell.Exiting `
        -Action { Invoke-PauseOnExit } | Out-Null
} catch { }
trap { Invoke-PauseOnExit; break }

# Admin check: NSSM must register a Windows service, which requires Administrator.
# If we aren't elevated, bail with a clear message -- the Add Node one-liner is
# responsible for triggering UAC via Start-Process -Verb RunAs before we run.
$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($currentIdentity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Err "This installer must run as Administrator (required to register the"
    Write-Err "LocalForgeWorker service via NSSM)."
    Write-Host ""
    Write-Host "Fix: in the dashboard's Add Node modal, use the updated one-liner." -ForegroundColor Yellow
    Write-Host "It will save this script to a temp file and relaunch it elevated," -ForegroundColor Yellow
    Write-Host "triggering a single UAC prompt." -ForegroundColor Yellow
    Invoke-PauseOnExit
    exit 1
}

Write-Host "=== LocalForge Windows Worker Setup ===" -ForegroundColor Green
Write-Host "Hub:        $Hub"
Write-Host "Port:       $Port"
Write-Host "Install to: $InstallDir"
Write-Host ""

# --- 1. Python ------------------------------------------------------------
# Must be 3.11+. We probe common launchers AND direct paths because winget
# installs to a per-user path that isn't on PATH until a new shell starts.
function Find-Python311Plus {
    $candidates = @(
        "py -3.12", "py -3.11",
        "python3.12", "python3.11",
        "python", "python3", "py"
    )
    foreach ($cmd in $candidates) {
        try {
            $ver = & cmd /c "$cmd --version 2>&1"
            if ($ver -match "Python (\d+)\.(\d+)") {
                $major = [int]$Matches[1]; $minor = [int]$Matches[2]
                if ($major -eq 3 -and $minor -ge 11) { return $cmd }
            }
        } catch { }
    }
    # Fallback: scan known winget install locations
    $wingetPaths = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "C:\Program Files\Python312\python.exe",
        "C:\Program Files\Python311\python.exe"
    )
    foreach ($p in $wingetPaths) {
        if (Test-Path $p) { return "`"$p`"" }
    }
    return $null
}

Write-Step "Checking Python 3.11+"
$pythonExe = Find-Python311Plus
if (-not $pythonExe) {
    Write-Warn "Python 3.11+ not found. Installing Python 3.12 via winget..."
    winget install --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements --silent
    # winget doesn't refresh current-shell PATH; probe the known install path.
    $pythonExe = Find-Python311Plus
    if (-not $pythonExe) {
        Write-Err "winget installed Python but we still can't find a 3.11+ launcher."
        Write-Err "Open a NEW PowerShell (Admin) window and rerun this script."
        exit 1
    }
}
Write-Host "    Python launcher: $pythonExe"
& cmd /c "$pythonExe --version"

# --- 2. Venv + install ----------------------------------------------------
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$venv = Join-Path $InstallDir "venv"
$venvPy = Join-Path $venv "Scripts\python.exe"

# If an existing venv is pinned to Python <3.11, nuke and recreate.
# localforge requires >=3.11 (pyproject.toml); prior failed runs may have
# baked an older interpreter into the venv before we upgraded detection.
$needsCreate = -not (Test-Path $venvPy)
if (-not $needsCreate) {
    $venvVer = & $venvPy --version 2>&1
    if ($venvVer -match "Python (\d+)\.(\d+)") {
        $vmaj = [int]$Matches[1]; $vmin = [int]$Matches[2]
        if ($vmaj -lt 3 -or ($vmaj -eq 3 -and $vmin -lt 11)) {
            Write-Warn "Existing venv uses $venvVer (need 3.11+). Recreating."
            Remove-Item -Recurse -Force $venv
            $needsCreate = $true
        }
    } else {
        Write-Warn "Could not determine venv Python version. Recreating."
        Remove-Item -Recurse -Force $venv
        $needsCreate = $true
    }
}
if ($needsCreate) {
    Write-Step "Creating venv at $venv"
    # $pythonExe may contain args (e.g. "py -3.12"), so delegate to cmd.
    & cmd /c "$pythonExe -m venv `"$venv`""
    if ($LASTEXITCODE -ne 0) { Write-Err "venv creation failed"; exit 1 }
}

if (-not (Test-Path $venvPy)) {
    Write-Err "venv python missing at $venvPy"
    exit 1
}

Write-Step "Installing localforge[worker] into venv"
# Use `python -m pip` so pip can replace itself without a Windows file lock.
# Install from a zip archive URL (no git dependency -- most Windows boxes
# don't have git installed).  PEP 440 direct-URL syntax.
& $venvPy -m pip install --upgrade pip
$archiveUrl = "$GitRepo/archive/refs/heads/main.zip"
Write-Host "    Installing from: $archiveUrl"
& $venvPy -m pip install "localforge[worker] @ $archiveUrl"
if ($LASTEXITCODE -ne 0) { Write-Err "pip install failed"; exit 1 }

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

# --- 4. llama-server + default model -------------------------------------
# device_worker.py spawns llama-server.exe as a subprocess when --model is
# passed. We stage both here so the NSSM service can start the full stack
# on first boot. Everything lives under $InstallDir so an uninstall is
# rm -rf of that directory plus `nssm remove LocalForgeWorker confirm`.
$llamaDir = Join-Path $InstallDir "llama-server"
$modelsDir = Join-Path $InstallDir "models"

if ($SkipModel) {
    Write-Step "Skipping llama-server + model install (-SkipModel set)"
    Write-Host "    Worker will be a task-receiver only (embeddings/rerank/classify)."
    Write-Host "    Chat tasks routed to this node will proxy back to the hub backend."
    $Model = ""
} else {
    # 4a. Backend auto-select. Vulkan is the safest default -- works on any
    # modern GPU with stock drivers; no CUDA runtime required. CPU fallback
    # when hardware detect found no GPU.
    if ($LlamaBackend -eq "auto") {
        if ($hw.gpu_type -eq "none") { $LlamaBackend = "cpu" }
        else { $LlamaBackend = "vulkan" }
    }
    Write-Host "    llama-server backend: $LlamaBackend"

    # 4b. Download llama-server binary from the latest llama.cpp release.
    # Asset name pattern: llama-b<N>-bin-win-<backend>-x64.zip. We query the
    # release API so we track upstream automatically; Vulkan fallback covers
    # the case where a tag drops the CUDA/HIP asset momentarily.
    $llamaBin = Join-Path $llamaDir "llama-server.exe"
    if (-not (Test-Path $llamaBin)) {
        Write-Step "Fetching llama.cpp latest release metadata"
        $release = $null
        try {
            $release = Invoke-RestMethod -Uri "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest" `
                                         -Headers @{ 'User-Agent' = 'LocalForge-Setup' } `
                                         -UseBasicParsing -TimeoutSec 30
        } catch {
            Write-Err "Could not reach GitHub to list llama.cpp releases: $_"
            exit 1
        }
        $pattern = "*bin-win-$LlamaBackend-x64.zip"
        $asset = $release.assets | Where-Object { $_.name -like $pattern } | Select-Object -First 1
        if (-not $asset) {
            Write-Warn "No '$LlamaBackend' asset in $($release.tag_name). Falling back to vulkan."
            $asset = $release.assets | Where-Object { $_.name -like '*bin-win-vulkan-x64.zip' } | Select-Object -First 1
            $LlamaBackend = "vulkan"
        }
        if (-not $asset) { Write-Err "No usable llama.cpp asset found"; exit 1 }

        $zipPath = Join-Path $env:TEMP $asset.name
        Write-Step "Downloading $($asset.name) ($([math]::Round($asset.size/1MB)) MB)"
        if (-not (Invoke-Download -Url $asset.browser_download_url -OutFile $zipPath)) {
            Write-Err "llama-server download failed."
            exit 1
        }
        New-Item -ItemType Directory -Force -Path $llamaDir | Out-Null
        Expand-Archive -Path $zipPath -DestinationPath $llamaDir -Force
        Remove-Item $zipPath
        if (-not (Test-Path $llamaBin)) {
            # Some release zips nest into a subfolder; hoist the exe up one level.
            $found = Get-ChildItem -Path $llamaDir -Recurse -Filter "llama-server.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($found) { Copy-Item $found.FullName $llamaBin -Force }
        }
        if (-not (Test-Path $llamaBin)) {
            Write-Err "Extracted zip but llama-server.exe not found under $llamaDir"
            exit 1
        }
        Write-Host "    llama-server installed: $llamaBin"
    } else {
        Write-Host "    llama-server already present at $llamaBin"
    }

    # 4c. Pick a default GGUF from the shared catalog (src/localforge/
    # models_catalog.py). Precedence: -Model path (skip download) -> -ModelId
    # (exact catalog id) -> -ModelTier (TIER_DEFAULTS lookup) -> pick_for_vram().
    # Querying Python keeps the bootstrapper honest -- if a model is added or
    # a URL moves, only the catalog changes, not this script.
    if (-not $Model) {
        New-Item -ItemType Directory -Force -Path $modelsDir | Out-Null
        $vram = [int]$hw.vram_mb
        $pickExpr = "pick_for_vram($vram, 'chat')"
        if ($ModelId) {
            # json.dumps to safely embed the user string in the python one-liner.
            $pickExpr = "by_id($(ConvertTo-Json $ModelId -Compress))"
        } elseif ($ModelTier) {
            $pickExpr = "by_id(TIER_DEFAULTS[$(ConvertTo-Json $ModelTier -Compress)])"
        }
        $pickPy = @"
import json
from localforge.models_catalog import by_id, pick_for_vram, TIER_DEFAULTS
m = $pickExpr
if m is None:
    raise SystemExit('catalog lookup returned None (bad --ModelId or --ModelTier)')
print(json.dumps({'id': m['id'], 'name': m['name'], 'filename': m['filename'],
                  'url': m['url'], 'size_gb': m['size_gb']}))
"@
        try {
            $pickJson = & $venvPy -c $pickPy
        } catch {
            Write-Err "Catalog lookup failed: $_"
            exit 1
        }
        if ($LASTEXITCODE -ne 0 -or -not $pickJson) {
            Write-Err "Catalog lookup returned no model. Pass -Model <path>, -ModelId <id>, or fix VRAM detect."
            exit 1
        }
        $pick = $pickJson | ConvertFrom-Json
        Write-Host "    Catalog pick: $($pick.name) (~$($pick.size_gb) GB)"
        $Model = Join-Path $modelsDir $pick.filename
        if (Test-Path $Model) {
            Write-Host "    Default model already present: $Model"
        } else {
            Write-Step "Downloading $($pick.filename) (~$($pick.size_gb) GB, first run only)"
            $tmpModel = "$Model.partial"
            if (-not (Invoke-Download -Url $pick.url -OutFile $tmpModel -TimeoutSec 1800)) {
                Write-Err "Model download failed. Fix network and re-run, or pass -Model <path> with a local GGUF."
                if (Test-Path $tmpModel) { Remove-Item $tmpModel -Force }
                exit 1
            }
            Move-Item -Path $tmpModel -Destination $Model -Force
            Write-Host "    Model installed: $Model"
        }
    } else {
        if (-not (Test-Path $Model)) {
            Write-Err "-Model path does not exist: $Model"
            exit 1
        }
        Write-Host "    Using supplied model: $Model"
    }
}

# --- 5. Persist env file (ACL restricted) --------------------------------
$envFile = Join-Path $InstallDir "env.ps1"
@"
`$env:LOCALFORGE_HUB_URL = '$Hub'
`$env:LOCALFORGE_API_KEY = '$workerKey'
`$env:LOCALFORGE_WORKER_PORT = '$Port'
`$env:LOCALFORGE_MODEL_PATH = '$Model'
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

# --- 6. NSSM -------------------------------------------------------------

$nssmExe = Join-Path $InstallDir "nssm.exe"
if (-not (Test-Path $nssmExe)) {
    Write-Step "Downloading NSSM"
    $tmp = Join-Path $env:TEMP "nssm.zip"
    if (-not (Invoke-Download -Url "https://nssm.cc/release/nssm-2.24.zip" -OutFile $tmp)) {
        Write-Err "Could not download NSSM from nssm.cc."
        Write-Err "Manual fix: download https://nssm.cc/release/nssm-2.24.zip, extract, and place nssm.exe at $nssmExe"
        exit 1
    }
    Expand-Archive -Path $tmp -DestinationPath $env:TEMP -Force
    $arch = if ([Environment]::Is64BitOperatingSystem) { "win64" } else { "win32" }
    Copy-Item (Join-Path $env:TEMP "nssm-2.24\$arch\nssm.exe") $nssmExe
    Remove-Item $tmp
}

# Build the arg list once so install + reconfigure share the same source of
# truth. `--model` is only appended when a GGUF is actually present, so
# -SkipModel cleanly downgrades the worker to task-receiver mode.
$svcArgs = @("-m", "localforge.workers.device_worker",
             "--port", "$Port",
             "--hub",  "$Hub",
             "--llama-port", "$LlamaPort")
if ($Model) {
    $svcArgs += @("--model", $Model)
}

$serviceName = "LocalForgeWorker"
# NSSM's AppParameters is a raw CreateProcess command-line; args with spaces
# need individual double-quoting.
$appParams = ($svcArgs | ForEach-Object {
    if ($_ -match '\s') { "`"$_`"" } else { $_ }
}) -join ' '
$existing = & $nssmExe status $serviceName 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Step "Reconfiguring existing $serviceName service"
    & $nssmExe stop $serviceName | Out-Null
    & $nssmExe set $serviceName Application $venvPy | Out-Null
    & $nssmExe set $serviceName AppParameters $appParams | Out-Null
} else {
    Write-Step "Installing $serviceName service"
    & $nssmExe install $serviceName $venvPy @svcArgs
}

# Env extras. LOCALFORGE_LLAMA_BIN points device_worker at the vendored
# binary without touching the service's inherited PATH -- NSSM's
# AppEnvironmentExtra REPLACES (not appends) any listed var, so setting
# PATH here would shadow System32 and the venv Scripts dir.
# LOCALFORGE_INSTALL_DIR + LOCALFORGE_MODELS_DIR let runtime model downloads
# (POST /models/download from the dashboard) resolve the same on-disk spot
# the bootstrapper seeded; otherwise `_models_dir()` falls back to
# ~/.localforge/models, splitting models across two locations.
$envExtras = @(
    "LOCALFORGE_API_KEY=$workerKey",
    "LOCALFORGE_HUB_URL=$Hub",
    "LOCALFORGE_INSTALL_DIR=$InstallDir",
    "LOCALFORGE_MODELS_DIR=$modelsDir"
)
if (-not $SkipModel) {
    $llamaBin = Join-Path $llamaDir "llama-server.exe"
    if (Test-Path $llamaBin) { $envExtras += "LOCALFORGE_LLAMA_BIN=$llamaBin" }
}
& $nssmExe set $serviceName AppEnvironmentExtra @envExtras
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

# --- 7. Firewall hint ----------------------------------------------------
Write-Host ""
Write-Warn "FIREWALL: Ensure inbound TCP $Port is ONLY allowed on the Tailscale interface."
Write-Host "  New-NetFirewallRule -DisplayName 'LocalForge Worker' -Direction Inbound ```
             -Action Allow -Protocol TCP -LocalPort $Port ```
             -InterfaceAlias 'Tailscale'"
if (-not $SkipModel) {
    Write-Host ""
    Write-Host "llama-server ($LlamaBackend) listens on 127.0.0.1:$LlamaPort -- loopback only, no rule needed."
}

Write-Host ""
Write-Host "=== Setup Complete ===" -ForegroundColor Green
Write-Host "Logs:      $InstallDir\worker.*.log"
if ($Model) { Write-Host "Model:     $Model" }
Write-Host "Stop:      $nssmExe stop $serviceName"
Write-Host "Start:     $nssmExe start $serviceName"
Write-Host "Health:    curl http://localhost:$Port/health"
Write-Host "Hub view:  $Hub/api/mesh/status (expect $workerId within 30s)"
Invoke-PauseOnExit
