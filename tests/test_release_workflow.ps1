Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$workflowPath = Join-Path $repoRoot ".github\workflows\release.yml"
$ciPath = Join-Path $repoRoot ".github\workflows\ci.yml"
$desktopBuilderPath = Join-Path $repoRoot "scripts\build-desktop-candidate.ps1"
$qualityValidatorPath = Join-Path `
    $repoRoot "scripts\validate-search-quality-gate.ps1"
$qualityReplayPath = Join-Path `
    $repoRoot "scripts\verify_search_quality_gate.py"

function Assert-Matches {
    param(
        [Parameter(Mandatory = $true)][string]$Content,
        [Parameter(Mandatory = $true)][string]$Pattern,
        [Parameter(Mandatory = $true)][string]$Scenario
    )

    if ($Content -notmatch $Pattern) {
        throw "Release workflow contract is missing ${Scenario}."
    }
}

if (-not (Test-Path -LiteralPath $workflowPath -PathType Leaf)) {
    throw "Release workflow was not found at $workflowPath"
}
$workflow = [System.IO.File]::ReadAllText($workflowPath)
$ci = [System.IO.File]::ReadAllText($ciPath)
$desktopBuilder = [System.IO.File]::ReadAllText($desktopBuilderPath)

Assert-Matches -Content $workflow `
    -Pattern '(?m)^  workflow_dispatch:\s*$' `
    -Scenario "a manual-only workflow_dispatch trigger"
foreach ($inputName in @(
    "version",
    "prerelease",
    "search_quality_gate_path",
    "allow_uncertified_search_quality_preview",
    "require_authenticode",
    "create_draft_release"
)) {
    Assert-Matches -Content $workflow `
        -Pattern "(?m)^      $([regex]::Escape($inputName)):\s*$" `
        -Scenario "the $inputName workflow input"
}
Assert-Matches -Content $workflow `
    -Pattern '(?ms)^permissions:\s*\r?\n  contents: read\s*$' `
    -Scenario "read-only default permissions"
Assert-Matches -Content $workflow `
    -Pattern '(?ms)^concurrency:\s*\r?\n  group:.*inputs\.version.*\r?\n  cancel-in-progress: false' `
    -Scenario "per-version release concurrency"
Assert-Matches -Content $workflow `
    -Pattern '(?ms)^  quality-gates:.*?uses: \./\.github/workflows/ci\.yml.*?contents: read' `
    -Scenario "the reusable full CI gate"
Assert-Matches -Content $ci `
    -Pattern '(?m)^  workflow_call:\s*$' `
    -Scenario "workflow_call support in CI"
Assert-Matches -Content $ci `
    -Pattern 'mypy tests/search_quality scripts/verify_search_quality_gate\.py' `
    -Scenario "type checking for the independent quality replay helper"
Assert-Matches -Content $ci `
    -Pattern 'tests\.test_native_launcher' `
    -Scenario "native launcher smoke tests on the Windows and Ubuntu matrix"
Assert-Matches -Content $ci `
    -Pattern 'tests\.test_native_package' `
    -Scenario "built wheel command-entry installation tests"
Assert-Matches -Content $ci `
    -Pattern '--constraint requirements-lock\.txt' `
    -Scenario "the native Python runtime lock in CI"
Assert-Matches -Content $ci `
    -Pattern '(?ms)^  native-python-compatibility:.*?windows-latest.*?ubuntu-latest.*?"3\.10".*?"3\.14"' `
    -Scenario "native Python minimum and maximum compatibility jobs"
Assert-Matches -Content $ci `
    -Pattern '(?ms)^  native-python-compatibility:.*?tests\.test_native_cli_smoke.*?zvec help' `
    -Scenario "installed native CLI smoke on every supported Python endpoint"

Assert-Matches -Content $workflow `
    -Pattern 'ZVEC_AUTHENTICODE_PFX_BASE64' `
    -Scenario "the base64 PFX secret"
Assert-Matches -Content $workflow `
    -Pattern 'ZVEC_AUTHENTICODE_PFX_PASSWORD' `
    -Scenario "the PFX password secret"
Assert-Matches -Content $workflow `
    -Pattern 'Import-PfxCertificate' `
    -Scenario "secure certificate-store import"
