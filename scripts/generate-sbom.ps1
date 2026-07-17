[CmdletBinding()]
param(
    [string]$RepositoryRoot,

    [Parameter(Mandatory = $true)][string]$OutputDirectory,

    [string]$Version,

    [ValidateSet("clean", "dirty", "unavailable")][string]$GitState,

    [string]$GitRevision,

    [string]$DotNetSdkVersion,

    [string]$BundledFrameworksPath,

    [string[]]$RuntimeIdentifiers = @(),

    [string]$ArtifactDirectory,

    [string]$GeneratedUtc,

    [ValidateSet("authenticode", "unsigned", "unknown")]
    [string]$SigningStatus = "unknown",

    [string]$BuilderId = "urn:zvec:builder:generate-sbom.ps1"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$generatorVersion = "1"
$spdxVersion = "SPDX-2.3"

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

function Write-Utf8File {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-TextSha256 {
    param([Parameter(Mandatory = $true)][string]$Text)

    $encoding = New-Object System.Text.UTF8Encoding($false)
    $bytes = $encoding.GetBytes($Text)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $algorithm.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hash)).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

function Get-TomlSection {
    param(
        [Parameter(Mandatory = $true)][string]$Content,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $escapedName = [regex]::Escape($Name)
    $match = [regex]::Match(
        $Content,
        "(?ms)^\s*\[$escapedName\]\s*(?<body>.*?)(?=^\s*\[|\z)"
    )
    if (-not $match.Success) {
        throw "TOML section [$Name] was not found."
    }
    return $match.Groups["body"].Value
}

function Get-TomlString {
    param(
        [Parameter(Mandatory = $true)][string]$Section,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $escapedName = [regex]::Escape($Name)
    $match = [regex]::Match(
        $Section,
        ('(?m)^\s*' + $escapedName + '\s*=\s*"(?<value>[^\"]+)"\s*$')
    )
    if (-not $match.Success) {
        throw "TOML string '$Name' was not found."
    }
    return $match.Groups["value"].Value
}

function Get-TomlStringArray {
    param(
        [Parameter(Mandatory = $true)][string]$Section,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $escapedName = [regex]::Escape($Name)
    $arrayMatch = [regex]::Match(
        $Section,
        "(?ms)^\s*$escapedName\s*=\s*\[(?<body>.*?)\]"
    )
    if (-not $arrayMatch.Success) {
        return [string[]]@()
    }
    $values = @(
        [regex]::Matches($arrayMatch.Groups["body"].Value, '"(?<value>[^\"]+)"') |
            ForEach-Object { $_.Groups["value"].Value }
    )
    return [string[]]$values
}

function Get-CanonicalPythonName {
    param([Parameter(Mandatory = $true)][string]$Name)

    return ([regex]::Replace($Name.Trim().ToLowerInvariant(), '[-_.]+', '-'))
}

function Read-PythonLock {
    param([Parameter(Mandatory = $true)][string]$Path)

    $packages = New-Object System.Collections.Generic.List[object]
    $seen = @{}
    $lineNumber = 0
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $lineNumber += 1
        $trimmed = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.StartsWith("#")) {
            continue
        }
        $match = [regex]::Match(
            $trimmed,
            ('^(?<name>[A-Za-z0-9][A-Za-z0-9._-]*)==' +
                '(?<version>[^\s;#]+)(?:\s*;\s*' +
                '(?<marker>python_version\s*(?:==|!=|<=|>=|<|>)\s*' +
                '(?<quote>["''])[0-9]+\.[0-9]+\k<quote>))?$')
        )
        if (-not $match.Success) {
            throw (
                "Python lock '$Path' line $lineNumber is not an exact name==version " +
                "pin with an optional python_version marker: $trimmed"
            )
        }
        $name = $match.Groups["name"].Value
        $canonicalName = Get-CanonicalPythonName -Name $name
        $marker = $match.Groups["marker"].Value.Trim()
        if ($seen.ContainsKey($canonicalName)) {
            $existingMarkers = @($seen[$canonicalName])
            if (
                [string]::IsNullOrWhiteSpace($marker) -or
                @($existingMarkers | Where-Object {
                    [string]::IsNullOrWhiteSpace([string]$_) -or $_ -ceq $marker
                }).Count -gt 0
            ) {
                throw (
                    "Python lock contains duplicate or unconditional package " +
                    "'$canonicalName'. Conditional duplicates require distinct " +
                    "python_version markers."
                )
            }
            $seen[$canonicalName] = @($existingMarkers + $marker)
        }
        else {
            $seen[$canonicalName] = @($marker)
        }
        $version = $match.Groups["version"].Value
        $packages.Add([pscustomobject]@{
            Name = $name
            CanonicalName = $canonicalName
            Version = $version
            Marker = $marker
            Identity = "$canonicalName==$version;$marker"
        })
    }
    if ($packages.Count -eq 0) {
        throw "Python lock contains no exact dependencies: $Path"
    }
    return $packages.ToArray()
}

function Get-GeneratedTimestamp {
    param([string]$ExplicitValue)

    if ([string]::IsNullOrWhiteSpace($ExplicitValue)) {
        $sourceDateEpoch = [Environment]::GetEnvironmentVariable("SOURCE_DATE_EPOCH")
        if (-not [string]::IsNullOrWhiteSpace($sourceDateEpoch)) {
            [long]$epochSeconds = 0
            if (-not [long]::TryParse($sourceDateEpoch, [ref]$epochSeconds) -or $epochSeconds -lt 0) {
                throw "SOURCE_DATE_EPOCH must be a non-negative integer."
            }
            return [DateTimeOffset]::FromUnixTimeSeconds($epochSeconds).UtcDateTime.ToString(
                "yyyy-MM-ddTHH:mm:ssZ",
                [System.Globalization.CultureInfo]::InvariantCulture
            )
        }
        return [DateTime]::UtcNow.ToString(
            "yyyy-MM-ddTHH:mm:ssZ",
            [System.Globalization.CultureInfo]::InvariantCulture
        )
    }

    [DateTimeOffset]$parsed = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParse(
        $ExplicitValue,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::AssumeUniversal,
        [ref]$parsed
    )) {
        throw "GeneratedUtc is not a valid ISO-8601 timestamp: $ExplicitValue"
    }
    return $parsed.UtcDateTime.ToString(
        "yyyy-MM-ddTHH:mm:ssZ",
        [System.Globalization.CultureInfo]::InvariantCulture
    )
}

function Get-GitSourceState {
    param([Parameter(Mandatory = $true)][string]$Root)

    if ($null -eq (Get-Command git -ErrorAction SilentlyContinue)) {
        return [pscustomobject]@{
            State = "unavailable"
            Revision = "unknown"
        }
    }
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $inside = @(& git -C $Root rev-parse --is-inside-work-tree 2>&1)
        $insideExitCode = $LASTEXITCODE
        if ($insideExitCode -ne 0 -or ($inside -join "").Trim() -cne "true") {
            return [pscustomobject]@{
                State = "unavailable"
                Revision = "unknown"
            }
        }
        $revisionOutput = @(& git -C $Root rev-parse HEAD 2>&1)
        $revisionExitCode = $LASTEXITCODE
        $statusOutput = @(
            & git -C $Root status --porcelain --untracked-files=normal 2>&1
        )
        $statusExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($revisionExitCode -ne 0 -or $statusExitCode -ne 0) {
        return [pscustomobject]@{
            State = "unavailable"
            Revision = "unknown"
        }
    }
    $revision = ($revisionOutput -join "").Trim().ToLowerInvariant()
    if ($revision -notmatch '^[0-9a-f]{40}$') {
        return [pscustomobject]@{
            State = "unavailable"
            Revision = "unknown"
        }
    }
    return [pscustomobject]@{
        State = if ($statusOutput.Count -gt 0) { "dirty" } else { "clean" }
        Revision = $revision
    }
}

function New-SpdxPackage {
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$PackageVersion,
        [Parameter(Mandatory = $true)][string]$Purpose,
        [string]$Comment,
        [string]$DownloadLocation = "NOASSERTION",
        [object[]]$ExternalRefs = @(),
        [object[]]$Checksums = @()
    )

    $unknownText = (
        "License, supplier, copyright, and any unlisted package artifact digest " +
        "are NOASSERTION because this offline generator did not verify them."
    )
    $fullComment = if ([string]::IsNullOrWhiteSpace($Comment)) {
        $unknownText
    }
    else {
        "$Comment $unknownText"
    }
    $package = [ordered]@{
        SPDXID = $Id
        name = $Name
        versionInfo = if ([string]::IsNullOrWhiteSpace($PackageVersion)) {
            "unknown"
        }
        else {
            $PackageVersion
        }
        downloadLocation = $DownloadLocation
        filesAnalyzed = $false
        supplier = "NOASSERTION"
        licenseConcluded = "NOASSERTION"
        licenseDeclared = "NOASSERTION"
        copyrightText = "NOASSERTION"
        primaryPackagePurpose = $Purpose
        comment = $fullComment
    }
    if (@($ExternalRefs).Count -gt 0) {
        $package["externalRefs"] = @($ExternalRefs)
    }
    if (@($Checksums).Count -gt 0) {
        $package["checksums"] = @($Checksums)
    }
    return $package
}

