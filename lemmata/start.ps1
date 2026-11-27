<#
.SYNOPSIS
Start the lemmata math shell: the fork's Codex TUI under the lemmata profile.

.DESCRIPTION
Checks WSL, the ~/.lemmata install, the release binary and the ChatGPT login, points the
WSL install at the main checkout, installs the profiles into the lemmata CODEX_HOME
(%USERPROFILE%\.lemmata), starts every MCP server once to list its tools, then opens the TUI.

Everything runs from the repository's main checkout (C:\lemmata), even when this script is
started from a git worktree, so deleting a worktree never leaves WSL pointing at it.

.EXAMPLE
lemmata\start.cmd             # prover/refuter shell
lemmata\start.cmd -Research   # research profile (web search limited to math sources)
lemmata\start.cmd -Check      # health check only, no shell

.EXAMPLE
wsl -d Ubuntu --cd ~ --exec .lemmata/bin/lemmata-mcp lts search --sort easiest
#>
param(
    [switch]$Research,
    [switch]$Check
)

function Write-Step($text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }
function Write-Ok($text) { Write-Host "  ok   $text" -ForegroundColor Green }
function Write-Note($text) { Write-Host "  ..   $text" -ForegroundColor Yellow }
function Stop-Lemmata($text) {
    Write-Host "  !!   $text" -ForegroundColor Red
    Write-Host ''
    exit 1
}

# --- Main checkout -----------------------------------------------------------------
$here = Split-Path -Parent $PSScriptRoot
$root = $here
if (Get-Command git -ErrorAction SilentlyContinue) {
    $worktrees = git -C $here worktree list --porcelain
    if ($LASTEXITCODE -eq 0 -and $worktrees) {
        $root = ($worktrees[0] -replace '^worktree ', '') -replace '/', '\'
    }
}
$profileName = if ($Research) { 'lemmata-research' } else { 'lemmata' }
$codexHome = Join-Path $env:USERPROFILE '.lemmata'
$codex = Join-Path $root 'codex-rs\target\release\codex.exe'

Write-Host ''
Write-Host "lemmata ($profileName profile)" -ForegroundColor White
Write-Step 'Checkout'
$branch = git -C $root rev-parse --abbrev-ref HEAD
$commit = git -C $root rev-parse --short HEAD
Write-Ok "$root ($branch @ $commit)"
if ($branch -ne 'main') {
    Write-Note "$root is on '$branch', not main. To use main: git -C $root switch main"
}

# --- Prerequisites ----------------------------------------------------------------
Write-Step 'Prerequisites'
$pins = Join-Path $root 'lemmata\mcp\lemmata_mcp\pins.env'
$distro = (Select-String -Path $pins -Pattern '^WSL_DISTRO=(.+)$').Matches[0].Groups[1].Value
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    Stop-Lemmata 'WSL is not installed. Install it with: wsl --install -d Ubuntu-24.04'
}
wsl.exe -d $distro --exec true
if ($LASTEXITCODE -ne 0) {
    Stop-Lemmata "WSL distro '$distro' does not start. Check: wsl -l -v"
}
Write-Ok "WSL distro $distro"

$wslHere = wsl.exe -d $distro --exec wslpath -a $here
$wslRoot = wsl.exe -d $distro --exec wslpath -a $root
wsl.exe -d $distro --cd '~' --exec bash "$wslHere/lemmata/mcp/start-check.sh" "$wslRoot/lemmata/mcp" |
    ForEach-Object { Write-Host "  $_" }
if ($LASTEXITCODE -ne 0) { Stop-Lemmata 'Fix the WSL install as shown above, then run this again.' }

if (-not (Test-Path $codex)) {
    Stop-Lemmata ("No release build at $codex. Build it (about 20 minutes):`n" +
        "         cd $root\codex-rs; cargo build --release -j 4 -p codex-cli --bin codex")
}
$built = (Get-Item $codex).LastWriteTime
$lastRust = git -C $root log -1 --format=%ct -- codex-rs
if ($lastRust -and [DateTimeOffset]::FromUnixTimeSeconds([long]$lastRust).LocalDateTime -gt $built) {
    Write-Note 'codex.exe is older than the last codex-rs commit. Rebuild it if Rust changed.'
}
Write-Ok "codex.exe built $($built.ToString('yyyy-MM-dd HH:mm'))"

