[CmdletBinding()]
param(
    [string]$Version,

    [string[]]$RuntimeIdentifiers = @("win-x64", "win-arm64"),

    [string]$OutputDirectory,

    [string]$DotNetPath,

    [string]$MakeNsisPath,

    [switch]$SkipInstaller,

    [switch]$RequireInstaller,

    [switch]$AllowDirty,

    [string]$SearchQualityGatePath,

    [switch]$AllowUncertifiedSearchQualityPreview,

    [string]$CertificateThumbprint,

    [string]$CertificatePath,

    [string]$CertificatePasswordEnvironmentVariable = "ZVEC_DESKTOP_CERT_PASSWORD",

    [string]$SignToolPath,

    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$projectPath = Join-Path $repoRoot "pyproject.toml"
$modelCatalogPath = Join-Path $repoRoot "model-catalog.default.json"
$desktopProject = Join-Path $repoRoot "desktop\Zvec.Desktop\Zvec.Desktop.csproj"
$installerScript = Join-Path $repoRoot "installer\Zvec.Desktop.nsi"
$installerBackendGuard = Join-Path $repoRoot "installer\check-persistent-backend.ps1"
$sbomGenerator = Join-Path $repoRoot "scripts\generate-sbom.ps1"
$searchQualityValidator = Join-Path `
    $repoRoot "scripts\validate-search-quality-gate.ps1"
$versionPattern = (
    '^(?<major>0|[1-9][0-9]*)\.(?<minor>0|[1-9][0-9]*)\.' +
    '(?<patch>0|[1-9][0-9]*)' +
    '(?<prerelease>-(?:[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$'
)

function Resolve-InputPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$BaseDirectory
    )

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $BaseDirectory $Path))
}

function Resolve-Executable {
    param(
        [string]$ExplicitPath,
        [Parameter(Mandatory = $true)][string[]]$CommandNames,
        [string[]]$FallbackPaths = @(),
        [Parameter(Mandatory = $true)][string]$DisplayName,
        [switch]$Optional
    )

    if (-not [string]::IsNullOrWhiteSpace($ExplicitPath)) {
        $resolved = Resolve-InputPath -Path $ExplicitPath -BaseDirectory (Get-Location).Path
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            throw "$DisplayName was not found at $resolved"
        }
        return $resolved
    }

    foreach ($commandName in $CommandNames) {
        $command = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($null -ne $command) {
            return $command.Source
        }
    }
    foreach ($fallbackPath in $FallbackPaths) {
        if (
            -not [string]::IsNullOrWhiteSpace($fallbackPath) -and
            (Test-Path -LiteralPath $fallbackPath -PathType Leaf)
        ) {
            return [System.IO.Path]::GetFullPath($fallbackPath)
        }
    }
    if ($Optional) {
        return $null
    }
    throw "$DisplayName was not found."
}

function Invoke-ExternalCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][object[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$DisplayName
    )

    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $FilePath @Arguments
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($exitCode -ne 0) {
        throw "$DisplayName failed with exit code $exitCode."
    }
}

function Invoke-ExternalCapture {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][object[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$DisplayName
    )

    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& $FilePath @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($exitCode -ne 0) {
        throw "$DisplayName failed with exit code $exitCode."
    }
    return [string[]]@($output | ForEach-Object { $_.ToString() })
}

function Write-Utf8File {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Get-PeMachine {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    $reader = New-Object System.IO.BinaryReader($stream)
    try {
        if ($stream.Length -lt 64 -or $reader.ReadUInt16() -ne 0x5A4D) {
            throw "Published executable is not a valid PE file: $Path"
        }
        $stream.Position = 0x3C
        $peOffset = $reader.ReadInt32()
        if ($peOffset -lt 0 -or $peOffset + 6 -gt $stream.Length) {
            throw "Published executable has an invalid PE header: $Path"
        }
        $stream.Position = $peOffset
        if ($reader.ReadUInt32() -ne 0x00004550) {
            throw "Published executable has an invalid PE signature: $Path"
        }
        return $reader.ReadUInt16()
    }
    finally {
        $reader.Dispose()
        $stream.Dispose()
    }
}

function Assert-PublishedApplication {
    param(
        [Parameter(Mandatory = $true)][string]$PublishDirectory,
        [Parameter(Mandatory = $true)][string]$RuntimeIdentifier,
        [Parameter(Mandatory = $true)][string]$ExpectedModelCatalogSha256
    )

    foreach ($relativePath in @(
        "Zvec.Desktop.exe",
        "Zvec.Desktop.dll",
        "hostfxr.dll",
        "coreclr.dll",
        "scripts\zvec.ps1",
        "backend\image_service.py",
        "backend\zvec_launcher.py",
        "backend\zvec_logging.py",
        "backend\pyproject.toml",
        "backend\requirements.txt",
        "backend\requirements-lock.txt",
        "backend\model-catalog.default.json",
        "backend\README.md",
        "backend\image_vector_service\__init__.py",
        "backend\image_vector_service\backend_instance_lock.py",
        "backend\image_vector_service\backend_server.py",
        "backend\image_vector_service\image_data_uri.py",
        "backend\image_vector_service\workspace_backup.py",
        "backend\image_vector_service\auto_tagging_assets\__init__.py",
        "backend\image_vector_service\auto_tagging_assets\v1.py",
        "backend\image_vector_service\auto_tagging_assets\v2.py",
        "backend\image_vector_service\auto_tagging_assets\v3.py"
    )) {
        $path = Join-Path $PublishDirectory $relativePath
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Self-contained publish is missing '$relativePath' for $RuntimeIdentifier."
        }
    }

    $publishedModelCatalog = Join-Path `
        $PublishDirectory "backend\model-catalog.default.json"
    $publishedModelCatalogHash = (Get-FileHash `
        -LiteralPath $publishedModelCatalog `
        -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($publishedModelCatalogHash -cne $ExpectedModelCatalogSha256) {
        throw "Published default model catalog does not match the repository input."
    }

    $satelliteCultureDirectories = @(
        Get-ChildItem -LiteralPath $PublishDirectory -Directory |
            Where-Object {
                $null -ne (
                    Get-ChildItem -LiteralPath $_.FullName `
                        -Filter "*.resources.dll" `
                        -File |
                        Select-Object -First 1
                )
            } |
            Sort-Object Name
    )
    $satelliteCultureNames = @($satelliteCultureDirectories.Name)
    if (
        $satelliteCultureNames.Count -ne 1 -or
        $satelliteCultureNames[0] -cne "zh-Hans"
    ) {
        throw (
            "Self-contained publish must contain only the zh-Hans satellite " +
            "resource directory; found: " +
            ($satelliteCultureNames -join ", ")
        )
    }

    $expectedMachine = switch ($RuntimeIdentifier) {
        "win-x64" { 0x8664 }
        "win-arm64" { 0xAA64 }
        default { throw "Unsupported runtime identifier: $RuntimeIdentifier" }
    }
    $executable = Join-Path $PublishDirectory "Zvec.Desktop.exe"
    $actualMachine = Get-PeMachine -Path $executable
    if ($actualMachine -ne $expectedMachine) {
        throw (
            "Published executable machine 0x$($actualMachine.ToString('X4')) does not " +
            "match $RuntimeIdentifier."
        )
    }
}

function Get-BundledFrameworkVersions {
    param([Parameter(Mandatory = $true)][string]$PublishDirectory)

    $runtimeConfigPath = Join-Path $PublishDirectory "Zvec.Desktop.runtimeconfig.json"
    if (-not (Test-Path -LiteralPath $runtimeConfigPath -PathType Leaf)) {
        throw "Publish is missing Zvec.Desktop.runtimeconfig.json."
    }
    try {
        $runtimeConfig = [System.IO.File]::ReadAllText($runtimeConfigPath) |
            ConvertFrom-Json
    }
    catch {
        throw "Could not parse runtime config: $($_.Exception.Message)"
    }
    if (
        $runtimeConfig.runtimeOptions.PSObject.Properties.Name -notcontains
            "includedFrameworks"
    ) {
        throw "Self-contained runtime config has no includedFrameworks list."
    }
    $frameworks = [ordered]@{}
    foreach ($framework in @($runtimeConfig.runtimeOptions.includedFrameworks)) {
        $name = [string]$framework.name
        $frameworkVersion = [string]$framework.version
        if (
            [string]::IsNullOrWhiteSpace($name) -or
            [string]::IsNullOrWhiteSpace($frameworkVersion)
        ) {
            throw "Self-contained runtime config contains an invalid framework entry."
        }
        $frameworks[$name] = $frameworkVersion
    }
    return $frameworks
}

function Get-GitBuildState {
    if ($null -eq (Get-Command git -ErrorAction SilentlyContinue)) {
        if (-not $AllowDirty) {
            throw (
                "Git CLI is unavailable, so the desktop release source cannot be " +
                "verified. Install Git or pass -AllowDirty explicitly."
            )
        }
        return [pscustomobject]@{
            Revision = $null
            Dirty = $null
            State = "unavailable"
        }
    }

    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $insideOutput = @(
            & git -C $repoRoot rev-parse --is-inside-work-tree 2>&1
        )
        $insideExitCode = $LASTEXITCODE
        if (
            $insideExitCode -ne 0 -or
            ($insideOutput -join "").Trim() -cne "true"
        ) {
            if (-not $AllowDirty) {
                throw (
                    "The desktop source is not inside a Git worktree and cannot be " +
                    "verified. Pass -AllowDirty explicitly to continue."
                )
            }
            return [pscustomobject]@{
                Revision = $null
                Dirty = $null
                State = "unavailable"
            }
        }

        $revisionOutput = @(& git -C $repoRoot rev-parse HEAD 2>&1)
        $revisionExitCode = $LASTEXITCODE
        $statusOutput = @(
            & git -C $repoRoot status --porcelain --untracked-files=normal 2>&1
        )
        $statusExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($revisionExitCode -ne 0 -or $statusExitCode -ne 0) {
        throw "Could not read Git revision and worktree status."
    }
    $revision = ($revisionOutput -join "").Trim().ToLowerInvariant()
    if ($revision -notmatch '^[0-9a-f]{40}$') {
        throw "Git returned an invalid revision: $revision"
    }
    $dirty = $statusOutput.Count -gt 0
    if ($dirty -and -not $AllowDirty) {
        throw (
            "Refusing to publish desktop artifacts from a dirty Git worktree. " +
            "Commit or stash changes, or pass -AllowDirty explicitly."
        )
    }
    return [pscustomobject]@{
        Revision = $revision
        Dirty = $dirty
        State = if ($dirty) { "dirty" } else { "clean" }
    }
}

function Invoke-AuthenticodeSign {
    param(
        [Parameter(Mandatory = $true)][string[]]$Files,
        [Parameter(Mandatory = $true)][string]$Description
    )

    $arguments = @("sign", "/fd", "SHA256", "/d", $Description)
    if (-not [string]::IsNullOrWhiteSpace($TimestampUrl)) {
        $arguments += @("/tr", $TimestampUrl, "/td", "SHA256")
    }
    if (-not [string]::IsNullOrWhiteSpace($normalizedThumbprint)) {
        $arguments += @("/sha1", $normalizedThumbprint, "/s", "My")
    }
    else {
        $arguments += @("/f", $resolvedCertificatePath)
        $password = [Environment]::GetEnvironmentVariable(
            $CertificatePasswordEnvironmentVariable
        )
        if (-not [string]::IsNullOrEmpty($password)) {
            $arguments += @("/p", $password)
        }
    }
    $arguments += $Files
    Invoke-ExternalCommand `
        -FilePath $resolvedSignTool `
        -Arguments $arguments `
        -DisplayName "Authenticode signing"

    foreach ($file in $Files) {
        Invoke-ExternalCommand `
            -FilePath $resolvedSignTool `
            -Arguments @("verify", "/pa", "/v", $file) `
            -DisplayName "Authenticode verification"
    }
}

