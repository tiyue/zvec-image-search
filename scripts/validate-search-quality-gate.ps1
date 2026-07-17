[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Path,

    [string]$RepositoryRoot = (Get-Location).Path,

    [string]$PythonPath,

    [switch]$Quiet
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Read-JsonObject {
    param(
        [Parameter(Mandatory = $true)][string]$JsonPath,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $JsonPath -PathType Leaf)) {
        throw "$Label was not found at $JsonPath"
    }
    try {
        $value = Get-Content -LiteralPath $JsonPath -Raw -Encoding utf8 |
            ConvertFrom-Json
    }
    catch {
        throw "$Label is not valid JSON: $JsonPath ($($_.Exception.Message))"
    }
    if ($null -eq $value -or $value -isnot [pscustomobject]) {
        throw "$Label JSON root must be an object: $JsonPath"
    }
    return $value
}

function Get-RequiredProperty {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Context
    )

    if (
        $null -eq $Object -or
        $Object -isnot [pscustomobject] -or
        $Object.PSObject.Properties.Name -notcontains $Name
    ) {
        throw "$Context is missing required property '$Name'."
    }
    return $Object.PSObject.Properties[$Name].Value
}

function Assert-ExactText {
    param(
        $Value,
        [Parameter(Mandatory = $true)][string]$Expected,
        [Parameter(Mandatory = $true)][string]$Context
    )

    if ($Value -isnot [string] -or $Value -cne $Expected) {
        throw "$Context must equal '$Expected'."
    }
}

function Assert-Sha256Text {
    param(
        $Value,
        [Parameter(Mandatory = $true)][string]$Context
    )

    if ($Value -isnot [string] -or $Value -cnotmatch '^[0-9a-f]{64}$') {
        throw "$Context must contain a lowercase SHA-256 digest."
    }
}

function Assert-True {
    param(
        $Value,
        [Parameter(Mandatory = $true)][string]$Context
    )

    if ($Value -isnot [bool] -or $Value -ne $true) {
        throw "$Context must be true."
    }
}

function Assert-False {
    param(
        $Value,
        [Parameter(Mandatory = $true)][string]$Context
    )

    if ($Value -isnot [bool] -or $Value -ne $false) {
        throw "$Context must be false."
    }
}

function Assert-IntegerAtLeast {
    param(
        $Value,
        [Parameter(Mandatory = $true)][long]$Minimum,
        [Parameter(Mandatory = $true)][string]$Context
    )

    $integerTypes = @(
        [byte], [sbyte], [int16], [uint16], [int32], [uint32], [int64], [uint64]
    )
    $isInteger = $false
    foreach ($integerType in $integerTypes) {
        if ($Value -is $integerType) {
            $isInteger = $true
            break
        }
    }
    if (-not $isInteger -or [long]$Value -lt $Minimum) {
        throw "$Context must be an integer greater than or equal to $Minimum."
    }
}

function Resolve-ReferencedFile {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$GateDirectory,
        [Parameter(Mandatory = $true)][string]$RootDirectory,
        [Parameter(Mandatory = $true)][string]$Context
    )

    if ([string]::IsNullOrWhiteSpace($Source)) {
        throw "$Context source path cannot be empty."
    }
    if ([System.IO.Path]::IsPathRooted($Source)) {
        $candidates = @([System.IO.Path]::GetFullPath($Source))
    }
    else {
        $candidates = @(
            [System.IO.Path]::GetFullPath((Join-Path $RootDirectory $Source)),
            [System.IO.Path]::GetFullPath((Join-Path $GateDirectory $Source)),
            [System.IO.Path]::GetFullPath((Join-Path (Get-Location).Path $Source))
        ) | Select-Object -Unique
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    throw "$Context source file was not found: $Source"
}