Assert-Matches -Content $workflow `
    -Pattern 'CertificateThumbprint = \$env:CERTIFICATE_THUMBPRINT' `
    -Scenario "thumbprint-only publisher signing"
if (
    $workflow -match '-CertificatePath' -or
    $workflow -match '(?i)signtool.*(?:/p|-p)'
) {
    throw "Release workflow may expose the PFX password on a child-process command line."
}
Assert-Matches -Content $workflow `
    -Pattern 'Remove imported signing certificates' `
    -Scenario "certificate cleanup"
Assert-Matches -Content $workflow `
    -Pattern '(?s)Authenticode environment secrets are incomplete.*Configure both' `
    -Scenario "fail-closed partial-secret handling"

Assert-Matches -Content $desktopBuilder `
    -Pattern '(?s)publish-desktop\.ps1.*?-RequireInstaller' `
    -Scenario "desktop packaging through publish-desktop.ps1"
Assert-Matches -Content $desktopBuilder `
    -Pattern 'SearchQualityGatePath' `
    -Scenario "the formal desktop search-quality gate parameter"
Assert-Matches -Content $desktopBuilder `
    -Pattern 'AllowUncertifiedSearchQualityPreview' `
    -Scenario "the explicit uncertified desktop Preview override"
if (-not (Test-Path -LiteralPath $qualityValidatorPath -PathType Leaf)) {
    throw "Search-quality release validator was not found at $qualityValidatorPath"
}
if (-not (Test-Path -LiteralPath $qualityReplayPath -PathType Leaf)) {
    throw "Search-quality replay verifier was not found at $qualityReplayPath"
}
$qualityValidator = [System.IO.File]::ReadAllText($qualityValidatorPath)
foreach ($bindingField in @(
    "fingerprint_algorithm",
    "source_sha256",
    "source_sha256_algorithm",
    "source_dataset_source",
    "source_dataset_case_count",
    "validation_dataset_source",
    "validation_dataset_fingerprint",
    "collection_fairness_enforced"
)) {
    Assert-Matches -Content $qualityValidator `
        -Pattern ([regex]::Escape($bindingField)) `
        -Scenario "search-quality validation of $bindingField"
}
Assert-Matches -Content $qualityValidator `
    -Pattern 'verify_search_quality_gate\.py' `
    -Scenario "independent Python search-quality replay"
Assert-Matches -Content $qualityValidator `
    -Pattern '--repository-root' `
    -Scenario "repository-scoped replay inputs"
Assert-Matches -Content $qualityValidator `
    -Pattern 'Get-RequiredProperty \$run "split"' `
    -Scenario "mandatory validation split metadata on formal runs"
Assert-Matches -Content $workflow `
    -Pattern '(?ms)^  prepare:.*?actions/setup-python@.*?python-version: "3\.12".*?Validate version, tag, and license policy' `
    -Scenario "Python 3.12 before release request validation"
Assert-Matches -Content $workflow `
    -Pattern '(?s)validate-search-quality-gate\.ps1.*?-PythonPath \$pythonPath' `
    -Scenario "the pinned workflow Python passed into quality replay"
Assert-Matches -Content $workflow `
    -Pattern '(?ms)^  desktop-signing:.*?if: needs\.prepare\.outputs\.exact_tag_ref == ''true''.*?environment: authenticode-signing' `
    -Scenario "the exact-tag protected signing environment"
$unsignedJob = [regex]::Match(
    $workflow,
    '(?ms)^  desktop-unsigned:.*?(?=^  desktop-signing:)'
).Value
if ([string]::IsNullOrWhiteSpace($unsignedJob)) {
    throw "Could not isolate the unsigned desktop job."
}
if (
    $unsignedJob -match 'ZVEC_AUTHENTICODE' -or
    $unsignedJob -match 'CERTIFICATE_(?:BASE64|PASSWORD)'
) {
    throw "Branch-capable unsigned desktop job must never receive signing secrets."
}
Assert-Matches -Content $workflow `
    -Pattern 'prerelease must exactly match the SemVer prerelease suffix' `
    -Scenario "strict prerelease input consistency"
Assert-Matches -Content $workflow `
    -Pattern '(?ms)^  native-package:.*?python -m pip wheel.*?python -m venv.*?bin/zvec.*?help' `
    -Scenario "isolated native wheel build, install, and command smoke"