function Remove-StagingDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedParent,
        [ValidateRange(1, 100)][int]$MaxAttempts = 20,
        [ValidateRange(1, 5000)][int]$RetryDelayMilliseconds = 250
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    $resolvedParent = [System.IO.Path]::GetFullPath($ExpectedParent).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $prefix = $resolvedParent + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolvedPath.StartsWith(
        $prefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to remove unexpected staging directory: $resolvedPath"
    }
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        if (-not (Test-Path -LiteralPath $resolvedPath)) {
            return
        }
        try {
            Remove-Item -LiteralPath $resolvedPath -Recurse -Force -ErrorAction Stop
            return
        }
        catch {
            if (-not (Test-Path -LiteralPath $resolvedPath)) {
                return
            }
            $isTransientFileLock = (
                $_.Exception -is [System.IO.IOException] -or
                $_.Exception -is [System.UnauthorizedAccessException]
            )
            if (-not $isTransientFileLock -or $attempt -eq $MaxAttempts) {
                throw
            }
            Start-Sleep -Milliseconds $RetryDelayMilliseconds
        }
    }
}

function Move-StagedReleaseDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$ExpectedParent,
        [ValidateRange(1, 100)][int]$MaxAttempts = 20,
        [ValidateRange(1, 5000)][int]$RetryDelayMilliseconds = 250
    )

    $resolvedSource = [System.IO.Path]::GetFullPath($Source)
    $resolvedDestination = [System.IO.Path]::GetFullPath($Destination)
    $resolvedParent = [System.IO.Path]::GetFullPath($ExpectedParent).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $prefix = $resolvedParent + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolvedSource.StartsWith(
        $prefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to move unexpected staging directory: $resolvedSource"
    }
    $destinationParent = [System.IO.Path]::GetDirectoryName($resolvedDestination)
    if (-not [string]::Equals(
        $destinationParent,
        $resolvedParent,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to publish outside the expected directory: $resolvedDestination"
    }
    if (Test-Path -LiteralPath $resolvedDestination) {
        throw "Desktop release output already exists: $resolvedDestination"
    }

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            Move-Item `
                -LiteralPath $resolvedSource `
                -Destination $resolvedDestination `
                -ErrorAction Stop
            return
        }
        catch {
            if (
                -not (Test-Path -LiteralPath $resolvedSource) -and
                (Test-Path -LiteralPath $resolvedDestination -PathType Container)
            ) {
                return
            }
            $isTransientFileLock = (
                $_.Exception -is [System.IO.IOException] -or
                $_.Exception -is [System.UnauthorizedAccessException]
            )
            if (-not $isTransientFileLock -or $attempt -eq $MaxAttempts) {
                throw
            }
            Start-Sleep -Milliseconds $RetryDelayMilliseconds
        }
    }
}

if (-not (Test-Path -LiteralPath $projectPath -PathType Leaf)) {
    throw "pyproject.toml was not found at $projectPath"
}
if (-not (Test-Path -LiteralPath $modelCatalogPath -PathType Leaf)) {
    throw "Default model catalog was not found at $modelCatalogPath"
}
if (-not (Test-Path -LiteralPath $desktopProject -PathType Leaf)) {
    throw "Desktop project was not found at $desktopProject"
}
if (-not (Test-Path -LiteralPath $sbomGenerator -PathType Leaf)) {
    throw "SBOM generator was not found at $sbomGenerator"
}
if (-not (Test-Path -LiteralPath $searchQualityValidator -PathType Leaf)) {
    throw "Search-quality gate validator was not found at $searchQualityValidator"
}
if (
    -not $SkipInstaller -and
    -not (Test-Path -LiteralPath $installerBackendGuard -PathType Leaf)
) {
    throw "Installer backend guard was not found at $installerBackendGuard"
}

$projectContent = [System.IO.File]::ReadAllText($projectPath)
$modelCatalogHash = (Get-FileHash `
    -LiteralPath $modelCatalogPath `
    -Algorithm SHA256).Hash.ToLowerInvariant()
$projectVersionMatch = [regex]::Match(
    $projectContent,
    '(?m)^\s*version\s*=\s*"(?<version>[^"]+)"\s*$'
)
if (-not $projectVersionMatch.Success) {
    throw "Could not read the project version from pyproject.toml."
}
$projectVersion = $projectVersionMatch.Groups["version"].Value
if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = $projectVersion
}
$versionMatch = [regex]::Match($Version, $versionPattern)
if (-not $versionMatch.Success) {
    throw "Version must use SemVer, for example 0.4.0 or 0.5.0-rc.1."
}
$prerelease = $versionMatch.Groups["prerelease"].Value.TrimStart("-")
if (-not [string]::IsNullOrWhiteSpace($prerelease)) {
    foreach ($identifier in $prerelease.Split(".")) {
        if (
            $identifier -match '^[0-9]+$' -and
            $identifier.Length -gt 1 -and
            $identifier.StartsWith("0", [System.StringComparison]::Ordinal)
        ) {
            throw "Numeric prerelease identifiers cannot contain leading zeroes."
        }
    }
}
if ($Version -cne $projectVersion) {
    throw (
        "Desktop release version '$Version' does not match pyproject.toml version " +
        "'$projectVersion'."
    )
}
$fileVersion = (
    $versionMatch.Groups["major"].Value + "." +
    $versionMatch.Groups["minor"].Value + "." +
    $versionMatch.Groups["patch"].Value + ".0"
)

$normalizedRids = New-Object System.Collections.Generic.List[string]
foreach ($ridValue in @($RuntimeIdentifiers)) {
    if ([string]::IsNullOrWhiteSpace($ridValue)) {
        throw "Runtime identifiers cannot be empty."
    }
    $rid = $ridValue.Trim().ToLowerInvariant()
    if ($rid -notin @("win-x64", "win-arm64")) {
        throw "Supported runtime identifiers are win-x64 and win-arm64."
    }
    if ($normalizedRids.Contains($rid)) {
        throw "Duplicate runtime identifier: $rid"
    }
    $normalizedRids.Add($rid)
}
if ($normalizedRids.Count -eq 0) {
    throw "At least one runtime identifier is required."
}
$RuntimeIdentifiers = $normalizedRids.ToArray()

if ($SkipInstaller -and $RequireInstaller) {
    throw "-SkipInstaller and -RequireInstaller cannot be used together."
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
        "Formal desktop publishing requires -SearchQualityGatePath with a " +
        "passing, run-bound validation report. Preview/development builds must " +
        "explicitly pass -AllowUncertifiedSearchQualityPreview."
    )
}
if (
    -not [string]::IsNullOrWhiteSpace($CertificateThumbprint) -and
    -not [string]::IsNullOrWhiteSpace($CertificatePath)
) {
    throw "Use either -CertificateThumbprint or -CertificatePath, not both."
}
if (
    [string]::IsNullOrWhiteSpace($CertificatePasswordEnvironmentVariable) -or
    $CertificatePasswordEnvironmentVariable -notmatch '^[A-Za-z_][A-Za-z0-9_]*$'
) {
    throw "Certificate password environment variable name is invalid."
}

$normalizedThumbprint = if ([string]::IsNullOrWhiteSpace($CertificateThumbprint)) {
    ""
}
else {
    $CertificateThumbprint.Replace(" ", "").ToUpperInvariant()
}
if (
    -not [string]::IsNullOrWhiteSpace($normalizedThumbprint) -and
    $normalizedThumbprint -notmatch '^[0-9A-F]{40}$'
) {
    throw "Certificate thumbprint must contain 40 hexadecimal characters."
}
$resolvedCertificatePath = $null
if (-not [string]::IsNullOrWhiteSpace($CertificatePath)) {
    $resolvedCertificatePath = Resolve-InputPath `
        -Path $CertificatePath `
        -BaseDirectory (Get-Location).Path
    if (-not (Test-Path -LiteralPath $resolvedCertificatePath -PathType Leaf)) {
        throw "Signing certificate was not found at $resolvedCertificatePath"
    }
}
$signingEnabled = (
    -not [string]::IsNullOrWhiteSpace($normalizedThumbprint) -or
    $null -ne $resolvedCertificatePath
)

$resolvedSearchQualityGate = $null
$searchQualityReport = $null
$searchQualityGateHash = $null
$searchQualityStatus = "uncertified-preview"
$searchQualityWarning = (
    "No passing human-verified validation quality gate was supplied. This " +
    "artifact is an uncertified search-quality Preview and must not be " +
    "represented as a stable release."
)
if (-not [string]::IsNullOrWhiteSpace($SearchQualityGatePath)) {
    $resolvedSearchQualityGate = Resolve-InputPath `
        -Path $SearchQualityGatePath `
        -BaseDirectory (Get-Location).Path
    & $searchQualityValidator `
        -Path $resolvedSearchQualityGate `
        -RepositoryRoot $repoRoot
    $searchQualityReport = Get-Content `
        -LiteralPath $resolvedSearchQualityGate `
        -Raw `
        -Encoding utf8 |
        ConvertFrom-Json
    $searchQualityGateHash = (Get-FileHash `
        -LiteralPath $resolvedSearchQualityGate `
        -Algorithm SHA256).Hash.ToLowerInvariant()
    $searchQualityStatus = "certified"
    $searchQualityWarning = $null
}
else {
    Write-Warning ("UNCERTIFIED SEARCH QUALITY PREVIEW: " + $searchQualityWarning)
}

