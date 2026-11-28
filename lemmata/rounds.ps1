<#
.SYNOPSIS
Chain headless Astra rounds on one frozen challenge until a stop rule fires.

.DESCRIPTION
Each round is one `codex exec --profile lemmata` run that forks the previous round's thread
(or starts fresh with -Fresh). Per round N the runner writes, under -OutDir:
  <Name>-r<N>-prompt.txt        the round prompt (UTF-8, no BOM)
  <Name>-r<N>-launch.cmd        the exact cmd launch, rerunnable by hand
  <Name>-r<N>.log               codex JSONL output
  <Name>-r<N>-last-message.txt  the final agent message
and appends one record per round to <Name>-run.jsonl. Runner messages go to <Name>-runner.log.

Stop rules: an accepted PROVED/DISPROVED (verdict), -NoProgressRounds consecutive rounds without
goal-tree progress (no-progress), -MaxRounds (max-rounds), -TimeCapHours (time-cap), or the same
round failing twice (failed). Progress and the verdict come from `lemmata-mcp goals snapshot`
(goal tree). Without the goals CLI, progress is unknown (never counts as no progress) and the
verdict is read from the round log's accepted submit_verdict result.

Runs only on the ChatGPT login in %USERPROFILE%\.lemmata; API keys are removed from the
environment, and the runner stops if the login is not ChatGPT.

.EXAMPLE
lemmata\rounds.cmd -Challenge 5f8c55c0733a5f9e -Name giuga -Fork 01a21f84-1721-7ad1-a3b4-33090053de2e -MaxRounds 3