Assert-Matches -Content $workflow `
    -Pattern 'native-\$\{\{ inputs\.version \}\}-wheel' `
    -Scenario "the native wheel release artifact"
if (
    ($workflow + "`n" + $ci) -match 'docker/setup-(?:qemu|buildx)-action' -or
    ($workflow + "`n" + $ci) -match 'docker/build-push-action' -or
    ($workflow + "`n" + $ci) -match 'tests[/\\]docker_smoke\.py' -or
    ($workflow + "`n" + $ci) -match 'publish-docker\.ps1'
) {
    throw "Default CI and release workflows must not depend on Docker assets."
}

Assert-Matches -Content $workflow `
    -Pattern 'licen\[cs\]e' `
    -Scenario "the repository LICENSE gate"
Assert-Matches -Content $workflow `
    -Pattern 'refs/tags/v\$\(\$env:RELEASE_VERSION\)' `
    -Scenario "the exact v<version> ref gate"
Assert-Matches -Content $workflow `
    -Pattern 'git rev-list -n 1' `
    -Scenario "tag-to-commit verification"
Assert-Matches -Content $workflow `
    -Pattern 'Unsigned artifacts cannot enter the stable channel' `
    -Scenario "the unsigned stable-channel rejection"
Assert-Matches -Content $workflow `
    -Pattern 'desktop artifacts are explicitly unsigned' `
    -Scenario "the explicit unsigned preview reason"
Assert-Matches -Content $workflow `
    -Pattern 'A formal run-bound validation search-quality gate is required' `
    -Scenario "the default fail-closed search-quality release gate"
Assert-Matches -Content $workflow `
    -Pattern 'validate-search-quality-gate\.ps1' `
    -Scenario "run-bound search-quality report validation"
Assert-Matches -Content $workflow `
    -Pattern 'search quality is explicitly uncertified' `
    -Scenario "the uncertified search-quality preview reason"
Assert-Matches -Content $workflow `
    -Pattern 'search_quality_certified = \$env:SEARCH_QUALITY_CERTIFIED -eq "true"' `
    -Scenario "search-quality certification in release policy metadata"

Assert-Matches -Content $workflow `
    -Pattern '(?ms)^  create-draft-release:.*?if: inputs\.create_draft_release.*?environment: github-release.*?permissions:\s*\r?\n      contents: write' `
    -Scenario "an approved, least-privilege draft release job"
Assert-Matches -Content $workflow `
    -Pattern '(?s)gh release view.*refusing to overwrite' `
    -Scenario "immutable GitHub Release handling"
Assert-Matches -Content $workflow `
    -Pattern '--verify-tag' `
    -Scenario "existing-tag enforcement"
Assert-Matches -Content $workflow `
    -Pattern '--draft' `
    -Scenario "draft-only GitHub Release creation"
Assert-Matches -Content $workflow `
    -Pattern 'arguments\+?=\(--prerelease\)' `
    -Scenario "effective prerelease propagation"
Assert-Matches -Content $workflow `
    -Pattern 'A stable SemVer tag cannot be consumed by a preview Release' `
    -Scenario "stable-tag preview protection"
foreach ($checksumName in @(
    "DESKTOP-SHA256SUMS.txt",
    "NATIVE-SHA256SUMS.txt",
    "RELEASE-ASSETS-SHA256SUMS.txt"
)) {
    Assert-Matches -Content $workflow `
        -Pattern ([regex]::Escape($checksumName)) `
        -Scenario "the unique $checksumName asset"
}
Assert-Matches -Content $workflow `
    -Pattern 'Duplicate GitHub Release asset basename' `
    -Scenario "release asset basename collision detection"
if (($workflow + "`n" + $ci) -match '(?m)^\s*-?\s*uses:\s+[^\s]+@v[0-9]+\s*(?:#.*)?$') {
    throw "Release and CI workflows must pin external actions by commit SHA."
}
Assert-Matches -Content ($workflow + "`n" + $ci) `
    -Pattern 'choco install nsis --version=3\.12\.0' `
    -Scenario "the pinned NSIS version"

$fixtureRoot = Join-Path `
    (Join-Path $repoRoot "artifacts") `
    (".release-quality-replay-test-" + [guid]::NewGuid().ToString("N"))
