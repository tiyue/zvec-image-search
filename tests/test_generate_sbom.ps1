Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$generator = Join-Path $repoRoot "scripts\generate-sbom.ps1"
$testRoot = Join-Path $env:TEMP (
    "zvec-sbom-contract-" + [guid]::NewGuid().ToString("N")
)

function Assert-Equal {
    param(
        [Parameter(Mandatory = $true)]$Actual,
        [Parameter(Mandatory = $true)]$Expected,
        [Parameter(Mandatory = $true)][string]$Message
    )

    if ($Actual -cne $Expected) {
        throw "$Message Expected '$Expected', got '$Actual'."
    }
}

function Get-PackageByPurl {
    param(
        [Parameter(Mandatory = $true)]$Document,
        [Parameter(Mandatory = $true)][string]$Purl
    )

    $matches = @(
        $Document.packages |
            Where-Object {
                $_.PSObject.Properties.Name -contains "externalRefs" -and
                @($_.externalRefs).referenceLocator -contains $Purl
            }
    )
    if ($matches.Count -ne 1) {
        throw "Expected one SPDX package for $Purl, found $($matches.Count)."
    }
    return $matches[0]
}

try {
    $null = New-Item -ItemType Directory -Path $testRoot -Force
    $artifactDirectory = Join-Path $testRoot "artifacts"
    $frameworkPath = Join-Path $testRoot "bundled-frameworks.json"
    $firstOutput = Join-Path $testRoot "first"
    $secondOutput = Join-Path $testRoot "second"
    $null = New-Item -ItemType Directory -Path $artifactDirectory -Force
    [System.IO.File]::WriteAllText(
        (Join-Path $artifactDirectory "Zvec-Desktop-0.4.0-win-x64-unsigned.zip"),
        "deterministic artifact fixture"
    )
    [System.IO.File]::WriteAllText(
        $frameworkPath,
        '{"win-x64":{"Microsoft.NETCore.App":"8.0.28",' +
        '"Microsoft.WindowsDesktop.App":"8.0.28"},' +
        '"win-arm64":{"Microsoft.NETCore.App":"8.0.28",' +
        '"Microsoft.WindowsDesktop.App":"8.0.28"}}'
    )

    $tokens = $null
    $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $generator,
        [ref]$tokens,
        [ref]$parseErrors
    )
    if ($parseErrors.Count -gt 0) {
        throw "SBOM generator has parse errors: $($parseErrors -join '; ')"
    }
    $forbiddenCommands = @(
        "Invoke-WebRequest",
        "Invoke-RestMethod",
        "Start-BitsTransfer",
        "curl",
        "curl.exe",
        "wget",
        "wget.exe"
    )
    foreach ($command in @(
        $ast.FindAll({
            param($node)
            $node -is [System.Management.Automation.Language.CommandAst]
        }, $true)
    )) {
        $name = $command.GetCommandName()
        if ($forbiddenCommands -contains $name) {
            throw "SBOM generator contains forbidden network command '$name'."
        }
    }
    $generatorSource = [System.IO.File]::ReadAllText($generator)
    if ($generatorSource -match 'System\.Net\.|HttpClient|WebClient') {
        throw "SBOM generator contains a hidden .NET network client."
    }

    $revision = (& git -C $repoRoot rev-parse HEAD).Trim().ToLowerInvariant()
    $commonArguments = @{
        RepositoryRoot = $repoRoot
        Version = "0.4.0"
        GitState = "dirty"
        GitRevision = $revision
        DotNetSdkVersion = "8.0.422"
        BundledFrameworksPath = $frameworkPath
        RuntimeIdentifiers = @("win-x64", "win-arm64")
        ArtifactDirectory = $artifactDirectory
        GeneratedUtc = "2026-07-13T01:02:03Z"
        SigningStatus = "unsigned"
    }
    & $generator @commonArguments -OutputDirectory $firstOutput
    & $generator @commonArguments -OutputDirectory $secondOutput

    $sbomName = "Zvec-Desktop-0.4.0.spdx.json"
    $provenanceName = "Zvec-Desktop-0.4.0.provenance.json"
    $firstSbomPath = Join-Path $firstOutput $sbomName
    $secondSbomPath = Join-Path $secondOutput $sbomName
    $firstProvenancePath = Join-Path $firstOutput $provenanceName
    $secondProvenancePath = Join-Path $secondOutput $provenanceName
    foreach ($path in @(
        $firstSbomPath,
        $secondSbomPath,
        $firstProvenancePath,
        $secondProvenancePath
    )) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "SBOM contract output was not created: $path"
        }
    }
    Assert-Equal `
        -Actual (Get-FileHash $firstSbomPath -Algorithm SHA256).Hash `
        -Expected (Get-FileHash $secondSbomPath -Algorithm SHA256).Hash `
        -Message "SPDX output is not repeatable."
    Assert-Equal `
        -Actual (Get-FileHash $firstProvenancePath -Algorithm SHA256).Hash `
        -Expected (Get-FileHash $secondProvenancePath -Algorithm SHA256).Hash `
        -Message "Provenance output is not repeatable."

    $sbom = Get-Content -LiteralPath $firstSbomPath -Raw -Encoding UTF8 |
        ConvertFrom-Json
    Assert-Equal $sbom.spdxVersion "SPDX-2.3" "SPDX version is incorrect."
    Assert-Equal $sbom.dataLicense "CC0-1.0" "SPDX data license is incorrect."
    Assert-Equal `
        $sbom.creationInfo.created `
        "2026-07-13T01:02:03Z" `
        "SPDX timestamp is incorrect."
    if ($sbom.documentNamespace -notmatch '^urn:zvec:spdx:zvec-desktop:0\.4\.0:[0-9a-f]{64}$') {
        throw "SPDX document namespace is not content-addressed."
    }

    $allowedPurposes = @(
        "APPLICATION",
        "FILE",
        "FRAMEWORK",
        "LIBRARY",
        "OTHER"
    )
    foreach ($package in @($sbom.packages)) {
        Assert-Equal `
            $package.licenseDeclared `
            "NOASSERTION" `
            "Package '$($package.name)' declared license was guessed."
        Assert-Equal `
            $package.licenseConcluded `
            "NOASSERTION" `
            "Package '$($package.name)' concluded license was guessed."
        if ($allowedPurposes -notcontains [string]$package.primaryPackagePurpose) {
            throw "Package '$($package.name)' has invalid SPDX package purpose."
        }
    }

    foreach ($locked in @(
        @{
            Purl = "pkg:pypi/numpy@2.2.6"
            Version = "2.2.6"
            Marker = 'python_version < "3.11"'
        },
        @{
            Purl = "pkg:pypi/numpy@2.3.5"
            Version = "2.3.5"
            Marker = 'python_version >= "3.11"'
        },
        @{ Purl = "pkg:pypi/pillow@12.3.0"; Version = "12.3.0" },
        @{ Purl = "pkg:pypi/zvec@0.5.1"; Version = "0.5.1" }
    )) {
        $package = Get-PackageByPurl -Document $sbom -Purl $locked.Purl
        Assert-Equal `
            ([string]$package.versionInfo) `
            $locked.Version `
            "Locked Python dependency version is incorrect."
        if ($package.PSObject.Properties.Name -contains "checksums") {
            throw "Python package '$($package.name)' contains an unverified artifact digest."
        }
        if (
            $locked.ContainsKey("Marker") -and
            ([string]$package.comment).IndexOf(
                $locked.Marker,
                [StringComparison]::Ordinal
            ) -lt 0
        ) {
            throw "Conditional Python pin marker is missing from the SPDX package."
        }
    }

    $frameworks = @(
        $sbom.packages |
            Where-Object { $_.primaryPackagePurpose -ceq "FRAMEWORK" }
    )
    if (
        $frameworks.Count -ne 2 -or
        @($frameworks | Where-Object { $_.versionInfo -cne "8.0.28" }).Count -gt 0
    ) {
        throw "Bundled .NET framework versions are incomplete."
    }
    $dotnetSdk = @(
        $sbom.packages |
            Where-Object { $_.name -ceq ".NET SDK" }
    )
    if ($dotnetSdk.Count -ne 1 -or $dotnetSdk[0].versionInfo -cne "8.0.422") {
        throw "Build SDK component is missing or incorrect."
    }
    foreach ($componentName in @(
        "Zvec.Desktop",
        "zvec PowerShell launcher",
        "zvec-image-search native Python runtime",
        "zvec-image-search",
        "Zvec default model catalog"
    )) {
        if (@($sbom.packages | Where-Object { $_.name -ceq $componentName }).Count -ne 1) {
            throw "Project component '$componentName' is missing from the SPDX SBOM."
        }
    }
    $modelCatalogPackage = @(
        $sbom.packages |
            Where-Object { $_.name -ceq "Zvec default model catalog" }
    )
    $expectedModelCatalogHash = (
        Get-FileHash `
            -LiteralPath (Join-Path $repoRoot "model-catalog.default.json") `
            -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if (
        $modelCatalogPackage.Count -ne 1 -or
        $modelCatalogPackage[0].primaryPackagePurpose -cne "FILE" -or
        @($modelCatalogPackage[0].checksums).Count -ne 1 -or
        ([string]$modelCatalogPackage[0].checksums[0].algorithm) -cne "SHA256" -or
        ([string]$modelCatalogPackage[0].checksums[0].checksumValue) -cne
            $expectedModelCatalogHash
    ) {
        throw "Default model catalog is missing or incorrectly hashed in the SPDX SBOM."
    }
    $modelCatalogRelationship = @(
        $sbom.relationships |
            Where-Object {
                $_.spdxElementId -ceq "SPDXRef-Package-Zvec-Desktop" -and
                $_.relationshipType -ceq "CONTAINS" -and
                $_.relatedSpdxElement -ceq
                    "SPDXRef-Package-Zvec-Default-Model-Catalog"
            }
    )
    if ($modelCatalogRelationship.Count -ne 1) {
        throw "Desktop SPDX package does not contain the default model catalog."
    }

    $ociPackages = @(
        $sbom.packages |
            Where-Object {
                $_.PSObject.Properties.Name -contains "externalRefs" -and
                @($_.externalRefs).referenceType -contains "oci"
            }
    )
    if ($ociPackages.Count -ne 0) {
        throw "Native desktop SBOM still contains a Docker/OCI runtime package."
    }

    $provenance = Get-Content `
        -LiteralPath $firstProvenancePath `
        -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($outputPath in @($firstSbomPath, $firstProvenancePath)) {
        $outputText = [System.IO.File]::ReadAllText($outputPath)
        if (
            $outputText.IndexOf(
                $repoRoot,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -ge 0 -or
            $outputText.IndexOf(
                $testRoot,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -ge 0
        ) {
            throw "Public supply-chain sidecar leaked an absolute local path."
        }
    }
    Assert-Equal `
        $provenance._type `
        "https://in-toto.io/Statement/v1" `
        "Provenance statement type is incorrect."
    Assert-Equal `
        $provenance.predicateType `
        "https://slsa.dev/provenance/v1" `
        "Provenance predicate type is incorrect."
    Assert-Equal `
        $provenance.predicate.buildDefinition.internalParameters.git_revision `
        $revision `
        "Git revision was not recorded."
    Assert-Equal `
        $provenance.predicate.buildDefinition.internalParameters.dotnet_sdk_version `
        "8.0.422" `
        "Build SDK version was not recorded."
    Assert-Equal `
        $provenance.predicate.runDetails.builder.id `
        "urn:zvec:builder:generate-sbom.ps1" `
        "Standalone provenance builder identity is incorrect."
    Assert-Equal `
        $provenance.predicate.buildDefinition.internalParameters.provenance_signature_status `
        "unsigned" `
        "Unsigned provenance status is not explicit."
    Assert-Equal `
        $provenance.predicate.buildDefinition.internalParameters.native_python_runtime.isolation `
        "venv" `
        "Native Python environment isolation is not explicit."
    Assert-Equal `
        $provenance.predicate.buildDefinition.internalParameters.native_python_runtime.package_format `
        "wheel" `
        "Native Python package format is not explicit."
    $lockedDependencies = @(
        $provenance.predicate.buildDefinition.internalParameters.python_locked_dependencies
    )
    foreach ($expectedMarker in @(
        'python_version < "3.11"',
        'python_version >= "3.11"'
    )) {
        if (
            @($lockedDependencies | Where-Object {
                $_.name -ceq "numpy" -and
                $_.environment_marker -ceq $expectedMarker
            }).Count -ne 1
        ) {
            throw "Conditional NumPy lock is missing from provenance: $expectedMarker"
        }
    }
    $lockMaterial = @(
        $provenance.predicate.buildDefinition.resolvedDependencies |
            Where-Object { $_.uri -ceq "file:requirements-lock.txt" }
    )
    $expectedLockHash = (
        Get-FileHash (Join-Path $repoRoot "requirements-lock.txt") -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if (
        $lockMaterial.Count -ne 1 -or
        ([string]$lockMaterial[0].digest.sha256) -cne $expectedLockHash
    ) {
        throw "Python lock source material digest is missing or incorrect."
    }
    $modelCatalogMaterial = @(
        $provenance.predicate.buildDefinition.resolvedDependencies |
            Where-Object { $_.uri -ceq "file:model-catalog.default.json" }
    )
    if (
        $modelCatalogMaterial.Count -ne 1 -or
        ([string]$modelCatalogMaterial[0].digest.sha256) -cne
            $expectedModelCatalogHash -or
        ([string]$provenance.predicate.buildDefinition.internalParameters.model_catalog_file) -cne
            "model-catalog.default.json" -or
        ([string]$provenance.predicate.buildDefinition.internalParameters.model_catalog_packaged_path) -cne
            "backend/model-catalog.default.json" -or
        ([string]$provenance.predicate.buildDefinition.internalParameters.model_catalog_sha256) -cne
            $expectedModelCatalogHash
    ) {
        throw "Default model catalog provenance material is missing or incorrect."
    }
    $artifactSubject = @(
        $provenance.subject |
            Where-Object { $_.name -ceq "Zvec-Desktop-0.4.0-win-x64-unsigned.zip" }
    )
    $expectedArtifactHash = (
        Get-FileHash (
            Join-Path $artifactDirectory "Zvec-Desktop-0.4.0-win-x64-unsigned.zip"
        ) -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if (
        $artifactSubject.Count -ne 1 -or
        ([string]$artifactSubject[0].digest.sha256) -cne $expectedArtifactHash
    ) {
        throw "Release artifact subject digest is missing or incorrect."
    }
    $sbomSubject = @(
        $provenance.subject | Where-Object { $_.name -ceq $sbomName }
    )
    $expectedSbomHash = (
        Get-FileHash $firstSbomPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if (
        $sbomSubject.Count -ne 1 -or
        ([string]$sbomSubject[0].digest.sha256) -cne $expectedSbomHash
    ) {
        throw "SPDX sidecar is not covered by the provenance statement."
    }

    $invalidRoot = Join-Path $testRoot "invalid-lock-repository"
    $null = New-Item -ItemType Directory -Path (
        Join-Path $invalidRoot "desktop\Zvec.Desktop"
    ) -Force
    [System.IO.File]::WriteAllText(
        (Join-Path $invalidRoot "pyproject.toml"),
        "[build-system]`nrequires = [`"setuptools==80.9.0`"]`n" +
        "build-backend = `"setuptools.build_meta`"`n`n" +
        "[project]`nname = `"fixture`"`nversion = `"0.4.0`"`n" +
        "requires-python = `">=3.10`"`n" +
        "dependencies = [`"Pillow>=10.0`"]`n"
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $invalidRoot "requirements-lock.txt"),
        "Pillow>=10.0`n"
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $invalidRoot "model-catalog.default.json"),
        "{}`n"
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $invalidRoot "desktop\Zvec.Desktop\Zvec.Desktop.csproj"),
        "<Project Sdk=`"Microsoft.NET.Sdk`"><PropertyGroup>" +
        "<TargetFramework>net8.0-windows</TargetFramework>" +
        "</PropertyGroup></Project>"
    )
    $invalidOutput = Join-Path $testRoot "invalid-output"
    $failureMessage = $null
    try {
        & $generator `
            -RepositoryRoot $invalidRoot `
            -OutputDirectory $invalidOutput `
            -GitState unavailable `
            -DotNetSdkVersion 8.0.422 `
            -GeneratedUtc 2026-07-13T01:02:03Z
    }
    catch {
        $failureMessage = $_.Exception.Message
    }
    if ($failureMessage -notmatch 'not an exact name==version pin') {
        throw "Unpinned Python dependency was not rejected: $failureMessage"
    }
    if (Test-Path -LiteralPath $invalidOutput) {
        throw "Invalid lock generation exposed a partial output directory."
    }

    Write-Host "SBOM and provenance contract tests passed."
}
finally {
    if (Test-Path -LiteralPath $testRoot) {
        $resolvedTestRoot = (Resolve-Path -LiteralPath $testRoot).Path
        $resolvedTemp = (Resolve-Path -LiteralPath $env:TEMP).Path
        if ($resolvedTestRoot.StartsWith(
            $resolvedTemp + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
        }
    }
}