function Assert-FormalRunSource {
    param(
        [Parameter(Mandatory = $true)]$Binding,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$GateDirectory,
        [Parameter(Mandatory = $true)][string]$RootDirectory,
        [Parameter(Mandatory = $true)][string]$ManifestFingerprint
    )

    $context = "$Label run binding"
    $source = Get-RequiredProperty $Binding "source" $context
    if ($source -isnot [string]) {
        throw "$context source must be a string."
    }
    Assert-Sha256Text `
        -Value (Get-RequiredProperty $Binding "fingerprint" $context) `
        -Context "$context canonical fingerprint"
    Assert-ExactText `
        -Value (Get-RequiredProperty $Binding "fingerprint_algorithm" $context) `
        -Expected "sha256-canonical-run-json-v1" `
        -Context "$context fingerprint_algorithm"
    $expectedSourceHash = Get-RequiredProperty $Binding "source_sha256" $context
    Assert-Sha256Text `
        -Value $expectedSourceHash `
        -Context "$context source_sha256"
    Assert-ExactText `
        -Value (Get-RequiredProperty $Binding "source_sha256_algorithm" $context) `
        -Expected "sha256-file-bytes-v1" `
        -Context "$context source_sha256_algorithm"

    $sourcePath = Resolve-ReferencedFile `
        -Source $source `
        -GateDirectory $GateDirectory `
        -RootDirectory $RootDirectory `
        -Context $context
    $actualSourceHash = (Get-FileHash `
        -LiteralPath $sourcePath `
        -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualSourceHash -cne $expectedSourceHash) {
        throw "$context source SHA-256 does not match the bound run file."
    }

    $run = Read-JsonObject -JsonPath $sourcePath -Label "$Label run"
    if ([int](Get-RequiredProperty $run "schema_version" "$Label run") -ne 1) {
        throw "$Label run schema_version must equal 1."
    }
    Assert-False `
        -Value (Get-RequiredProperty $run "draft" "$Label run") `
        -Context "$Label run draft"
    Assert-True `
        -Value (Get-RequiredProperty $run "baseline_eligible" "$Label run") `
        -Context "$Label run baseline_eligible"

    $capture = Get-RequiredProperty $run "capture" "$Label run"
    Assert-ExactText `
        -Value (Get-RequiredProperty $capture "kind" "$Label run capture") `
        -Expected "zvec-persistent-backend-capture" `
        -Context "$Label run capture kind"
    Assert-True `
        -Value (Get-RequiredProperty $capture "baseline_eligible" "$Label run capture") `
        -Context "$Label run capture baseline_eligible"
    $pendingCount = Get-RequiredProperty $capture "pending_case_count" "$Label run capture"
    Assert-IntegerAtLeast `
        -Value $pendingCount `
        -Minimum 0 `
        -Context "$Label run capture pending_case_count"
    if ([long]$pendingCount -ne 0) {
        throw "$Label run capture must contain no pending cases."
    }
    Assert-IntegerAtLeast `
        -Value (Get-RequiredProperty $capture "top_k" "$Label run capture") `
        -Minimum 5 `
        -Context "$Label run capture top_k"
    Assert-IntegerAtLeast `
        -Value (Get-RequiredProperty $capture "candidate_k" "$Label run capture") `
        -Minimum 50 `
        -Context "$Label run capture candidate_k"

    $dataset = Get-RequiredProperty $run "dataset" "$Label run"
    Assert-Sha256Text `
        -Value (Get-RequiredProperty $dataset "fingerprint" "$Label run dataset") `
        -Context "$Label run query corpus fingerprint"
    Assert-ExactText `
        -Value (Get-RequiredProperty $dataset "fingerprint_algorithm" "$Label run dataset") `
        -Expected "sha256-canonical-query-corpus-v1" `
        -Context "$Label run query corpus fingerprint_algorithm"
    Assert-IntegerAtLeast `
        -Value (Get-RequiredProperty $dataset "case_count" "$Label run dataset") `
        -Minimum 1 `
        -Context "$Label run dataset case_count"

    $split = Get-RequiredProperty $run "split" "$Label run"
    Assert-ExactText `
        -Value (Get-RequiredProperty $split "kind" "$Label run split") `
        -Expected "zvec-search-quality-split" `
        -Context "$Label run split kind"
    Assert-ExactText `
        -Value (Get-RequiredProperty $split "role" "$Label run split") `
        -Expected "validation" `
        -Context "$Label run split role"
    Assert-ExactText `
        -Value (Get-RequiredProperty $split "manifest_fingerprint" "$Label run split") `
        -Expected $ManifestFingerprint `
        -Context "$Label run split manifest_fingerprint"
    Assert-ExactText `
        -Value (Get-RequiredProperty $split "manifest_fingerprint_algorithm" "$Label run split") `
        -Expected "sha256-canonical-json-v1" `
        -Context "$Label run split manifest_fingerprint_algorithm"
    return [pscustomobject]@{
        Path = $sourcePath
        Sha256 = $actualSourceHash
    }
}