if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $RepositoryRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
}
$builderUri = $null
if (
    [string]::IsNullOrWhiteSpace($BuilderId) -or
    -not [System.Uri]::TryCreate($BuilderId, [System.UriKind]::Absolute, [ref]$builderUri)
) {
    throw "BuilderId must be an absolute URI."
}
else {
    $RepositoryRoot = Resolve-InputPath -Path $RepositoryRoot -BaseDirectory (Get-Location).Path
}
$repoRoot = [System.IO.Path]::GetFullPath($RepositoryRoot)
if (-not (Test-Path -LiteralPath $repoRoot -PathType Container)) {
    throw "Repository root was not found: $repoRoot"
}

$outputRoot = Resolve-InputPath -Path $OutputDirectory -BaseDirectory (Get-Location).Path
$artifactRoot = $null
if (-not [string]::IsNullOrWhiteSpace($ArtifactDirectory)) {
    $artifactRoot = Resolve-InputPath -Path $ArtifactDirectory -BaseDirectory (Get-Location).Path
    if (-not (Test-Path -LiteralPath $artifactRoot -PathType Container)) {
        throw "Artifact directory was not found: $artifactRoot"
    }
}

$pyprojectPath = Join-Path $repoRoot "pyproject.toml"
$pythonLockPath = Join-Path $repoRoot "requirements-lock.txt"
$modelCatalogPath = Join-Path $repoRoot "model-catalog.default.json"
$desktopProjectPath = Join-Path $repoRoot "desktop/Zvec.Desktop/Zvec.Desktop.csproj"
foreach ($requiredPath in @(
    $pyprojectPath,
    $pythonLockPath,
    $modelCatalogPath,
    $desktopProjectPath
)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required SBOM input was not found: $requiredPath"
    }
}

