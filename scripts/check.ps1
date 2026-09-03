# One command that says whether the build is in the state the docs claim.
#
#   powershell -ExecutionPolicy Bypass -File scripts\check.ps1            # unit + e2e + retrieval regression
#   powershell -ExecutionPolicy Bypass -File scripts\check.ps1 -Quick     # unit tests only
#
# Exit code is non-zero if any stage fails. The retrieval regression needs the
# benchmark clones (see README "Development"); it is skipped with a warning when
# bench/agentctx/repo is absent rather than reported as a failure.
param([switch]$Quick)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
$failed = 0

function Stage($name, $cmd) {
    Write-Host "`n=== $name ===" -ForegroundColor Cyan
    & $cmd
    if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $name" -ForegroundColor Red; $script:failed = 1 }
    else { Write-Host "ok: $name" -ForegroundColor Green }
}

Stage "unit tests" { python -m pytest tests -q --ignore=tests/test_e2e_branch_cache.py }
if ($Quick) { exit $failed }

Stage "end-to-end branch cache (real graphify)" { python -m pytest tests/test_e2e_branch_cache.py -q -m e2e }

if (Test-Path "bench/agentctx/repo") {
    Stage "retrieval regression (shipped defaults, per task)" {
        python bench/agentctx/regress.py --config dyn300-mention-first
    }
} else {
    Write-Host "`n(skipping retrieval regression: bench/agentctx/repo not cloned)" -ForegroundColor Yellow
}

if (Test-Path "bench/fixeval/results.jsonl") {
    Stage "fix-eval report (informational)" { python bench/fixeval/run.py report; $global:LASTEXITCODE = 0 }
}

exit $failed