$resolvedGatePath = [System.IO.Path]::GetFullPath($Path)
$resolvedRoot = [System.IO.Path]::GetFullPath($RepositoryRoot)
$gateDirectory = Split-Path -Parent $resolvedGatePath
$report = Read-JsonObject `
    -JsonPath $resolvedGatePath `
    -Label "Search-quality gate report"

if ([int](Get-RequiredProperty $report "schema_version" "gate report") -ne 1) {
    throw "Search-quality gate report schema_version must equal 1."
}
Assert-ExactText `
    -Value (Get-RequiredProperty $report "kind" "gate report") `
    -Expected "zvec-search-quality-comparison" `
    -Context "gate report kind"

$dataset = Get-RequiredProperty $report "dataset" "gate report"
$datasetFingerprint = Get-RequiredProperty $dataset "fingerprint" "gate dataset"
Assert-Sha256Text -Value $datasetFingerprint -Context "gate dataset fingerprint"
Assert-ExactText `
    -Value (Get-RequiredProperty $dataset "fingerprint_algorithm" "gate dataset") `
    -Expected "sha256-canonical-json-v1" `
    -Context "gate dataset fingerprint_algorithm"

$holdout = Get-RequiredProperty $report "holdout_split" "gate report"
Assert-ExactText `
    -Value (Get-RequiredProperty $holdout "kind" "validation holdout") `
    -Expected "zvec-search-quality-split" `
    -Context "validation holdout kind"
Assert-ExactText `
    -Value (Get-RequiredProperty $holdout "role" "validation holdout") `
    -Expected "validation" `
    -Context "validation holdout role"
$manifestFingerprint = Get-RequiredProperty `
    $holdout "manifest_fingerprint" "validation holdout"
Assert-Sha256Text `
    -Value $manifestFingerprint `
    -Context "validation holdout manifest_fingerprint"
Assert-ExactText `
    -Value (Get-RequiredProperty $holdout "manifest_fingerprint_algorithm" "validation holdout") `
    -Expected "sha256-canonical-json-v1" `
    -Context "validation holdout manifest_fingerprint_algorithm"
$validationFingerprint = Get-RequiredProperty `
    $holdout "validation_dataset_fingerprint" "validation holdout"
Assert-Sha256Text `
    -Value $validationFingerprint `
    -Context "validation holdout dataset fingerprint"
Assert-ExactText `
    -Value (Get-RequiredProperty $holdout "evaluated_dataset_fingerprint" "validation holdout") `
    -Expected $validationFingerprint `
    -Context "validation holdout evaluated_dataset_fingerprint"
Assert-ExactText `
    -Value $datasetFingerprint `
    -Expected $validationFingerprint `
    -Context "gate dataset fingerprint"
$sourceDatasetSource = Get-RequiredProperty `
    $holdout "source_dataset_source" "validation holdout"
if ($sourceDatasetSource -isnot [string] -or [string]::IsNullOrWhiteSpace($sourceDatasetSource)) {
    throw "Validation holdout source_dataset_source must be a non-empty string."
}
$validationDatasetSource = Get-RequiredProperty `
    $holdout "validation_dataset_source" "validation holdout"
if (
    $validationDatasetSource -isnot [string] -or
    [string]::IsNullOrWhiteSpace($validationDatasetSource)
) {
    throw "Validation holdout validation_dataset_source must be a non-empty string."
}
$sourceCaseCount = Get-RequiredProperty `
    $holdout "source_dataset_case_count" "validation holdout"