$pyprojectContent = [System.IO.File]::ReadAllText($pyprojectPath)
$projectSection = Get-TomlSection -Content $pyprojectContent -Name "project"
$pythonProjectName = Get-TomlString -Section $projectSection -Name "name"
$projectVersion = Get-TomlString -Section $projectSection -Name "version"
$pythonVersionRequirement = Get-TomlString `
    -Section $projectSection `
    -Name "requires-python"
if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = $projectVersion
}
elseif ($Version -cne $projectVersion) {
    throw (
        "SBOM version '$Version' does not match pyproject.toml version " +
        "'$projectVersion'."
    )
}
if ($Version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$') {
    throw "SBOM version is not a supported SemVer value: $Version"
}

$pythonLocks = @(Read-PythonLock -Path $pythonLockPath)
$lockByName = @{}
foreach ($lockedPackage in $pythonLocks) {
    $canonicalName = $lockedPackage.CanonicalName
    if ($lockByName.ContainsKey($canonicalName)) {
        $lockByName[$canonicalName] = @(
            @($lockByName[$canonicalName]) + $lockedPackage
        )
    }
    else {
        $lockByName[$canonicalName] = @($lockedPackage)
    }
}

$projectDependencies = New-Object System.Collections.Generic.List[object]
foreach ($requirement in @(Get-TomlStringArray -Section $projectSection -Name "dependencies")) {
    $match = [regex]::Match(
        $requirement,
        '^(?<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]+\])?(?<specifier>.*)$'
    )
    if (-not $match.Success) {
        throw "Unsupported Python project dependency: $requirement"
    }
    $canonicalName = Get-CanonicalPythonName -Name $match.Groups["name"].Value
    if (-not $lockByName.ContainsKey($canonicalName)) {
        throw (
            "Python dependency '$canonicalName' is not pinned by " +
            "requirements-lock.txt."
        )
    }
    $locked = @($lockByName[$canonicalName])
    $exactMatch = [regex]::Match(
        $match.Groups["specifier"].Value,
        '^\s*==\s*(?<version>[^\s;]+)\s*$'
    )
    $mismatchedLocks = @(
        $locked |
            Where-Object {
                $_.Version -cne $exactMatch.Groups["version"].Value
            }
    )
    if ($exactMatch.Success -and $mismatchedLocks.Count -gt 0) {
        throw (
            "Python dependency '$canonicalName' requests version " +
            "'$($exactMatch.Groups['version'].Value)' but the lock pins " +
            "'$(@($locked.Version) -join ', ')'."
        )
    }
    $projectDependencies.Add([pscustomobject]@{
        CanonicalName = $canonicalName
        Requirement = $requirement
    })
}

$buildSection = Get-TomlSection -Content $pyprojectContent -Name "build-system"
$pythonBuildRequirements = New-Object System.Collections.Generic.List[object]
foreach ($requirement in @(Get-TomlStringArray -Section $buildSection -Name "requires")) {
    $match = [regex]::Match(
        $requirement,
        '^(?<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?<version>[^\s;]+)$'
    )
    if (-not $match.Success) {
        throw (
            "Python build requirement must be an exact name==version pin for " +
            "repeatable provenance: $requirement"
        )
    }
    $pythonBuildRequirements.Add([pscustomobject]@{
        Name = $match.Groups["name"].Value
        CanonicalName = Get-CanonicalPythonName -Name $match.Groups["name"].Value
        Version = $match.Groups["version"].Value
        Requirement = $requirement
    })
}

try {
    [xml]$desktopProject = [System.IO.File]::ReadAllText($desktopProjectPath)
}
catch {
    throw "Could not parse desktop project XML: $($_.Exception.Message)"
}
$desktopPropertyGroups = @($desktopProject.Project.PropertyGroup)
$targetFramework = [string](
    $desktopPropertyGroups |
        ForEach-Object { $_.TargetFramework } |
        Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } |
        Select-Object -First 1
)
if ([string]::IsNullOrWhiteSpace($targetFramework)) {
    $targetFramework = "unknown"
}
$desktopAssemblyName = [string](
    $desktopPropertyGroups |
        ForEach-Object { $_.AssemblyName } |
        Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } |
        Select-Object -First 1
)
if ([string]::IsNullOrWhiteSpace($desktopAssemblyName)) {
    $desktopAssemblyName = "Zvec.Desktop"
}

