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

# Warn if a non-uv meshapi is about to be shadowed.
$existing = Get-Command meshapi -ErrorAction SilentlyContinue
if ($existing -and $existing.Source -notmatch 'uv\\tools|\.local\\bin\\meshapi') {
  Write-Host "Note: found an existing meshapi at $($existing.Source) (not uv-managed). uv will take over the 'meshapi' command." -ForegroundColor Yellow
}

# Install, or upgrade if already present (idempotent).
$installed = (uv tool list 2>$null | Select-String "^$Pkg\s") -ne $null
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

# Launch: unlike POSIX `curl | sh`, `irm | iex` runs this script in-process in
# the console, so a launched meshapi.exe inherits the real console stdin and
# the first-run key prompt works — no /dev/tty reconnect needed.
if ([Environment]::UserInteractive) {
  Write-Host "Launching meshapi... (first run asks for your rsk_ API key)" -ForegroundColor Cyan
  meshapi
} else {
  Write-Host "Installed. Start it any time with:  meshapi" -ForegroundColor Cyan
}
