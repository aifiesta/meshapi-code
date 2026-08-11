# meshapi installer  —  Windows (PowerShell)
#
#   powershell -ExecutionPolicy ByPass -c "irm https://cli.meshapi.ai/install.ps1 | iex"
#
# Installs uv (a single binary that brings its own Python), then
# `uv tool install meshapi-code`, fixes PATH, and launches meshapi. Nothing
# needs to be preinstalled — no Python, no pipx. Re-running upgrades an
# existing install.
#
# Source: https://github.com/aifiesta/meshapi-code/blob/main/install.ps1
$ErrorActionPreference = 'Stop'

$Pkg = 'meshapi-code'
$UvVersion = '0.12.2'   # pinned for supply-chain safety; bump deliberately

function Add-ToSessionPath($dir) {
  if ((Test-Path $dir) -and (($env:Path -split ';') -notcontains $dir)) {
    $env:Path = "$dir;$env:Path"
  }
}

# uv + its tool shims land in %USERPROFILE%\.local\bin on current releases;
# older uv used \.cargo\bin. Put both on PATH for this session.
$binDirs = @("$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin")
foreach ($d in $binDirs) { Add-ToSessionPath $d }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "Installing uv (Python bootstrapper)..." -ForegroundColor Cyan
  # Pinned, HTTPS; ByPass applies only to this child process.
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/$UvVersion/install.ps1 | iex"
  foreach ($d in $binDirs) { Add-ToSessionPath $d }
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw "uv is installed but not on PATH - open a new PowerShell window and re-run this command."
}

# Detect a non-uv meshapi ANYWHERE on PATH (warned after install). Get-Command
# without -All returns only the first match — on an upgrade that's our own uv
# copy, which hides an older pip copy that still wins in new shells. Scan all.
$shadowPath = $null
foreach ($m in @(Get-Command meshapi -All -ErrorAction SilentlyContinue)) {
  # Only real executables — Get-Command -All also returns aliases/functions
  # whose empty .Source would pass the -notmatch and stop the scan early.
  if ($m.CommandType -eq 'Application' -and $m.Source `
      -and ($m.Source -notmatch 'uv\\tools|\.local\\bin|\.cargo\\bin')) {
    $shadowPath = $m.Source; break
  }
}

# Install, or upgrade if already present (idempotent).
$installed = $null -ne (uv tool list 2>$null | Select-String "^$Pkg\s")
if ($installed) {
  Write-Host "Upgrading $Pkg..." -ForegroundColor Cyan
  uv tool upgrade $Pkg
} else {
  Write-Host "Installing $Pkg..." -ForegroundColor Cyan
  uv tool install $Pkg
}

# Persist PATH for future shells, refresh this one.
uv tool update-shell | Out-Null
foreach ($d in $binDirs) { Add-ToSessionPath $d }
if (-not (Get-Command meshapi -ErrorAction SilentlyContinue)) {
  throw "meshapi not found after install."
}
Write-Host "OK: $(meshapi --version)" -ForegroundColor Green

# Launch only when a human is actually at an interactive console — the mirror of
# the POSIX `[ -t 1 ]` guard. [Environment]::UserInteractive is TRUE even in CI,
# so it can't stand alone: also require a non-redirected stdin and no CI marker,
# else meshapi's first-run key prompt hits a non-tty and exits 1, failing install.
try {
  $canLaunch = [Environment]::UserInteractive `
    -and (-not [Console]::IsInputRedirected) `
    -and (-not $env:CI)
} catch {
  # [Console]::IsInputRedirected can throw with no valid console handle; a
  # launch-gate probe must never fail an already-successful install.
  $canLaunch = $false
}
if ($shadowPath) {
  # A shadow copy makes 'meshapi' in a new shell run the OLD version, so don't
  # launch into a copy the user can't re-launch — explain the fix instead.
  Write-Host ""
  Write-Host "WARNING: another meshapi is EARLIER on your PATH and will win:" -ForegroundColor Yellow
  Write-Host "    $shadowPath" -ForegroundColor Yellow
  Write-Host "  Your 'meshapi' command runs that OLD copy (symptom: 'No API key found')." -ForegroundColor Yellow
  Write-Host "  Remove it (pip uninstall meshapi-code from the Python that owns it)," -ForegroundColor Yellow
  Write-Host "  reopen PowerShell, then run:  meshapi" -ForegroundColor Yellow
} elseif ($canLaunch) {
  Write-Host "Launching meshapi... (first run asks for your rsk_ API key)" -ForegroundColor Cyan
  meshapi
} else {
  Write-Host "Installed. Start it any time with:  meshapi" -ForegroundColor Cyan
}