$normalizedRids = New-Object System.Collections.Generic.List[string]
foreach ($ridValue in @($RuntimeIdentifiers)) {
    if ([string]::IsNullOrWhiteSpace($ridValue)) {
        continue
    }
    $rid = $ridValue.Trim().ToLowerInvariant()
    if (-not $normalizedRids.Contains($rid)) {
        $normalizedRids.Add($rid)
    }
}

$bundledFrameworks = [ordered]@{}
$bundledFrameworkStatus = "unknown"
if (-not [string]::IsNullOrWhiteSpace($BundledFrameworksPath)) {
    $frameworkPath = Resolve-InputPath `
        -Path $BundledFrameworksPath `
        -BaseDirectory (Get-Location).Path
    if (-not (Test-Path -LiteralPath $frameworkPath -PathType Leaf)) {
        throw "Bundled framework manifest was not found: $frameworkPath"
    }
    try {
        $frameworkJson = [System.IO.File]::ReadAllText($frameworkPath) |
            ConvertFrom-Json
    }
    catch {
        throw "Could not parse bundled framework manifest: $($_.Exception.Message)"
    }
    foreach ($ridProperty in @($frameworkJson.PSObject.Properties | Sort-Object Name)) {
        $frameworkMap = [ordered]@{}
        foreach ($frameworkProperty in @(
            $ridProperty.Value.PSObject.Properties | Sort-Object Name
        )) {
            $frameworkName = [string]$frameworkProperty.Name
            $frameworkVersion = [string]$frameworkProperty.Value
            if (
                [string]::IsNullOrWhiteSpace($frameworkName) -or
                [string]::IsNullOrWhiteSpace($frameworkVersion)
            ) {
                throw "Bundled framework manifest contains an invalid entry."
            }
            $frameworkMap[$frameworkName] = $frameworkVersion
        }
        if ($frameworkMap.Count -eq 0) {
            throw "Bundled framework manifest has no entries for $($ridProperty.Name)."
        }
        $ridName = $ridProperty.Name.ToLowerInvariant()
        $bundledFrameworks[$ridName] = $frameworkMap
        if (-not $normalizedRids.Contains($ridName)) {
            $normalizedRids.Add($ridName)
        }
    }
    if ($bundledFrameworks.Count -eq 0) {
        throw "Bundled framework manifest is empty."
    }
    foreach ($rid in $normalizedRids) {
        if (-not $bundledFrameworks.Contains($rid)) {
            throw "Bundled framework manifest has no entry for runtime '$rid'."
        }
    }
    $bundledFrameworkStatus = "verified-from-runtimeconfig"
}

$frameworkRecords = @{}
if ($bundledFrameworks.Count -gt 0) {
    foreach ($rid in @($bundledFrameworks.Keys | Sort-Object)) {
        foreach ($frameworkName in @($bundledFrameworks[$rid].Keys | Sort-Object)) {
            $frameworkVersion = [string]$bundledFrameworks[$rid][$frameworkName]
            $recordKey = "$frameworkName|$frameworkVersion"
            if (-not $frameworkRecords.ContainsKey($recordKey)) {
                $frameworkRecords[$recordKey] = [pscustomobject]@{
                    Name = $frameworkName
                    Version = $frameworkVersion
                    Rids = New-Object System.Collections.Generic.List[string]
                }
            }
            $frameworkRecords[$recordKey].Rids.Add($rid)
        }
    }
}
else {
    foreach ($frameworkName in @(
        "Microsoft.NETCore.App",
        "Microsoft.WindowsDesktop.App"
    )) {
        $recordKey = "$frameworkName|unknown"
        $record = [pscustomobject]@{
            Name = $frameworkName
            Version = "unknown"
            Rids = New-Object System.Collections.Generic.List[string]
        }
        foreach ($rid in $normalizedRids) {
            $record.Rids.Add($rid)
        }
        $frameworkRecords[$recordKey] = $record
    }
}

$gitStateWasProvided = $PSBoundParameters.ContainsKey("GitState")
if ($gitStateWasProvided) {
    if ($GitState -eq "unavailable") {
        if (-not [string]::IsNullOrWhiteSpace($GitRevision)) {
            throw "GitRevision cannot be provided when GitState is unavailable."
        }
        $resolvedGitRevision = "unknown"
    }
    else {
        $resolvedGitRevision = $GitRevision.Trim().ToLowerInvariant()
        if ($resolvedGitRevision -notmatch '^[0-9a-f]{40}$') {
            throw "GitRevision must contain 40 hexadecimal characters for $GitState source."
        }
    }
    $resolvedGitState = $GitState
}
else {
    $discoveredGit = Get-GitSourceState -Root $repoRoot
    $resolvedGitState = $discoveredGit.State
    $resolvedGitRevision = $discoveredGit.Revision
}