$resolvedDotNet = Resolve-Executable `
    -ExplicitPath $DotNetPath `
    -CommandNames @("dotnet.exe", "dotnet") `
    -DisplayName ".NET SDK"
$dotnetSdkOutput = Invoke-ExternalCapture `
    -FilePath $resolvedDotNet `
    -Arguments @("--version") `
    -DisplayName "dotnet --version"
$dotnetSdkVersion = ($dotnetSdkOutput -join "").Trim()
if ($dotnetSdkVersion -notmatch '^[0-9]+\.[0-9]+\.[0-9]+(?:[-+].+)?$') {
    throw "dotnet --version returned an invalid SDK version: $dotnetSdkVersion"
}
$gitState = Get-GitBuildState

$resolvedMakeNsis = $null
if (-not $SkipInstaller) {
    $nsisFallbacks = @()
    if (-not [string]::IsNullOrWhiteSpace(${env:ProgramFiles(x86)})) {
        $nsisFallbacks += Join-Path ${env:ProgramFiles(x86)} "NSIS\makensis.exe"
    }
    if (-not [string]::IsNullOrWhiteSpace($env:ProgramFiles)) {
        $nsisFallbacks += Join-Path $env:ProgramFiles "NSIS\makensis.exe"
    }
    $resolvedMakeNsis = Resolve-Executable `
        -ExplicitPath $MakeNsisPath `
        -CommandNames @("makensis.exe", "makensis") `
        -FallbackPaths $nsisFallbacks `
        -DisplayName "NSIS compiler" `
        -Optional
    if ($null -eq $resolvedMakeNsis -and $RequireInstaller) {
        throw "NSIS compiler was not found, but -RequireInstaller was specified."
    }
    if ($null -ne $resolvedMakeNsis) {
        if (-not (Test-Path -LiteralPath $installerScript -PathType Leaf)) {
            throw "NSIS installer script was not found at $installerScript"
        }
    }
}

$resolvedSignTool = $null
if ($signingEnabled) {
    $signToolFallbacks = @()
    if (-not [string]::IsNullOrWhiteSpace(${env:ProgramFiles(x86)})) {
        $windowsKitBin = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
        if (Test-Path -LiteralPath $windowsKitBin -PathType Container) {
            $signToolFallbacks += @(
                Get-ChildItem -LiteralPath $windowsKitBin -Directory |
                    Sort-Object Name -Descending |
                    ForEach-Object { Join-Path $_.FullName "x64\signtool.exe" }
            )
        }
    }
    $resolvedSignTool = Resolve-Executable `
        -ExplicitPath $SignToolPath `
        -CommandNames @("signtool.exe", "signtool") `
        -FallbackPaths $signToolFallbacks `
        -DisplayName "SignTool"
}

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repoRoot "dist\desktop\$Version"
}
else {
    $OutputDirectory = Resolve-InputPath `
        -Path $OutputDirectory `
        -BaseDirectory (Get-Location).Path
}
$targetDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $targetDirectory) {
    throw "Desktop release output already exists: $targetDirectory"
}
$outputParent = Split-Path -Parent $targetDirectory
$null = [System.IO.Directory]::CreateDirectory($outputParent)
$stagingRoot = Join-Path $outputParent (
    ".zvec-desktop-publish-" + [guid]::NewGuid().ToString("N")
)
$releaseStage = Join-Path $stagingRoot "release"
$publishRoot = Join-Path $stagingRoot "publish"
$null = [System.IO.Directory]::CreateDirectory($releaseStage)
$null = [System.IO.Directory]::CreateDirectory($publishRoot)
$releaseGeneratedUtc = [DateTime]::UtcNow.ToString(
    "yyyy-MM-ddTHH:mm:ssZ",
    [System.Globalization.CultureInfo]::InvariantCulture
)

$artifactQualifier = if ($signingEnabled) { "" } else { "-unsigned" }
$signingStatus = if ($signingEnabled) { "authenticode" } else { "unsigned" }
$installerStatus = if ($null -ne $resolvedMakeNsis) {
    "built"
}
elseif ($SkipInstaller) {
    "skipped"
}
else {
    "not-built"
}
$artifacts = New-Object System.Collections.Generic.List[object]
$bundledFrameworks = [ordered]@{}

try {
    foreach ($rid in $RuntimeIdentifiers) {
        $packageBaseName = "Zvec-Desktop-$Version-$rid"
        $publishDirectory = Join-Path $publishRoot $packageBaseName
        Write-Host "Publishing $rid self-contained application ..."
        Invoke-ExternalCommand `
            -FilePath $resolvedDotNet `
            -Arguments @(
                "publish", $desktopProject,
                "--configuration", "Release",
                "--runtime", $rid,
                "--self-contained", "true",
                "--output", $publishDirectory,
                "--nologo",
                "-p:Version=$Version",
                "-p:FileVersion=$fileVersion",
                "-p:AssemblyVersion=$fileVersion",
                "-p:InformationalVersion=$Version",
                "-p:DebugSymbols=false",
                "-p:DebugType=None",
                "-p:SatelliteResourceLanguages=zh-Hans"
            ) `
            -DisplayName "dotnet publish ($rid)"
        Assert-PublishedApplication `
            -PublishDirectory $publishDirectory `
            -RuntimeIdentifier $rid `
            -ExpectedModelCatalogSha256 $modelCatalogHash
        $bundledFrameworks[$rid] = Get-BundledFrameworkVersions `
            -PublishDirectory $publishDirectory

        $applicationFiles = @(
            (Join-Path $publishDirectory "Zvec.Desktop.dll"),
            (Join-Path $publishDirectory "Zvec.Desktop.exe")
        )
        if ($signingEnabled) {
            Invoke-AuthenticodeSign `
                -Files $applicationFiles `
                -Description "Zvec Desktop $Version"
            $statusText = (
                "Authenticode signing and verification passed for Zvec Desktop " +
                "$Version ($rid).`r`n"
            )
        }
        else {
            $statusText = (
                "UNSIGNED PREVIEW BUILD`r`n" +
                "This package has no Authenticode signature. Verify SHA256SUMS.txt " +
                "before testing.`r`n"
            )
        }
        if ($gitState.Dirty -eq $true) {
            $statusText += (
                "DIRTY SOURCE WORKTREE`r`n" +
                "This artifact was built with -AllowDirty and is not a clean " +
                "release candidate.`r`n"
            )
        }
        elseif ($gitState.State -eq "unavailable") {
            $statusText += (
                "UNVERIFIED SOURCE STATE`r`n" +
                "Git revision and cleanliness were unavailable. This artifact was " +
                "built only because -AllowDirty was supplied.`r`n"
            )
        }
        Write-Utf8File `
            -Path (Join-Path $publishDirectory "SIGNING-STATUS.txt") `
            -Content $statusText
        $searchQualityStatusText = if ($searchQualityStatus -ceq "certified") {
            (
                "SEARCH QUALITY CERTIFIED`r`n" +
                "Formal validation acceptance gate: PASS`r`n" +
                "Gate SHA-256: $searchQualityGateHash`r`n" +
                "Validation manifest: " +
                "$($searchQualityReport.holdout_split.manifest_fingerprint)`r`n" +
                "Validation dataset: " +
                "$($searchQualityReport.holdout_split.validation_dataset_fingerprint)`r`n"
            )
        }
        else {
            "UNCERTIFIED SEARCH QUALITY PREVIEW`r`n$searchQualityWarning`r`n"
        }
        Write-Utf8File `
            -Path (Join-Path $publishDirectory "SEARCH-QUALITY-STATUS.txt") `
            -Content $searchQualityStatusText

        $zipName = "$packageBaseName$artifactQualifier.zip"
        $zipPath = Join-Path $releaseStage $zipName
        Compress-Archive `
            -LiteralPath $publishDirectory `
            -DestinationPath $zipPath `
            -CompressionLevel Optimal
        $zipInfo = Get-Item -LiteralPath $zipPath
        $artifacts.Add([ordered]@{
            file = $zipInfo.Name
            kind = "zip"
            rid = $rid
            signed = $signingEnabled
            size_bytes = $zipInfo.Length
            sha256 = (Get-FileHash `
                -LiteralPath $zipInfo.FullName `
                -Algorithm SHA256).Hash.ToLowerInvariant()
        })

        if ($null -ne $resolvedMakeNsis) {
            $installerName = "$packageBaseName$artifactQualifier-setup.exe"
            $installerPath = Join-Path $releaseStage $installerName
            Write-Host "Building $rid per-user NSIS installer ..."
            Invoke-ExternalCommand `
                -FilePath $resolvedMakeNsis `
                -Arguments @(
                    "/DVERSION=$Version",
                    "/DFILE_VERSION=$fileVersion",
                    "/DRID=$rid",
                    "/DSOURCE_DIR=$publishDirectory",
                    "/DOUTPUT_FILE=$installerPath",
                    "/DSIGNING_STATUS=$signingStatus",
                    $installerScript
                ) `
                -DisplayName "NSIS installer build ($rid)"
            if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
                throw "NSIS did not produce the expected installer: $installerPath"
            }
            if ($signingEnabled) {
                Invoke-AuthenticodeSign `
                    -Files @($installerPath) `
                    -Description "Zvec Desktop $Version Installer ($rid)"
            }
            $installerInfo = Get-Item -LiteralPath $installerPath
            $artifacts.Add([ordered]@{
                file = $installerInfo.Name
                kind = "nsis-installer"
                rid = $rid
                signed = $signingEnabled
                size_bytes = $installerInfo.Length
                sha256 = (Get-FileHash `
                    -LiteralPath $installerInfo.FullName `
                    -Algorithm SHA256).Hash.ToLowerInvariant()
            })
        }
    }

    if ($null -eq $resolvedMakeNsis -and -not $SkipInstaller) {
        Write-Warning (
            "NSIS was not found. ZIP packages were built without installers. " +
            "Install NSIS or pass -RequireInstaller in release automation."
        )
    }

    $searchQualityArtifact = $null
    if ($searchQualityStatus -ceq "certified") {
        $searchQualityName = "Zvec-Desktop-$Version.search-quality.json"
        $packagedSearchQualityPath = Join-Path $releaseStage $searchQualityName
        Copy-Item `
            -LiteralPath $resolvedSearchQualityGate `
            -Destination $packagedSearchQualityPath
        $packagedSearchQualityInfo = Get-Item `
            -LiteralPath $packagedSearchQualityPath
        $packagedSearchQualityHash = (Get-FileHash `
            -LiteralPath $packagedSearchQualityPath `
            -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($packagedSearchQualityHash -cne $searchQualityGateHash) {
            throw "Packaged search-quality gate hash changed while copying."
        }
        $searchQualityArtifact = [ordered]@{
            file = $packagedSearchQualityInfo.Name
            kind = "search-quality-gate"
            rid = $null
            signed = $false
            size_bytes = $packagedSearchQualityInfo.Length
            sha256 = $packagedSearchQualityHash
        }
        $artifacts.Add($searchQualityArtifact)
    }

    $frameworkManifestPath = Join-Path $stagingRoot "bundled-frameworks.json"
    Write-Utf8File `
        -Path $frameworkManifestPath `
        -Content (($bundledFrameworks | ConvertTo-Json -Depth 5) + "`n")
    $sbomArguments = @{
        RepositoryRoot = $repoRoot
        OutputDirectory = $releaseStage
        Version = $Version
        GitState = $gitState.State
        DotNetSdkVersion = $dotnetSdkVersion
        BundledFrameworksPath = $frameworkManifestPath
        RuntimeIdentifiers = @($RuntimeIdentifiers)
        ArtifactDirectory = $releaseStage
        GeneratedUtc = $releaseGeneratedUtc
        SigningStatus = $signingStatus
        BuilderId = "urn:zvec:builder:publish-desktop.ps1"
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$gitState.Revision)) {
        $sbomArguments["GitRevision"] = $gitState.Revision
    }
    Write-Host "Generating offline SPDX SBOM and provenance statement ..."
    & $sbomGenerator @sbomArguments

    $sbomName = "Zvec-Desktop-$Version.spdx.json"
    $provenanceName = "Zvec-Desktop-$Version.provenance.json"
    $sbomPath = Join-Path $releaseStage $sbomName
    $provenancePath = Join-Path $releaseStage $provenanceName
    foreach ($supplyChainPath in @($sbomPath, $provenancePath)) {
        if (-not (Test-Path -LiteralPath $supplyChainPath -PathType Leaf)) {
            throw "Supply-chain generator did not create: $supplyChainPath"
        }
    }
    $sbomInfo = Get-Item -LiteralPath $sbomPath
    $provenanceInfo = Get-Item -LiteralPath $provenancePath
    $sbomHash = (Get-FileHash `
        -LiteralPath $sbomPath `
        -Algorithm SHA256).Hash.ToLowerInvariant()
    $provenanceHash = (Get-FileHash `
        -LiteralPath $provenancePath `
        -Algorithm SHA256).Hash.ToLowerInvariant()
    $artifacts.Add([ordered]@{
        file = $sbomInfo.Name
        kind = "spdx-sbom"
        rid = $null
        signed = $false
        size_bytes = $sbomInfo.Length
        sha256 = $sbomHash
    })
    $artifacts.Add([ordered]@{
        file = $provenanceInfo.Name
        kind = "slsa-provenance"
        rid = $null
        signed = $false
        size_bytes = $provenanceInfo.Length
        sha256 = $provenanceHash
    })

    $searchQualityMetadata = if ($searchQualityStatus -ceq "certified") {
        [ordered]@{
            status = "certified"
            certified = $true
            warning = $null
            gate = [ordered]@{
                file = $searchQualityArtifact.file
                kind = "zvec-search-quality-comparison"
                sha256 = $searchQualityArtifact.sha256
                size_bytes = $searchQualityArtifact.size_bytes
            }
            holdout = [ordered]@{
                role = "validation"
                manifest_fingerprint = (
                    $searchQualityReport.holdout_split.manifest_fingerprint
                )
                validation_dataset_fingerprint = (
                    $searchQualityReport.holdout_split.validation_dataset_fingerprint
                )
                validation_case_count = [int](
                    $searchQualityReport.holdout_split.validation_case_count
                )
            }
            before = [ordered]@{
                fingerprint = $searchQualityReport.before.fingerprint
                fingerprint_algorithm = (
                    $searchQualityReport.before.fingerprint_algorithm
                )
                source_sha256 = $searchQualityReport.before.source_sha256
            }
            after = [ordered]@{
                fingerprint = $searchQualityReport.after.fingerprint
                fingerprint_algorithm = (
                    $searchQualityReport.after.fingerprint_algorithm
                )
                source_sha256 = $searchQualityReport.after.source_sha256
            }
        }
    }
    else {
        [ordered]@{
            status = "uncertified-preview"
            certified = $false
            warning = $searchQualityWarning
            gate = $null
            holdout = $null
            before = $null
            after = $null
        }
    }

    $metadata = [ordered]@{
        schema_version = 3
        product = "Zvec Desktop"
        version = $Version
        generated_utc = $releaseGeneratedUtc
        git_revision = $gitState.Revision
        git_dirty = $gitState.Dirty
        git_state = $gitState.State
        dotnet_sdk_version = $dotnetSdkVersion
        target_framework = "net8.0-windows"
        bundled_frameworks = $bundledFrameworks
        runtime_identifiers = @($RuntimeIdentifiers)
        self_contained = $true
        signing = [ordered]@{
            status = $signingStatus
            timestamp_url = if ($signingEnabled) { $TimestampUrl } else { $null }
        }
        installer = [ordered]@{
            status = $installerStatus
            technology = if ($null -ne $resolvedMakeNsis) { "NSIS" } else { $null }
            scope = if ($null -ne $resolvedMakeNsis) { "per-user" } else { $null }
            architecture_strategy = "architecture-specific-payloads"
        }
        search_quality = $searchQualityMetadata
        supply_chain = [ordered]@{
            sbom = [ordered]@{
                file = $sbomInfo.Name
                format = "SPDX-2.3-json"
                sha256 = $sbomHash
                size_bytes = $sbomInfo.Length
                signature_status = "unsigned"
            }
            provenance = [ordered]@{
                file = $provenanceInfo.Name
                statement_type = "https://in-toto.io/Statement/v1"
                predicate_type = "https://slsa.dev/provenance/v1"
                sha256 = $provenanceHash
                size_bytes = $provenanceInfo.Length
                signature_status = "unsigned"
            }
            python_lock = [ordered]@{
                file = "requirements-lock.txt"
                sha256 = (Get-FileHash `
                    -LiteralPath (Join-Path $repoRoot "requirements-lock.txt") `
                    -Algorithm SHA256).Hash.ToLowerInvariant()
                requirement = "exact-name-version-pins"
            }
            model_catalog = [ordered]@{
                file = "model-catalog.default.json"
                packaged_path = "backend/model-catalog.default.json"
                sha256 = $modelCatalogHash
            }
            python_runtime = [ordered]@{
                mode = "native-venv"
                package_format = "wheel"
                docker_required = $false
            }
            verification = "SHA256SUMS.txt"
        }
        artifacts = $artifacts.ToArray()
    }
    $metadataPath = Join-Path $releaseStage "desktop-release.json"
    Write-Utf8File `
        -Path $metadataPath `
        -Content (($metadata | ConvertTo-Json -Depth 6) + "`n")

    $checksumFiles = @(
        Get-ChildItem -LiteralPath $releaseStage -File |
            Sort-Object Name
    )
    $checksumLines = @(
        foreach ($file in $checksumFiles) {
            $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
            "$($hash.ToLowerInvariant())  $($file.Name)"
        }
    )
    Write-Utf8File `
        -Path (Join-Path $releaseStage "SHA256SUMS.txt") `
        -Content (($checksumLines -join "`n") + "`n")

    Move-StagedReleaseDirectory `
        -Source $releaseStage `
        -Destination $targetDirectory `
        -ExpectedParent $outputParent
    Write-Host "Desktop release created: $targetDirectory"
    Write-Host "Signing status: $signingStatus"
    Write-Host "Installer status: $installerStatus"
    Write-Host "Search-quality status: $searchQualityStatus"
}
finally {
    Remove-StagingDirectory -Path $stagingRoot -ExpectedParent $outputParent
}
