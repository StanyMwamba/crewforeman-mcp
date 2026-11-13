#!/usr/bin/env pwsh
# Single Windows CI entry point for crewforeman-mcp.
#
# The GitHub workflow is a thin wrapper: it checks out, installs the matrix
# Python, then runs this script. It mirrors scripts/ci.sh step for step so the
# same gate runs on Windows:
#
#   $env:PYTHON = "C:\path\to\python.exe"; pwsh -NoProfile -File scripts\ci.ps1
#
# PowerShell is used instead of Git Bash so paths, %TEMP% and native exit codes
# stay native (no MSYS path translation). See A-NOTES.md.
#
# No secrets, no network calls of our own, no git writes and no artifact
# upload. A C compiler is required for the test-only fake dsh launcher.

$ErrorActionPreference = 'Stop'
# Native commands report failure through $LASTEXITCODE. Keep their stderr from
# becoming a terminating error (the preference exists only on PowerShell 7.3+;
# assigning it is a harmless ordinary variable on Windows PowerShell 5.1).
$PSNativeCommandUseErrorActionPreference = $false

$utf8 = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONIOENCODING = 'utf-8'

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $RepoRoot

$Python = if ($env:PYTHON) { $env:PYTHON } else { 'python' }
$FakeDir = Join-Path $env:TEMP ("cf-ci-fake-dsh." + $PID)

function Fail {
    param([string]$Message)
    Write-Host "ci: $Message" -ForegroundColor Red
    exit 1
}

try {
    Write-Host "== environment =="
    try {
        & $Python --version
    } catch {
        Fail "python interpreter not found: $Python"
    }
    if ($LASTEXITCODE -ne 0) { Fail "cannot run $Python" }

    Write-Host ("repository:  " + $RepoRoot)
    Write-Host ("interpreter: " + (Get-Command $Python).Source)
    Write-Host ("temp dir:    " + $FakeDir)

    Write-Host "== static CI contract =="
    & $Python tools/ci_hygiene.py
    if ($LASTEXITCODE -ne 0) { Fail "ci_hygiene.py failed" }

    Write-Host "== commit identity gate =="
    $gateBase = $env:GATE_BASE
    if (-not $gateBase) {
        & git rev-parse --verify origin/main *> $null
        if ($LASTEXITCODE -eq 0) { $gateBase = 'origin/main' }
    }
    if ($gateBase) {
        if ($gateBase -eq ('0' * 40)) {
            $gateBase = (& git rev-list --max-parents=0 HEAD).Trim()
        }
        & git cat-file -e ("{0}^{{commit}}" -f $gateBase) *> $null
        if ($LASTEXITCODE -ne 0) {
            $gateBase = (& git rev-list --max-parents=0 HEAD).Trim()
        }
        $savedPythonPath = $env:PYTHONPATH
        try {
            $env:PYTHONPATH = $RepoRoot
            & $Python -m deepseek_mcp.git_identity --repo $RepoRoot `
                --base $gateBase --head HEAD --remote origin
            if ($LASTEXITCODE -ne 0) { Fail "commit identity/content gate failed" }
        } finally {
            $env:PYTHONPATH = $savedPythonPath
        }
    }

    Write-Host "== install package =="
    & $Python -m pip install --disable-pip-version-check --no-input .
    if ($LASTEXITCODE -ne 0) { Fail "pip install . failed" }

    Write-Host "== build test fake dsh =="
    New-Item -ItemType Directory -Force -Path $FakeDir | Out-Null
    & $Python tests/build_fake_dsh.py (Join-Path $FakeDir 'fake_dsh.py')
    if ($LASTEXITCODE -ne 0) {
        Fail "fake dsh build failed (Visual Studio C++ tools are required)"
    }

    Write-Host "== sandbox probe (require) =="
    $probeOutput = & $Python tests/sandbox_probe.py --require --json
    $probeRc = $LASTEXITCODE
    $probeOutput | ForEach-Object { Write-Host $_ }
    Write-Host "note: the probe JSON records admin (process-token elevation) and admin_member (Administrators group)."
    if ($probeRc -ne 0) { Fail "no enforced OS sandbox on this host" }

    # Exact documented gate command (docs/OPERATIONS.md, "Running the full
    # suite"). The variables are scoped to this command and restored before the
    # boundary run so the boundary is never in "OS sandbox disabled" mode.
    $suiteRc = 0
    $boundaryRc = 0
    $savedOsSandbox = [Environment]::GetEnvironmentVariable('DSH_OS_SANDBOX', 'Process')
    $savedPreflight = [Environment]::GetEnvironmentVariable('DSH_REVIEW_PREFLIGHT', 'Process')

    Write-Host "== full test suite (gate command) =="
    try {
        $env:DSH_OS_SANDBOX=0
        $env:DSH_REVIEW_PREFLIGHT=0
        & $Python -m unittest discover -s tests -q
        $suiteRc = $LASTEXITCODE
    } finally {
        [Environment]::SetEnvironmentVariable('DSH_OS_SANDBOX', $savedOsSandbox, 'Process')
        [Environment]::SetEnvironmentVariable('DSH_REVIEW_PREFLIGHT', $savedPreflight, 'Process')
    }

    # Run even when the suite failed. CF_REQUIRE_REAL_SANDBOX=1 turns every
    # platform/boundary skip in the dispatcher into a hard failure, so the real
    # Windows boundary cannot silently disappear on a CI runner.
    $savedRequire = [Environment]::GetEnvironmentVariable('CF_REQUIRE_REAL_SANDBOX', 'Process')
    Write-Host "== sandbox boundary (CF_REQUIRE_REAL_SANDBOX=1; skips fail) =="
    try {
        $env:CF_REQUIRE_REAL_SANDBOX=1
        & $Python -m unittest -v tests.test_sandbox_boundary
        $boundaryRc = $LASTEXITCODE
    } finally {
        [Environment]::SetEnvironmentVariable('CF_REQUIRE_REAL_SANDBOX', $savedRequire, 'Process')
    }

    Write-Host "== summary =="
    Write-Host ("full suite exit code:       {0}" -f $suiteRc)
    Write-Host ("sandbox boundary exit code: {0}" -f $boundaryRc)
    if ($suiteRc -ne 0 -or $boundaryRc -ne 0) {
        Write-Host "CI FAILED" -ForegroundColor Red
        exit 1
    }
    Write-Host "CI PASSED"
    exit 0
}
finally {
    Remove-Item -LiteralPath $FakeDir -Recurse -Force -ErrorAction SilentlyContinue
}