$packagedReport = Join-Path $env:TEMP `
    ("zvec-packaged-quality-gate-" + [guid]::NewGuid().ToString("N") + ".json")
try {
    $pythonPath = @(
        Get-Command python -CommandType Application
    )[0].Source
    $fixtureOutput = @(
        & $pythonPath `
            -m tests.search_quality.release_gate_fixture `
            --output-dir $fixtureRoot `
            --repository-root $repoRoot 2>&1
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Could not generate release replay fixture: $($fixtureOutput -join ' ')"
    }
    $reportPath = [string]$fixtureOutput[-1]
    & $qualityValidatorPath `
        -Path $reportPath `
        -RepositoryRoot $repoRoot `
        -PythonPath $pythonPath `
        -Quiet
    Copy-Item -LiteralPath $reportPath -Destination $packagedReport
    & $qualityValidatorPath `
        -Path $packagedReport `
        -RepositoryRoot $repoRoot `
        -PythonPath $pythonPath `
        -Quiet

    $originalReportText = Get-Content -LiteralPath $reportPath -Raw -Encoding utf8
    $splitReport = $originalReportText | ConvertFrom-Json
    $afterRunPath = [System.IO.Path]::GetFullPath(
        (Join-Path $repoRoot ([string]$splitReport.after.source))
    )
    $originalAfterText = Get-Content -LiteralPath $afterRunPath -Raw -Encoding utf8
    $afterWithoutSplit = $originalAfterText | ConvertFrom-Json
    $afterWithoutSplit.PSObject.Properties.Remove("split")
    $afterWithoutSplit | ConvertTo-Json -Depth 100 |
        Set-Content -LiteralPath $afterRunPath -Encoding utf8
    $splitReport.after.source_sha256 = (
        Get-FileHash -LiteralPath $afterRunPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $splitReport | ConvertTo-Json -Depth 100 |
        Set-Content -LiteralPath $reportPath -Encoding utf8
    $missingSplitRejected = $false
    try {
        $null = @(
            & $qualityValidatorPath `
                -Path $reportPath `
                -RepositoryRoot $repoRoot `
                -PythonPath $pythonPath `
                -Quiet 2>&1
        )
    }
    catch {
        $missingSplitRejected = $true
    }
    if (-not $missingSplitRejected) {
        throw "Search-quality release validator accepted a run without split metadata."
    }
    [System.IO.File]::WriteAllText($afterRunPath, $originalAfterText)
    [System.IO.File]::WriteAllText($reportPath, $originalReportText)

    $tampered = Get-Content -LiteralPath $reportPath -Raw -Encoding utf8 |
        ConvertFrom-Json
    $tampered.quality_gate.checks.precision_at_5_gain.actual = 999.0
    $tampered | ConvertTo-Json -Depth 100 |
        Set-Content -LiteralPath $reportPath -Encoding utf8
    $replayRejected = $false
    try {
        $null = @(
            & $qualityValidatorPath `
                -Path $reportPath `
                -RepositoryRoot $repoRoot `
                -PythonPath $pythonPath `
                -Quiet 2>&1
        )
    }
    catch {
        $replayRejected = $true
    }
    if (-not $replayRejected) {
        throw "Search-quality release validator accepted a tampered gate report."
    }
}
finally {
    if (Test-Path -LiteralPath $packagedReport -PathType Leaf) {
        Remove-Item -LiteralPath $packagedReport -Force
    }
    if (Test-Path -LiteralPath $fixtureRoot) {
        $resolvedFixture = (Resolve-Path -LiteralPath $fixtureRoot).Path
        $artifactRoot = [System.IO.Path]::GetFullPath(
            (Join-Path $repoRoot "artifacts")
        ).TrimEnd([System.IO.Path]::DirectorySeparatorChar) +
            [System.IO.Path]::DirectorySeparatorChar
        if (
            -not $resolvedFixture.StartsWith(
                $artifactRoot,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -or
            (Split-Path -Leaf $resolvedFixture) -notlike ".release-quality-replay-test-*"
        ) {
            throw "Refusing to clean unexpected release replay fixture path."
        }
        Remove-Item -LiteralPath $resolvedFixture -Recurse -Force
    }
}

Write-Host "GitHub release workflow contract tests passed."