# --- Config -----------------------------------------------------------------------
Write-Step "Config ($codexHome)"
$env:CODEX_HOME = $codexHome
# Never let an API key reach this Codex: lemmata runs on the ChatGPT login only.
Remove-Item Env:OPENAI_API_KEY, Env:CODEX_API_KEY -ErrorAction SilentlyContinue
$work = Join-Path $codexHome 'work'
New-Item -ItemType Directory -Force $codexHome, $work | Out-Null
$config = Join-Path $root 'lemmata\config'
Copy-Item (Join-Path $config '*.config.toml'), (Join-Path $config '*.md') $codexHome -Force
$baseConfig = Join-Path $codexHome 'config.toml'
if (-not (Test-Path $baseConfig)) {
    # Plain `codex login` reads only config.toml, not the profiles.
    Set-Content $baseConfig 'forced_login_method = "chatgpt"' -Encoding ascii
}
Write-Ok 'installed lemmata and lemmata-research profiles'

# --- Login ------------------------------------------------------------------------
Write-Step 'ChatGPT login'
# codex prints status on stderr; PS 5.1 wraps 2>&1 lines as ErrorRecords, so stringify each line.
function Get-LoginStatus {
    $eap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $out = (& $codex login status 2>&1 | ForEach-Object { "$_" }) -join "`n" } finally { $ErrorActionPreference = $eap }
    $state = if ($LASTEXITCODE -ne 0 -or $out -match 'Not logged in') { 'none' }
        elseif ($out -match 'Logged in using ChatGPT') { 'chatgpt' }
        elseif ($out -match 'API key') { 'apikey' } else { 'other' }
    [pscustomobject]@{ State = $state; Text = $out.Trim() }
}
$login = Get-LoginStatus
if ($login.State -eq 'chatgpt') {
    Write-Ok 'Logged in using ChatGPT'
} elseif ($login.State -ne 'none') {
    Stop-Lemmata ("$codexHome has a non-ChatGPT login. lemmata uses the ChatGPT login only:`n" +
        "         `$env:CODEX_HOME='$codexHome'; & '$codex' logout   then run this again")
} elseif ($Check) {
    Write-Note 'Not logged in yet. Run start.cmd without -Check to log in with ChatGPT.'
} else {
    Write-Note 'Not logged in. A browser window opens: sign in with your ChatGPT account.'
    Write-Note '(Plan login only. Do not create an API key; lemmata never uses one.)'
    & $codex login -c forced_login_method=chatgpt
    $login = Get-LoginStatus
    if ($login.State -ne 'chatgpt') { Stop-Lemmata "Login did not finish ($($login.Text))." }
    Write-Ok 'Logged in using ChatGPT'
}

# --- MCP health check -------------------------------------------------------------
Write-Step 'MCP servers (start each one, list its tools)'
$eap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
$smoke = wsl.exe -d $distro --cd '~' --exec .lemmata/bin/lemmata-mcp smoke 2>&1 | ForEach-Object { "$_" }
$smokeOk = $LASTEXITCODE -eq 0
$ErrorActionPreference = $eap
foreach ($line in $smoke) {
    if ("$line" -match '^(\w+): (\d+) tools$') { Write-Ok "$($Matches[1]): $($Matches[2]) tools" }
    elseif (-not $smokeOk) { Write-Host "       $line" }
}
if (-not $smokeOk) { Stop-Lemmata 'An MCP server failed to start (output above).' }

if ($Check) {
    Write-Host ''
    Write-Host 'Health check passed.' -ForegroundColor Green
    exit 0
}

# --- Shell ------------------------------------------------------------------------
Write-Step 'Starting the lemmata shell'
Write-Host '  Type a claim, e.g.: Prove that for every natural number n, n + 0 = n.'
Write-Host '  Answers end in PROVED, DISPROVED or UNRESOLVED. Ctrl+C twice quits.'
Write-Host ''
& $codex --profile $profileName --cd $work
exit $LASTEXITCODE