Assert-IntegerAtLeast `
    -Value $sourceCaseCount `
    -Minimum 60 `
    -Context "validation holdout source case count"
if ([long]$sourceCaseCount -ne 60) {
    throw "Validation holdout source case count must equal 60."
}
$validationCaseCount = Get-RequiredProperty `
    $holdout "validation_case_count" "validation holdout"
Assert-IntegerAtLeast `
    -Value $validationCaseCount `
    -Minimum 24 `
    -Context "validation holdout case count"
if ([long]$validationCaseCount -ne 24) {
    throw "Validation holdout case count must equal 24."
}

$gate = Get-RequiredProperty $report "quality_gate" "gate report"
if ([int](Get-RequiredProperty $gate "schema_version" "quality gate") -ne 1) {
    throw "Quality gate schema_version must equal 1."
}
Assert-ExactText `
    -Value (Get-RequiredProperty $gate "kind" "quality gate") `
    -Expected "zvec-search-quality-acceptance-gate" `
    -Context "quality gate kind"
Assert-ExactText `
    -Value (Get-RequiredProperty $gate "status" "quality gate") `
    -Expected "pass" `
    -Context "quality gate status"
Assert-True `
    -Value (Get-RequiredProperty $gate "passed" "quality gate") `
    -Context "quality gate passed"
Assert-True `
    -Value (Get-RequiredProperty $gate "collection_fairness_enforced" "quality gate") `
    -Context "quality gate collection_fairness_enforced"

$checks = Get-RequiredProperty $gate "checks" "quality gate"
$requiredChecks = @(
    "no_answer_false_return_rate",
    "precision_at_5_gain",
    "recall_at_5_drop",
    "p95_latency_increase_ratio",
    "api_request_increase",
    "cross_collection_scope_consistency",
    "cross_collection_coverage",
    "cross_collection_pairwise_accuracy",
    "worst_directional_pairwise_accuracy"
)
foreach ($checkName in $requiredChecks) {
    $check = Get-RequiredProperty $checks $checkName "quality gate checks"
    Assert-True `
        -Value (Get-RequiredProperty $check "passed" "quality gate check $checkName") `
        -Context "quality gate check $checkName passed"
}
foreach ($property in $checks.PSObject.Properties) {
    Assert-True `
        -Value (Get-RequiredProperty $property.Value "passed" "quality gate check $($property.Name)") `
        -Context "quality gate check $($property.Name) passed"
}

$before = Assert-FormalRunSource `
    -Binding (Get-RequiredProperty $report "before" "gate report") `
    -Label "before" `
    -GateDirectory $gateDirectory `
    -RootDirectory $resolvedRoot `
    -ManifestFingerprint $manifestFingerprint
$after = Assert-FormalRunSource `
    -Binding (Get-RequiredProperty $report "after" "gate report") `
    -Label "after" `
    -GateDirectory $gateDirectory `
    -RootDirectory $resolvedRoot `
    -ManifestFingerprint $manifestFingerprint
if ($before.Path -ceq $after.Path -or $before.Sha256 -ceq $after.Sha256) {
    throw "Before and after quality runs must be distinct bound artifacts."
}

$replayVerifier = Join-Path `
    (Join-Path $resolvedRoot "scripts") "verify_search_quality_gate.py"
if (-not (Test-Path -LiteralPath $replayVerifier -PathType Leaf)) {
    throw "Search-quality replay verifier was not found at $replayVerifier"
}
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $pythonCommand = @(
        Get-Command python -CommandType Application -ErrorAction Stop
    )[0]
    $resolvedPythonPath = $pythonCommand.Source
}
else {
    $pythonCommand = @(
        Get-Command $PythonPath -CommandType Application -ErrorAction Stop
    )[0]
    $resolvedPythonPath = $pythonCommand.Source
}
& $resolvedPythonPath `
    $replayVerifier `
    --report $resolvedGatePath `
    --repository-root $resolvedRoot `
    --quiet
if ($LASTEXITCODE -ne 0) {
    throw "Independent search-quality gate replay failed with exit code $LASTEXITCODE."
}

if (-not $Quiet) {
    Write-Host (
        "Certified search-quality gate validated: $resolvedGatePath " +
        "(validation cases=$validationCaseCount)"
    )
}