.EXAMPLE
lemmata\rounds.cmd -Challenge <id> -Name even -MaxRounds 2 -DryRun   # write round 1 files, launch nothing
#>
param(
    [Parameter(Mandatory)][string]$Challenge,
    [Parameter(Mandatory)][string]$Name,
    [string]$Fork,
    [switch]$Fresh,
    [int]$MaxRounds = 10,
    [int]$NoProgressRounds = 2,
    [double]$TimeCapHours = 12,
    [double]$RoundCapHours = 4,
    [string]$Model = 'gpt-6.2-astra',
    [string]$Effort = 'max',
    [string]$OutDir = 'C:\Users\jaden\lemmata-run',
    [string]$PromptTemplate,
    [string]$FirstPrompt,
    [string]$WebhookEnv = 'C:\Users\jaden\lemmata-run\t3_webhook.env',
    [switch]$Resume,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
# wsl.exe passes the Linux program's UTF-8 bytes through; decode them as UTF-8 (blocker 4).
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$utf8 = New-Object Text.UTF8Encoding($false)

# These values land in a cmd line: allow only plain tokens.
foreach ($pair in @(@('Challenge', $Challenge), @('Name', $Name), @('Model', $Model), @('Effort', $Effort))) {
    if ($pair[1] -notmatch '^[A-Za-z0-9._-]+$') { throw "-$($pair[0]) must match [A-Za-z0-9._-]+" }
}
$idPattern = '^[0-9a-fA-F-]{36}$'
if ($Fork -and $Fork -notmatch $idPattern) { throw '-Fork must be a thread id (uuid)' }
if ($Fork -and $Fresh) { throw 'Use -Fork or -Fresh, not both' }
foreach ($file in @($PromptTemplate, $FirstPrompt)) {
    if ($file -and -not (Test-Path -LiteralPath $file)) { throw "No such file: $file" }
}
New-Item -ItemType Directory -Force $OutDir | Out-Null
$runLog = Join-Path $OutDir "$Name-runner.log"
$runJsonl = Join-Path $OutDir "$Name-run.jsonl"

function Write-Log($text) {
    $line = "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) $text"
    Write-Host $line
    [IO.File]::AppendAllText($runLog, "$line`r`n", $utf8)
}

# --- Main checkout, WSL distro, codex (as start.ps1) -------------------------------
$here = Split-Path -Parent $PSScriptRoot
$root = $here
if (Get-Command git -ErrorAction SilentlyContinue) {
    $worktrees = git -C $here worktree list --porcelain
    if ($LASTEXITCODE -eq 0 -and $worktrees) {
        $root = ($worktrees[0] -replace '^worktree ', '') -replace '/', '\'
    }
}
$pins = Join-Path $root 'lemmata\mcp\lemmata_mcp\pins.env'
$distro = (Select-String -Path $pins -Pattern '^WSL_DISTRO=(.+)$').Matches[0].Groups[1].Value
$codex = Join-Path $root 'codex-rs\target\release\codex.exe'
if (-not (Test-Path $codex)) { throw "No release build at $codex (see start.ps1)" }
$codexHome = Join-Path $env:USERPROFILE '.lemmata'
$work = Join-Path $codexHome 'work'
$env:CODEX_HOME = $codexHome
# Never let an API key reach this Codex: lemmata runs on the ChatGPT login only.
Remove-Item Env:OPENAI_API_KEY, Env:CODEX_API_KEY -ErrorAction SilentlyContinue

# Read-only status check; it never logs in or out.
$ErrorActionPreference = 'Continue'
$loginText = (& $codex login status 2>&1 | ForEach-Object { "$_" }) -join "`n"
$ErrorActionPreference = 'Stop'
if ($loginText -notmatch 'Logged in using ChatGPT') {
    throw "$codexHome is not on the ChatGPT login. Run lemmata\start.cmd first."
}

# --- WSL side: lemmata-mcp goals (goal tree CLI) ------------------------------------
# Returns stdout lines, or $null when the command fails or does not exist yet.
function Invoke-Goals([string[]]$GoalArgs) {
    $ErrorActionPreference = 'Continue'
    $out = & wsl.exe -d $distro --cd '~' --exec .lemmata/bin/lemmata-mcp goals @GoalArgs 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    , @($out | ForEach-Object { "$_" })
}

function Get-Snapshot([int]$N, [string]$Phase, [string]$Thread) {
    $goalArgs = @('snapshot', $Challenge, '--round', "$N", '--phase', $Phase)
    if ($Thread) { $goalArgs += @('--thread', $Thread) }
    $out = Invoke-Goals $goalArgs
    $line = if ($out) { $out | Where-Object { $_.Trim() } | Select-Object -Last 1 }
    if (-not $line) { return $null }
    try { $line | ConvertFrom-Json } catch { $null }
}

$templateWsl = $null
if ($PromptTemplate) {
    $templateWsl = (& wsl.exe -d $distro --exec wslpath -a ((Resolve-Path -LiteralPath $PromptTemplate).Path) | Select-Object -First 1)
}

# Used only when `goals prompt` is unavailable. {problem} and {round} are filled in.
$fallbackPrompt = @'
Round {round} on challenge {problem}. Work on challenge {problem}. Frozen statement unchanged; same certificate rules.
Read the whiteboard first and build on what earlier rounds recorded; do not re-derive or restate known lemmas.
This round must make Lean-checked progress: prove or refute a new lemma that narrows the open cases.
End with `submit_verdict` (UNRESOLVED citing lemma ids, or PROVED/DISPROVED with a certificate).
'@

function Get-RoundPrompt([int]$N) {
    if ($N -eq 1 -and $FirstPrompt) { return [IO.File]::ReadAllText((Resolve-Path -LiteralPath $FirstPrompt).Path) }
    $goalArgs = @('prompt', $Challenge, '--round', "$N")
    if ($templateWsl) { $goalArgs += @('--template', $templateWsl) }
    $out = Invoke-Goals $goalArgs
    if ($null -ne $out) { return ($out -join "`n") }
    Write-Log "goals prompt unavailable; using the built-in round prompt"
    $text = if ($PromptTemplate) { [IO.File]::ReadAllText((Resolve-Path -LiteralPath $PromptTemplate).Path) } else { $fallbackPrompt }
    $text.Replace('{problem}', $Challenge).Replace('{round}', "$N")
}

# --- Round log parsing (blockers 5 and 6) ------------------------------------------
function Read-RoundLog([string]$Log) {
    $result = @{ thread = $null; completed = $false; verdict = $null }
    if (-not (Test-Path $Log)) { return $result }
    foreach ($line in [IO.File]::ReadLines($Log)) {
        if (-not $result.thread -and $line.Contains('"type":"thread.started"')) {
            try { $result.thread = ($line | ConvertFrom-Json).thread_id } catch { }
        } elseif ($line.StartsWith('{"type":"turn.completed"')) {
            $result.completed = $true
        } elseif ($line.StartsWith('{"type":"item.completed"') -and $line.Contains('"tool":"submit_verdict"')) {
            # Only the verdict server's own result counts; `error` items are ignored.
            try { $item = ($line | ConvertFrom-Json).item } catch { continue }
            $sc = $item.result.structured_content
            if ($item.server -eq 'verdict' -and $sc.accepted -eq $true -and $sc.verdict -in 'PROVED', 'DISPROVED') {
                $result.verdict = [pscustomobject]@{ verdict = $sc.verdict; accepted = $true; reason = $sc.reason }
            }
        }
    }
    if ($result.thread -and $result.thread -notmatch $idPattern) { $result.thread = $null }
    $result
}

# --- Webhook (never prints the key or headers) -------------------------------------
function Send-Webhook([string]$ThreadId, [string]$Status) {
    if ($DryRun) { Write-Log "dry run: would post webhook $ThreadId $Status"; return }
    if (-not (Test-Path -LiteralPath $WebhookEnv)) { Write-Log 'no webhook env file; webhook skipped'; return }
    try {
        $e = Get-Content -LiteralPath $WebhookEnv
        $u = ($e | Where-Object { $_ -like 'URL=*' } | Select-Object -First 1).Substring(4)
        $k = ($e | Where-Object { $_ -like 'KEY=*' } | Select-Object -First 1).Substring(4)
        $body = @{ threadId = $ThreadId; status = $Status; pr = '' } | ConvertTo-Json
        Invoke-RestMethod -Method Post -Uri $u -Headers @{ Authorization = "Bearer $k" } -ContentType 'application/json' -Body $body | Out-Null
        Write-Log "webhook posted ($Status)"
    } catch {
        Write-Log "webhook failed: $($_.Exception.Message)"
    } finally {
        Remove-Variable k -ErrorAction SilentlyContinue
    }
}

# --- Resume ------------------------------------------------------------------------
$round = 1
$forkFrom = $Fork
$noProgress = 0
if ($Resume) {
    if (-not (Test-Path $runJsonl)) { throw "Nothing to resume: $runJsonl not found" }
    $records = @([IO.File]::ReadAllLines($runJsonl, $utf8) | Where-Object { $_.Trim() } |
        ForEach-Object { $_ | ConvertFrom-Json } | Where-Object { $_.round })
    $last = $records | Where-Object { $_.thread_id } | Select-Object -Last 1
    if (-not $last) { throw "No round with a thread id in $runJsonl" }
    $round = [int]$last.round + 1
    $forkFrom = $last.thread_id
    foreach ($r in $records) {
        if ($r.snapshot -and $r.snapshot.progress -eq $false) { $noProgress++ }
        elseif ($r.snapshot -and $r.snapshot.progress -eq $true) { $noProgress = 0 }
    }
    Write-Log "resuming at round $round from thread $forkFrom ($noProgress no-progress rounds so far)"
}
if ($Fresh) { $forkFrom = $null }

# --- Loop --------------------------------------------------------------------------
$deadline = (Get-Date).AddHours($TimeCapHours)
$failures = 0
$status = $null
Write-Log "run $Name on $Challenge (max $MaxRounds rounds, no-progress $NoProgressRounds, cap ${TimeCapHours}h, round cap ${RoundCapHours}h, $(if ($Fresh) { 'fresh' } else { 'fork' }) mode)"

while (-not $status) {
    if ($round -gt $MaxRounds) { $status = 'max-rounds'; break }
    $remaining = ($deadline - (Get-Date)).TotalMilliseconds
    if ($remaining -le 0) { $status = 'time-cap'; break }
    $roundMs = [Math]::Min($RoundCapHours * 3600000, $remaining)
    $cappedByTotal = $roundMs -lt $RoundCapHours * 3600000

    $base = Join-Path $OutDir "$Name-r$round"
    $promptFile = "$base-prompt.txt"
    $launch = "$base-launch.cmd"
    $log = "$base.log"
    $lastMessage = "$base-last-message.txt"

    if (-not $DryRun) { $null = Get-Snapshot $round 'begin' $forkFrom }
    [IO.File]::WriteAllText($promptFile, (Get-RoundPrompt $round), $utf8)

    # The proven redirection from the hand-made rounds: codex reads the prompt file on stdin.
    $forkPart = if ($forkFrom) { " fork $forkFrom" } else { '' }
    $cmdLines = @(
        '@echo off'
        'setlocal'
        "set CODEX_HOME=$codexHome"
        'set OPENAI_API_KEY='
        'set CODEX_API_KEY='
        "cd /d `"$work`""
        "echo.>> `"$log`""
        "echo ===== ROUND_LAUNCH r$round START_LOCAL=%DATE% %TIME% =====>> `"$log`""
        "`"$codex`" exec --profile lemmata --model $Model -c model_reasoning_effort=$Effort --cd `"$work`" --skip-git-repo-check --color never --json -o `"$lastMessage`"$forkPart - < `"$promptFile`" >> `"$log`" 2>&1"
        'set RC=%ERRORLEVEL%'
        "echo ROUND_EXIT_CODE=%RC% END_LOCAL=%DATE% %TIME%>> `"$log`""
        'exit %RC%'
    )
    [IO.File]::WriteAllText($launch, ($cmdLines -join "`r`n") + "`r`n", $utf8)

    if ($DryRun) {
        Write-Log "dry run: round $round prompt $promptFile"
        Write-Log "dry run: launch $launch"
        Write-Host ($cmdLines -join "`n")
        $status = 'dry-run'
        break
    }

    $started = Get-Date
    Write-Log "round $round start$(if ($forkFrom) { " (fork $forkFrom)" } else { ' (new session)' }), cap $([Math]::Round($roundMs / 3600000, 2))h"
    $proc = Start-Process cmd.exe -ArgumentList '/c', "`"$launch`"" -PassThru -NoNewWindow
    $null = $proc.Handle  # PS 5.1 needs the handle cached for ExitCode
    $killed = $false
    try {
        if (-not $proc.WaitForExit([int][Math]::Min($roundMs, [int]::MaxValue))) {
            & taskkill.exe /PID $proc.Id /T /F | Out-Null
            $proc.WaitForExit()
            $killed = $true
            [IO.File]::AppendAllText($log, "ROUND_KILLED $((Get-Date).ToString('o'))`r`n", $utf8)
            Write-Log "ROUND_KILLED round $round after the time cap"
        }
    } finally {
        # Ctrl+C or an error in the runner must not leave codex running.
        if (-not $proc.HasExited) { & taskkill.exe /PID $proc.Id /T /F | Out-Null }
    }
    $exitCode = $proc.ExitCode
    $parsed = Read-RoundLog $log
    $snapshot = Get-Snapshot $round 'end' $parsed.thread
    $verdict = if ($snapshot -and $snapshot.verdict) { $snapshot.verdict } else { $parsed.verdict }
    $failed = -not $killed -and $exitCode -ne 0 -and -not $parsed.completed

    $record = [ordered]@{
        round = $round; thread_id = $parsed.thread; fork_from = $forkFrom; exit_code = $exitCode
        killed = $killed; turn_completed = $parsed.completed; failed = $failed
        started = $started.ToString('o'); ended = (Get-Date).ToString('o')
        snapshot = $snapshot; log_verdict = $parsed.verdict
    }
    [IO.File]::AppendAllText($runJsonl, (ConvertTo-Json $record -Depth 8 -Compress) + "`n", $utf8)
    $progress = if ($snapshot) { $snapshot.progress } else { $null }
    Write-Log "round $round end: exit $exitCode, killed $killed, turn.completed $($parsed.completed), thread $($parsed.thread), progress $(if ($null -eq $progress) { 'unknown' } else { $progress })"

    if ($failed) {
        $failures++
        if ($failures -ge 2) { $status = 'failed'; break }
        Write-Log "round $round failed; retrying once"
        continue
    }
    $failures = 0
    if ($verdict -and $verdict.accepted -and $verdict.verdict -in 'PROVED', 'DISPROVED') {
        Write-Log "accepted $($verdict.verdict): $($verdict.reason)"
        $status = 'verdict'; break
    }
    if ($killed -and $cappedByTotal) { $status = 'time-cap'; break }
    if ($progress -eq $true) { $noProgress = 0 }
    elseif ($progress -eq $false) { $noProgress++ }
    if ($NoProgressRounds -gt 0 -and $noProgress -ge $NoProgressRounds) { $status = 'no-progress'; break }
    if ($round -ge $MaxRounds) { $status = 'max-rounds'; break }

    Send-Webhook "$Name-r$round" 'round-done'
    if (-not $Fresh -and $parsed.thread) { $forkFrom = $parsed.thread }
    $round++
}

$lastRound = [Math]::Min($round, $MaxRounds)
if (-not $DryRun) {
    [IO.File]::AppendAllText($runJsonl, (ConvertTo-Json ([ordered]@{ final = $status; last_round = $lastRound; ended = (Get-Date).ToString('o') }) -Compress) + "`n", $utf8)
}
Write-Log "stopped: $status (round $lastRound)"
Send-Webhook "$Name-r$lastRound" $status
if ($status -eq 'failed') { exit 1 }