$dotnetSdkSource = "caller"
if ([string]::IsNullOrWhiteSpace($DotNetSdkVersion)) {
    $dotnetCommand = Get-Command dotnet.exe,dotnet -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $dotnetCommand) {
        $previousErrorAction = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $versionOutput = @(& $dotnetCommand.Source --version 2>&1)
            $versionExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorAction
        }
        if ($versionExitCode -eq 0) {
            $DotNetSdkVersion = ($versionOutput -join "").Trim()
            $dotnetSdkSource = "dotnet --version"
        }
    }
    if ([string]::IsNullOrWhiteSpace($DotNetSdkVersion)) {
        $globalJsonPath = Join-Path $repoRoot "global.json"
        if (Test-Path -LiteralPath $globalJsonPath -PathType Leaf) {
            try {
                $globalJson = [System.IO.File]::ReadAllText($globalJsonPath) |
                    ConvertFrom-Json
                $DotNetSdkVersion = [string]$globalJson.sdk.version
                $dotnetSdkSource = "global.json declaration"
            }
            catch {
                $DotNetSdkVersion = "unknown"
                $dotnetSdkSource = "unknown"
            }
        }
    }
}
if ([string]::IsNullOrWhiteSpace($DotNetSdkVersion)) {
    $DotNetSdkVersion = "unknown"
    $dotnetSdkSource = "unknown"
}
$created = Get-GeneratedTimestamp -ExplicitValue $GeneratedUtc

$sbomFileName = "Zvec-Desktop-$Version.spdx.json"
$provenanceFileName = "Zvec-Desktop-$Version.provenance.json"
$artifactSubjects = New-Object System.Collections.Generic.List[object]
if ($null -ne $artifactRoot) {
    foreach ($artifact in @(
        Get-ChildItem -LiteralPath $artifactRoot -File |
            Where-Object {
                $_.Name -cne $sbomFileName -and
                $_.Name -cne $provenanceFileName -and
                $_.Name -cne "desktop-release.json" -and
                $_.Name -cne "SHA256SUMS.txt"
            } |
            Sort-Object Name
    )) {
        $artifactSubjects.Add([ordered]@{
            name = $artifact.Name
            digest = [ordered]@{
                sha256 = Get-FileSha256 -Path $artifact.FullName
            }
        })
    }
}

$materialRelativePaths = @(
    "pyproject.toml",
    "requirements-lock.txt",
    "model-catalog.default.json",
    "global.json",
    "zvec_launcher.py",
    "image_service.py",
    "image_vector_service/backend_instance_lock.py",
    "image_vector_service/backend_server.py",
    "image_vector_service/workspace_backup.py",
    "desktop/Zvec.Desktop/Zvec.Desktop.csproj",
    "desktop/Zvec.Desktop/Models/BackendInstanceModels.cs",
    "desktop/Zvec.Desktop/Models/WorkspaceMigrationModels.cs",
    "desktop/Zvec.Desktop/Services/BackendHostService.cs",
    "desktop/Zvec.Desktop/Services/BackendInstanceRegistry.cs",
    "desktop/Zvec.Desktop/Services/BackendSessionTokenStore.cs",
    "desktop/Zvec.Desktop/Services/LauncherConfigService.cs",
    "desktop/Zvec.Desktop/Services/WorkspaceMigrationService.cs",
    "installer/Zvec.Desktop.nsi",
    "installer/check-persistent-backend.ps1",
    "scripts/zvec.ps1",
    "scripts/publish-desktop.ps1",
    "scripts/validate-search-quality-gate.ps1",
    "scripts/verify_search_quality_gate.py",
    "scripts/generate-sbom.ps1"
)
$sourceMaterials = New-Object System.Collections.Generic.List[object]
foreach ($relativePath in $materialRelativePaths) {
    $nativeRelativePath = $relativePath.Replace(
        '/',
        [System.IO.Path]::DirectorySeparatorChar
    )
    $materialPath = Join-Path $repoRoot $nativeRelativePath
    if (Test-Path -LiteralPath $materialPath -PathType Leaf) {
        $sourceMaterials.Add([ordered]@{
            uri = "file:$relativePath"
            digest = [ordered]@{
                sha256 = Get-FileSha256 -Path $materialPath
            }
        })
    }
    else {
        $sourceMaterials.Add([ordered]@{
            uri = "file:$relativePath"
            annotations = [ordered]@{
                availability = "unknown"
                digest = "unknown"
            }
        })
    }
}

$packages = New-Object System.Collections.Generic.List[object]
$relationships = New-Object System.Collections.Generic.List[object]
$rootId = "SPDXRef-Package-Zvec-Desktop-Release"
$desktopId = "SPDXRef-Package-Zvec-Desktop"
$launcherId = "SPDXRef-Package-Zvec-PowerShell-Launcher"
$nativeRuntimeId = "SPDXRef-Package-Zvec-Native-Python-Runtime"
$pythonAppId = "SPDXRef-Package-Zvec-Python-Application"
$modelCatalogId = "SPDXRef-Package-Zvec-Default-Model-Catalog"
$dotnetSdkId = "SPDXRef-Package-DotNet-SDK"

