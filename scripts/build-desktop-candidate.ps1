[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Version,

    [Parameter(Mandatory = $true)][string]$OutputDirectory,

    [Parameter(Mandatory = $true)][string]$ExpectedRevision,

    [Parameter(Mandatory = $true)]
    [ValidateSet("unsigned", "authenticode")]
    [string]$SigningStatus,

    [string]$CertificateThumbprint,

    [string]$SearchQualityGatePath,

    [switch]$AllowUncertifiedSearchQualityPreview
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$publisher = Join-Path (Join-Path $repoRoot "scripts") "publish-desktop.ps1"
$searchQualityValidator = Join-Path `
    (Join-Path $repoRoot "scripts") "validate-search-quality-gate.ps1"

if ($ExpectedRevision -notmatch '^[0-9a-f]{40}$') {
    throw "ExpectedRevision must contain 40 lowercase hexadecimal characters."
}
if ($SigningStatus -eq "authenticode") {
    if ($CertificateThumbprint -notmatch '^[0-9A-Fa-f]{40}$') {
        throw "A 40-character certificate thumbprint is required for signing."
    }
}
elseif (-not [string]::IsNullOrWhiteSpace($CertificateThumbprint)) {
    throw "CertificateThumbprint cannot be supplied for an unsigned candidate."
}
if (
    -not [string]::IsNullOrWhiteSpace($SearchQualityGatePath) -and
    $AllowUncertifiedSearchQualityPreview
) {
    throw (
        "Use either -SearchQualityGatePath or " +
        "-AllowUncertifiedSearchQualityPreview, not both."
    )
}
if (
    [string]::IsNullOrWhiteSpace($SearchQualityGatePath) -and
    -not $AllowUncertifiedSearchQualityPreview
) {
    throw (
        "Formal desktop candidates require -SearchQualityGatePath. " +
        "Uncertified Preview candidates require the explicit " +
        "-AllowUncertifiedSearchQualityPreview switch."
    )
}
if (Test-Path -LiteralPath $OutputDirectory) {
    throw "Desktop candidate output already exists: $OutputDirectory"
}

$arguments = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $publisher,
    "-Version", $Version,
    "-OutputDirectory", $OutputDirectory,
    "-RequireInstaller"
)
if ($SigningStatus -eq "authenticode") {
    $arguments += @("-CertificateThumbprint", $CertificateThumbprint)
}
if (-not [string]::IsNullOrWhiteSpace($SearchQualityGatePath)) {
    $arguments += @("-SearchQualityGatePath", $SearchQualityGatePath)
}
else {
    $arguments += "-AllowUncertifiedSearchQualityPreview"
}
& powershell.exe @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Desktop publisher failed with exit code $LASTEXITCODE."
}

$releaseDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
$metadataPath = Join-Path $releaseDirectory "desktop-release.json"
$metadata = Get-Content -LiteralPath $metadataPath -Raw -Encoding utf8 |
    ConvertFrom-Json
if (
    [int]$metadata.schema_version -ne 3 -or
    $metadata.version -cne $Version -or
    $metadata.git_state -cne "clean" -or
    $metadata.git_revision -cne $ExpectedRevision -or
    $metadata.signing.status -cne $SigningStatus -or
    $metadata.installer.status -cne "built"
) {
    throw "Desktop release metadata failed the candidate policy gate."
}
$qualityCertified = -not [string]::IsNullOrWhiteSpace($SearchQualityGatePath)
if ($qualityCertified) {
    if (
        $metadata.search_quality.status -cne "certified" -or
        $metadata.search_quality.certified -ne $true -or
        $null -eq $metadata.search_quality.gate
    ) {
        throw "Desktop candidate is missing certified search-quality metadata."
    }
    $packagedGatePath = Join-Path `
        $releaseDirectory ([string]$metadata.search_quality.gate.file)
    & $searchQualityValidator `
        -Path $packagedGatePath `
        -RepositoryRoot $repoRoot `
        -Quiet
    $packagedGateHash = (Get-FileHash `
        -LiteralPath $packagedGatePath `
        -Algorithm SHA256).Hash
    if (
        $packagedGateHash -cne (
            [string]$metadata.search_quality.gate.sha256
        ).ToUpperInvariant()
    ) {
        throw "Packaged search-quality gate hash is inconsistent."
    }
}
else {
    if (
        $metadata.search_quality.status -cne "uncertified-preview" -or
        $metadata.search_quality.certified -ne $false -or
        $null -ne $metadata.search_quality.gate -or
        [string]::IsNullOrWhiteSpace(
            [string]$metadata.search_quality.warning
        )
    ) {
        throw "Uncertified Preview search-quality metadata is incorrect."
    }
    Write-Warning (
        "UNCERTIFIED SEARCH QUALITY PREVIEW: " +
        [string]$metadata.search_quality.warning
    )
}
$rids = @($metadata.runtime_identifiers | Sort-Object)
if (($rids -join ",") -cne "win-arm64,win-x64") {
    throw "Desktop release does not contain exactly x64 and ARM64."
}

$expectedSigned = $SigningStatus -eq "authenticode"
$artifacts = @($metadata.artifacts)
foreach ($artifact in $artifacts) {
    $path = Join-Path $releaseDirectory ([string]$artifact.file)
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Desktop manifest references a missing artifact: $path"
    }
    $actualSize = (Get-Item -LiteralPath $path).Length
    if ([long]$artifact.size_bytes -ne $actualSize) {
        throw "Desktop artifact size metadata is inconsistent: $path"
    }
    if ($artifact.PSObject.Properties.Name -contains "sha256") {
        $actualHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
        if ($actualHash -cne ([string]$artifact.sha256).ToUpperInvariant()) {
            throw "Desktop artifact hash metadata is inconsistent: $path"
        }
    }

    $isDesktopBinary = $artifact.kind -in @("zip", "nsis-installer")
    if ($isDesktopBinary -and [bool]$artifact.signed -ne $expectedSigned) {
        throw "Desktop binary signing metadata is inconsistent."
    }
    if (-not $isDesktopBinary -and [bool]$artifact.signed) {
        throw "Supply-chain sidecars must not claim Authenticode signing."
    }
    if ($isDesktopBinary) {
        $isUnsignedName = ([string]$artifact.file) -match '-unsigned'
        if ($isUnsignedName -ne (-not $expectedSigned)) {
            throw "Desktop artifact filename does not expose signing status."
        }
    }
}
foreach ($kind in @(
    "zip",
    "nsis-installer",
    "spdx-sbom",
    "slsa-provenance",
    "search-quality-gate"
)) {
    $expectedCount = if ($kind -in @("zip", "nsis-installer")) {
        2
    }
    elseif ($kind -ceq "search-quality-gate" -and -not $qualityCertified) {
        0
    }
    else {
        1
    }
    if (@($artifacts | Where-Object kind -eq $kind).Count -ne $expectedCount) {
        throw "Desktop release has an unexpected $kind artifact count."
    }
}

foreach ($field in @("sbom", "provenance")) {
    $supplyChainEntry = $metadata.supply_chain.$field
    $path = Join-Path $releaseDirectory ([string]$supplyChainEntry.file)
    $actualHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    if (
        $actualHash -cne ([string]$supplyChainEntry.sha256).ToUpperInvariant() -or
        $supplyChainEntry.signature_status -cne "unsigned"
    ) {
        throw "Desktop $field sidecar metadata is inconsistent."
    }
}

$signatureStatus = if ($expectedSigned) { "Valid" } else { "NotSigned" }
foreach ($installer in @($artifacts | Where-Object kind -eq "nsis-installer")) {
    $path = Join-Path $releaseDirectory ([string]$installer.file)
    $signature = Get-AuthenticodeSignature -LiteralPath $path
    if ([string]$signature.Status -cne $signatureStatus) {
        throw "Installer Authenticode status is not $signatureStatus`: $path"
    }
}

$inspectionRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    "zvec-candidate-signature-" + [guid]::NewGuid().ToString("N")
)
$null = [System.IO.Directory]::CreateDirectory($inspectionRoot)
try {
    foreach ($zip in @($artifacts | Where-Object kind -eq "zip")) {
        $zipPath = Join-Path $releaseDirectory ([string]$zip.file)
        $extractPath = Join-Path $inspectionRoot ([string]$zip.rid)
        Expand-Archive -LiteralPath $zipPath -DestinationPath $extractPath
        foreach ($name in @("Zvec.Desktop.exe", "Zvec.Desktop.dll")) {
            $path = Get-ChildItem -LiteralPath $extractPath -Recurse -File |
                Where-Object Name -ceq $name |
                Select-Object -First 1 -ExpandProperty FullName
            if ([string]::IsNullOrWhiteSpace($path)) {
                throw "Desktop ZIP is missing $name`: $zipPath"
            }
            $signature = Get-AuthenticodeSignature -LiteralPath $path
            if ([string]$signature.Status -cne $signatureStatus) {
                throw "ZIP payload Authenticode status is not $signatureStatus`: $path"
            }
        }
    }
}
finally {
    if (Test-Path -LiteralPath $inspectionRoot) {
        $resolvedInspection = [System.IO.Path]::GetFullPath($inspectionRoot)
        $resolvedTemp = [System.IO.Path]::GetFullPath(
            [System.IO.Path]::GetTempPath()
        ).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
        if (
            $resolvedInspection.StartsWith(
                $resolvedTemp + [System.IO.Path]::DirectorySeparatorChar,
                [System.StringComparison]::OrdinalIgnoreCase
            )
        ) {
            Remove-Item -LiteralPath $resolvedInspection -Recurse -Force
        }
    }
}

$checksumPath = Join-Path $releaseDirectory "SHA256SUMS.txt"
$covered = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::Ordinal
)
foreach ($line in Get-Content -LiteralPath $checksumPath -Encoding utf8) {
    if ($line -notmatch '^(?<hash>[0-9a-f]{64})  (?<file>.+)$') {
        throw "Invalid desktop checksum line: $line"
    }
    $path = Join-Path $releaseDirectory $Matches["file"]
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    if ($actual -cne $Matches["hash"].ToUpperInvariant()) {
        throw "Desktop checksum mismatch: $path"
    }
    $null = $covered.Add($Matches["file"])
}
$expectedFiles = @(
    Get-ChildItem -LiteralPath $releaseDirectory -File |
        Where-Object Name -cne "SHA256SUMS.txt" |
        ForEach-Object Name
)
if (
    $covered.Count -ne $expectedFiles.Count -or
    @($expectedFiles | Where-Object { -not $covered.Contains($_) }).Count -gt 0
) {
    throw "Desktop checksum manifest does not cover every release file."
}

$qualityLabel = if ($qualityCertified) {
    "search-quality-certified"
}
else {
    "UNCERTIFIED-search-quality-preview"
}
Write-Host (
    "Verified desktop $SigningStatus candidate ($qualityLabel): $releaseDirectory"
)