$packages.Add((New-SpdxPackage `
    -Id $rootId `
    -Name "Zvec Desktop Release" `
    -PackageVersion $Version `
    -Purpose "APPLICATION" `
    -Comment (
        "Logical release package described by the sidecar artifacts. Git state: " +
        "$resolvedGitState; Git revision: $resolvedGitRevision."
    )))
$packages.Add((New-SpdxPackage `
    -Id $desktopId `
    -Name $desktopAssemblyName `
    -PackageVersion $Version `
    -Purpose "APPLICATION" `
    -Comment "Local .NET project targeting $targetFramework."))
$packages.Add((New-SpdxPackage `
    -Id $launcherId `
    -Name "zvec PowerShell launcher" `
    -PackageVersion $Version `
    -Purpose "APPLICATION" `
    -Comment "Local scripts/zvec.ps1 component bundled with the desktop publish."))
$packages.Add((New-SpdxPackage `
    -Id $nativeRuntimeId `
    -Name "zvec-image-search native Python runtime" `
    -PackageVersion $Version `
    -Purpose "APPLICATION" `
    -Comment (
        "Host-native runtime installed as a wheel in an isolated virtual " +
        "environment; Python requirement: $pythonVersionRequirement."
    )))
$packages.Add((New-SpdxPackage `
    -Id $pythonAppId `
    -Name $pythonProjectName `
    -PackageVersion $Version `
    -Purpose "APPLICATION" `
    -Comment "Local Python project installed into the isolated native runtime." `
    -ExternalRefs @([ordered]@{
        referenceCategory = "PACKAGE-MANAGER"
        referenceType = "purl"
        referenceLocator = (
            "pkg:pypi/$(Get-CanonicalPythonName -Name $pythonProjectName)@$Version"
        )
    })))
$packages.Add((New-SpdxPackage `
    -Id $modelCatalogId `
    -Name "Zvec default model catalog" `
    -PackageVersion $Version `
    -Purpose "FILE" `
    -Comment (
        "Default model capability catalog bundled at " +
        "backend/model-catalog.default.json."
    ) `
    -Checksums @([ordered]@{
        algorithm = "SHA256"
        checksumValue = Get-FileSha256 -Path $modelCatalogPath
    })))
$packages.Add((New-SpdxPackage `
    -Id $dotnetSdkId `
    -Name ".NET SDK" `
    -PackageVersion $DotNetSdkVersion `
    -Purpose "OTHER" `
    -Comment "Build SDK version source: $dotnetSdkSource."))

$pythonPackageIds = @{}
$pythonPackageIdsByName = @{}
foreach ($lockedPackage in @(
    $pythonLocks | Sort-Object CanonicalName, Version, Marker
)) {
    $identityHash = (Get-TextSha256 -Text $lockedPackage.Identity).Substring(0, 12)
    $id = (
        "SPDXRef-Package-Python-" +
        $lockedPackage.CanonicalName.Replace('-', '.') +
        "-$identityHash"
    )
    $pythonPackageIds[$lockedPackage.Identity] = $id
    if ($pythonPackageIdsByName.ContainsKey($lockedPackage.CanonicalName)) {
        $pythonPackageIdsByName[$lockedPackage.CanonicalName] = @(
            @($pythonPackageIdsByName[$lockedPackage.CanonicalName]) + $id
        )
    }
    else {
        $pythonPackageIdsByName[$lockedPackage.CanonicalName] = @($id)
    }
    $markerComment = if ([string]::IsNullOrWhiteSpace($lockedPackage.Marker)) {
        "The pin is unconditional."
    }
    else {
        "The pin applies when '$($lockedPackage.Marker)'."
    }
    $packages.Add((New-SpdxPackage `
        -Id $id `
        -Name $lockedPackage.Name `
        -PackageVersion $lockedPackage.Version `
        -Purpose "LIBRARY" `
        -Comment (
            "Exact runtime closure pin from requirements-lock.txt. The lock file " +
            "digest is recorded in provenance; no package wheel digest was available " +
            "offline. $markerComment"
        ) `
        -ExternalRefs @([ordered]@{
            referenceCategory = "PACKAGE-MANAGER"
            referenceType = "purl"
            referenceLocator = (
                "pkg:pypi/$($lockedPackage.CanonicalName)@$($lockedPackage.Version)"
            )
        })))
}

$pythonBuildIds = @{}
foreach ($buildRequirement in @($pythonBuildRequirements | Sort-Object CanonicalName)) {
    $id = "SPDXRef-Package-Python-Build-$($buildRequirement.CanonicalName.Replace('-', '.'))"
    $pythonBuildIds[$buildRequirement.CanonicalName] = $id
    $externalRefs = @()
    if ($buildRequirement.Version -cne "unknown") {
        $externalRefs = @([ordered]@{
            referenceCategory = "PACKAGE-MANAGER"
            referenceType = "purl"
            referenceLocator = (
                "pkg:pypi/$($buildRequirement.CanonicalName)@$($buildRequirement.Version)"
            )
        })
    }
    $packages.Add((New-SpdxPackage `
        -Id $id `
        -Name $buildRequirement.Name `
        -PackageVersion $buildRequirement.Version `
        -Purpose "OTHER" `
        -Comment "Build-system requirement '$($buildRequirement.Requirement)' from pyproject.toml." `
        -ExternalRefs $externalRefs))
}

$frameworkIds = New-Object System.Collections.Generic.List[string]
foreach ($recordKey in @($frameworkRecords.Keys | Sort-Object)) {
    $record = $frameworkRecords[$recordKey]
    $idSuffix = [regex]::Replace("$($record.Name)-$($record.Version)", '[^A-Za-z0-9.-]', '-')
    $id = "SPDXRef-Package-DotNet-Framework-$idSuffix"
    $frameworkIds.Add($id)
    $ridText = if ($record.Rids.Count -gt 0) {
        ($record.Rids.ToArray() | Sort-Object) -join ", "
    }
    else {
        "unknown"
    }
    $packages.Add((New-SpdxPackage `
        -Id $id `
        -Name $record.Name `
        -PackageVersion $record.Version `
        -Purpose "FRAMEWORK" `
        -Comment (
            "Self-contained bundled framework runtime identifiers: $ridText. " +
            "Version status: $bundledFrameworkStatus."
        )))
}

function Add-SpdxRelationship {
    param(
        [Parameter(Mandatory = $true)][string]$Element,
        [Parameter(Mandatory = $true)][string]$Type,
        [Parameter(Mandatory = $true)][string]$Related
    )

    $relationships.Add([ordered]@{
        spdxElementId = $Element
        relationshipType = $Type
        relatedSpdxElement = $Related
    })
}

Add-SpdxRelationship -Element "SPDXRef-DOCUMENT" -Type "DESCRIBES" -Related $rootId
Add-SpdxRelationship -Element $rootId -Type "CONTAINS" -Related $desktopId
Add-SpdxRelationship -Element $rootId -Type "CONTAINS" -Related $launcherId
Add-SpdxRelationship -Element $desktopId -Type "DEPENDS_ON" -Related $launcherId
Add-SpdxRelationship -Element $desktopId -Type "CONTAINS" -Related $modelCatalogId
Add-SpdxRelationship -Element $launcherId -Type "DEPENDS_ON" -Related $nativeRuntimeId
Add-SpdxRelationship -Element $nativeRuntimeId -Type "CONTAINS" -Related $pythonAppId
Add-SpdxRelationship -Element $dotnetSdkId -Type "BUILD_DEPENDENCY_OF" -Related $desktopId
foreach ($frameworkId in $frameworkIds) {
    Add-SpdxRelationship -Element $frameworkId -Type "RUNTIME_DEPENDENCY_OF" -Related $desktopId
}
foreach ($lockedPackage in $pythonLocks) {
    Add-SpdxRelationship `
        -Element $nativeRuntimeId `
        -Type "CONTAINS" `
        -Related $pythonPackageIds[$lockedPackage.Identity]
}
foreach ($projectDependency in $projectDependencies) {
    foreach ($packageId in @($pythonPackageIdsByName[$projectDependency.CanonicalName])) {
        Add-SpdxRelationship `
            -Element $packageId `
            -Type "RUNTIME_DEPENDENCY_OF" `
            -Related $pythonAppId
    }
}
foreach ($buildRequirement in $pythonBuildRequirements) {
    Add-SpdxRelationship `
        -Element $pythonBuildIds[$buildRequirement.CanonicalName] `
        -Type "BUILD_DEPENDENCY_OF" `
        -Related $pythonAppId
}

$namespaceSeed = [ordered]@{
    version = $Version
    generated_utc = $created
    git_state = $resolvedGitState
    git_revision = $resolvedGitRevision
    dotnet_sdk = $DotNetSdkVersion
    runtime_identifiers = @($normalizedRids.ToArray() | Sort-Object)
    bundled_frameworks = $bundledFrameworks
    python_lock_sha256 = Get-FileSha256 -Path $pythonLockPath
    model_catalog_sha256 = Get-FileSha256 -Path $modelCatalogPath
    python_version_requirement = $pythonVersionRequirement
    subjects = $artifactSubjects.ToArray()
    source_materials = $sourceMaterials.ToArray()
}
$namespaceDigest = Get-TextSha256 -Text (
    $namespaceSeed | ConvertTo-Json -Depth 12 -Compress
)
$documentNamespace = "urn:zvec:spdx:zvec-desktop:${Version}:$namespaceDigest"

$spdxDocument = [ordered]@{
    spdxVersion = $spdxVersion
    dataLicense = "CC0-1.0"
    SPDXID = "SPDXRef-DOCUMENT"
    name = "Zvec-Desktop-$Version"
    documentNamespace = $documentNamespace
    creationInfo = [ordered]@{
        created = $created
        creators = @("Tool: zvec-generate-sbom-$generatorVersion")
        comment = (
            "Generated entirely from local repository and publish inputs without " +
            "network access. NOASSERTION means the value was not verified offline; " +
            "it is not a license or digest claim."
        )
    }
    documentDescribes = @($rootId)
    packages = @($packages.ToArray() | Sort-Object { $_.SPDXID })
    relationships = @(
        $relationships.ToArray() |
            Sort-Object spdxElementId,relationshipType,relatedSpdxElement
    )
}

$null = [System.IO.Directory]::CreateDirectory($outputRoot)
$sbomPath = Join-Path $outputRoot $sbomFileName
Write-Utf8File `
    -Path $sbomPath `
    -Content (($spdxDocument | ConvertTo-Json -Depth 16) + "`n")
$sbomSubject = [ordered]@{
    name = $sbomFileName
    digest = [ordered]@{
        sha256 = Get-FileSha256 -Path $sbomPath
    }
}

$resolvedDependencies = New-Object System.Collections.Generic.List[object]
if ($resolvedGitRevision -ne "unknown") {
    $resolvedDependencies.Add([ordered]@{
        uri = "urn:zvec:source:git:repository-unknown"
        digest = [ordered]@{
            gitCommit = $resolvedGitRevision
        }
        annotations = [ordered]@{
            repository_uri = "unknown"
            git_state = $resolvedGitState
        }
    })
}
else {
    $resolvedDependencies.Add([ordered]@{
        uri = "urn:zvec:source:git:unknown"
        annotations = [ordered]@{
            repository_uri = "unknown"
            git_revision = "unknown"
            git_state = $resolvedGitState
        }
    })
}
foreach ($sourceMaterial in $sourceMaterials) {
    $resolvedDependencies.Add($sourceMaterial)
}
foreach ($lockedPackage in @(
    $pythonLocks | Sort-Object CanonicalName, Version, Marker
)) {
    $resolvedDependencies.Add([ordered]@{
        uri = "pkg:pypi/$($lockedPackage.CanonicalName)@$($lockedPackage.Version)"
        annotations = [ordered]@{
            version_source = "requirements-lock.txt"
            environment_marker = if (
                [string]::IsNullOrWhiteSpace($lockedPackage.Marker)
            ) { $null } else { $lockedPackage.Marker }
            package_artifact_digest = "unknown"
            license = "unknown"
        }
    })
}

$provenanceSubjects = New-Object System.Collections.Generic.List[object]
foreach ($subject in $artifactSubjects) {
    $provenanceSubjects.Add($subject)
}
$provenanceSubjects.Add($sbomSubject)
$provenance = [ordered]@{
    _type = "https://in-toto.io/Statement/v1"
    subject = $provenanceSubjects.ToArray()
    predicateType = "https://slsa.dev/provenance/v1"
    predicate = [ordered]@{
        buildDefinition = [ordered]@{
            buildType = "urn:zvec:build-type:desktop-powershell:v1"
            externalParameters = [ordered]@{
                version = $Version
                runtime_identifiers = @($normalizedRids.ToArray() | Sort-Object)
                target_framework = $targetFramework
                signing_status = $SigningStatus
            }
            internalParameters = [ordered]@{
                git_state = $resolvedGitState
                git_revision = $resolvedGitRevision
                dotnet_sdk_version = $DotNetSdkVersion
                dotnet_sdk_version_source = $dotnetSdkSource
                bundled_frameworks_status = $bundledFrameworkStatus
                bundled_frameworks = $bundledFrameworks
                python_lock_file = "requirements-lock.txt"
                python_lock_sha256 = Get-FileSha256 -Path $pythonLockPath
                model_catalog_file = "model-catalog.default.json"
                model_catalog_packaged_path = "backend/model-catalog.default.json"
                model_catalog_sha256 = Get-FileSha256 -Path $modelCatalogPath
                python_locked_dependencies = @(
                    $pythonLocks |
                        Sort-Object CanonicalName, Version, Marker |
                        ForEach-Object {
                            [ordered]@{
                                name = $_.CanonicalName
                                version = $_.Version
                                environment_marker = if (
                                    [string]::IsNullOrWhiteSpace($_.Marker)
                                ) { $null } else { $_.Marker }
                                package_artifact_digest = "unknown"
                                license = "unknown"
                            }
                        }
                )
                native_python_runtime = [ordered]@{
                    requires_python = $pythonVersionRequirement
                    isolation = "venv"
                    package_format = "wheel"
                    launcher = "scripts/zvec.ps1"
                }
                unknown_value_policy = (
                    "unknown and SPDX NOASSERTION are explicit unverified values; " +
                    "they are not inferred claims"
                )
                provenance_signature_status = "unsigned"
            }
            resolvedDependencies = $resolvedDependencies.ToArray()
        }
        runDetails = [ordered]@{
            builder = [ordered]@{
                id = $BuilderId
                version = [ordered]@{
                    generator = $generatorVersion
                    powershell = [string]$PSVersionTable.PSVersion
                    dotnet_sdk = $DotNetSdkVersion
                }
            }
            metadata = [ordered]@{
                finishedOn = $created
            }
        }
    }
}
$provenancePath = Join-Path $outputRoot $provenanceFileName
Write-Utf8File `
    -Path $provenancePath `
    -Content (($provenance | ConvertTo-Json -Depth 18) + "`n")

Write-Host "SPDX SBOM created: $sbomPath"
Write-Host "SLSA provenance statement created: $provenancePath"
