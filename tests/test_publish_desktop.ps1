Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$publisher = Join-Path $repoRoot "scripts\publish-desktop.ps1"
$desktopProjectPath = Join-Path `
    $repoRoot "desktop\Zvec.Desktop\Zvec.Desktop.csproj"
$desktopProjectText = [System.IO.File]::ReadAllText($desktopProjectPath)
if (
    $desktopProjectText -notmatch 'requirements-lock\.txt' -or
    $desktopProjectText -notmatch 'backend\\requirements-lock\.txt' -or
    $desktopProjectText -notmatch 'model-catalog\.default\.json' -or
    $desktopProjectText -notmatch 'backend\\model-catalog\.default\.json'
) {
    throw "Desktop publish does not bundle the runtime lock and model catalog."
}
$testRoot = Join-Path $env:TEMP (
    "zvec-desktop-publish-test-" + [guid]::NewGuid().ToString("N")
)
$fakeBin = Join-Path $testRoot "bin"
$dotnetLog = Join-Path $testRoot "dotnet.log"
$nsisLog = Join-Path $testRoot "nsis.log"
$signToolLog = Join-Path $testRoot "signtool.log"
$previousScenario = $env:ZVEC_DESKTOP_TEST_SCENARIO
$previousDotnetLog = $env:ZVEC_DESKTOP_DOTNET_LOG
$previousNsisLog = $env:ZVEC_DESKTOP_NSIS_LOG
$previousSignToolLog = $env:ZVEC_DESKTOP_SIGNTOOL_LOG
$dirtyProbe = $null
$qualityFixtureRoot = $null
$sbomContract = Join-Path $repoRoot "tests\test_generate_sbom.ps1"

$sbomContractOutput = @(
    & powershell.exe -NoProfile -ExecutionPolicy Bypass `
        -File $sbomContract 2>&1
)
$sbomContractExitCode = $LASTEXITCODE
if ($sbomContractExitCode -ne 0) {
    throw (
        "SBOM contract test failed with exit code $sbomContractExitCode`: " +
        ($sbomContractOutput -join [Environment]::NewLine)
    )
}

function Invoke-Publisher {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$PublisherPath = $publisher
    )

    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(
            & powershell.exe -NoProfile -ExecutionPolicy Bypass `
                -File $PublisherPath @Arguments 2>&1
        )
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = [string[]]@($output | ForEach-Object { $_.ToString() })
    }
}

function Assert-Succeeded {
    param(
        [Parameter(Mandatory = $true)]$Result,
        [Parameter(Mandatory = $true)][string]$Scenario
    )

    if ($Result.ExitCode -ne 0) {
        throw "$Scenario failed: $($Result.Output -join [Environment]::NewLine)"
    }
}

function Assert-FailedWith {
    param(
        [Parameter(Mandatory = $true)]$Result,
        [Parameter(Mandatory = $true)][string]$Pattern,
        [Parameter(Mandatory = $true)][string]$Scenario
    )

    $message = $Result.Output -join [Environment]::NewLine
    if ($Result.ExitCode -eq 0 -or $message -notmatch $Pattern) {
        throw "$Scenario did not fail as expected: $message"
    }
}

function Reset-Probe {
    param([string]$Scenario = "success")

    foreach ($path in @($dotnetLog, $nsisLog, $signToolLog)) {
        [System.IO.File]::WriteAllText($path, "")
    }
    $env:ZVEC_DESKTOP_TEST_SCENARIO = $Scenario
}

function Assert-Checksums {
    param([Parameter(Mandatory = $true)][string]$ReleaseDirectory)

    $checksumPath = Join-Path $ReleaseDirectory "SHA256SUMS.txt"
    if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) {
        throw "SHA256SUMS.txt was not created."
    }
    $lines = @(Get-Content -LiteralPath $checksumPath -Encoding UTF8)
    if ($lines.Count -eq 0) {
        throw "SHA256SUMS.txt is empty."
    }
    $referencedFiles = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
        if ($line -notmatch '^(?<hash>[0-9a-f]{64})  (?<file>.+)$') {
            throw "Invalid checksum line: $line"
        }
        $artifactPath = Join-Path $ReleaseDirectory $Matches["file"]
        if (-not (Test-Path -LiteralPath $artifactPath -PathType Leaf)) {
            throw "Checksum references a missing artifact: $artifactPath"
        }
        $actual = (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash
        if ($actual -cne $Matches["hash"].ToUpperInvariant()) {
            throw "Checksum mismatch for $artifactPath"
        }
        $referencedFiles.Add($Matches["file"])
    }
    $expectedFiles = @(
        Get-ChildItem -LiteralPath $ReleaseDirectory -File |
            Where-Object { $_.Name -cne "SHA256SUMS.txt" } |
            ForEach-Object { $_.Name } |
            Sort-Object
    )
    $actualFiles = @($referencedFiles.ToArray() | Sort-Object)
    if (($expectedFiles -join "`n") -cne ($actualFiles -join "`n")) {
        throw (
            "SHA256SUMS.txt does not cover the complete release directory. " +
            "Expected: $($expectedFiles -join ', '); actual: " +
            ($actualFiles -join ', ')
        )
    }
}

function Get-ZipStatusText {
    param(
        [Parameter(Mandatory = $true)][string]$ZipPath,
        [string]$EntryName = "SIGNING-STATUS.txt"
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $entry = $archive.Entries |
            Where-Object {
                (Split-Path -Leaf $_.FullName) -ceq $EntryName
            } |
            Select-Object -First 1
        if ($null -eq $entry) {
            throw "ZIP does not contain $EntryName`: $ZipPath"
        }
        $stream = $entry.Open()
        $reader = New-Object System.IO.StreamReader($stream)
        try {
            return $reader.ReadToEnd()
        }
        finally {
            $reader.Dispose()
            $stream.Dispose()
        }
    }
    finally {
        $archive.Dispose()
    }
}

try {
    New-Item -ItemType Directory -Path $fakeBin -Force | Out-Null

    $dotnetSource = @'
using System;
using System.IO;

public static class DesktopDotNetFake {
    private static void WritePe(string path, ushort machine) {
        var bytes = new byte[512];
        bytes[0] = 0x4d;
        bytes[1] = 0x5a;
        Array.Copy(BitConverter.GetBytes(0x80), 0, bytes, 0x3c, 4);
        bytes[0x80] = 0x50;
        bytes[0x81] = 0x45;
        Array.Copy(BitConverter.GetBytes(machine), 0, bytes, 0x84, 2);
        File.WriteAllBytes(path, bytes);
    }

    public static int Main(string[] args) {
        var log = Environment.GetEnvironmentVariable("ZVEC_DESKTOP_DOTNET_LOG");
        File.AppendAllText(log, string.Join("|", args) + Environment.NewLine);

        if (args.Length == 1 && args[0] == "--version") {
            Console.WriteLine("8.0.422");
            return 0;
        }

        string rid = null;
        string output = null;
        string project = args.Length > 1 && args[0] == "publish" ? args[1] : null;
        for (var i = 0; i < args.Length - 1; i++) {
            if (args[i] == "--runtime") rid = args[i + 1];
            if (args[i] == "--output") output = args[i + 1];
        }
        if (rid == null || output == null || project == null) return 91;
        var scenario = Environment.GetEnvironmentVariable("ZVEC_DESKTOP_TEST_SCENARIO");
        if (scenario == "arm64-failure" && rid == "win-arm64") {
            Console.Error.WriteLine("forced arm64 publish failure");
            return 17;
        }

        Directory.CreateDirectory(output);
        Directory.CreateDirectory(Path.Combine(output, "zh-Hans"));
        Directory.CreateDirectory(Path.Combine(output, "scripts"));
        Directory.CreateDirectory(Path.Combine(output, "backend"));
        Directory.CreateDirectory(Path.Combine(output, "backend", "image_vector_service"));
        Directory.CreateDirectory(
            Path.Combine(output, "backend", "image_vector_service", "auto_tagging_assets")
        );
        WritePe(
            Path.Combine(output, "Zvec.Desktop.exe"),
            rid == "win-arm64" ? (ushort)0xAA64 : (ushort)0x8664
        );
        File.WriteAllText(Path.Combine(output, "Zvec.Desktop.dll"), "desktop dll");
        File.WriteAllText(Path.Combine(output, "hostfxr.dll"), "hostfxr");
        File.WriteAllText(Path.Combine(output, "coreclr.dll"), "coreclr");
        File.WriteAllText(
            Path.Combine(output, "zh-Hans", "PresentationFramework.resources.dll"),
            "zh-Hans resources"
        );
        if (scenario == "extra-satellite-culture") {
            Directory.CreateDirectory(Path.Combine(output, "fr"));
            File.WriteAllText(
                Path.Combine(output, "fr", "PresentationFramework.resources.dll"),
                "unexpected resources"
            );
        }
        File.WriteAllText(Path.Combine(output, "scripts", "zvec.ps1"), "Write-Host zvec");
        foreach (var file in new[] {
            "image_service.py",
            "zvec_launcher.py",
            "zvec_logging.py",
            "pyproject.toml",
            "requirements.txt",
            "requirements-lock.txt",
            "README.md"
        }) {
            File.WriteAllText(Path.Combine(output, "backend", file), file);
        }
        var modelCatalogSource = Path.GetFullPath(
            Path.Combine(
                Path.GetDirectoryName(project),
                "..",
                "..",
                "model-catalog.default.json"
            )
        );
        File.Copy(
            modelCatalogSource,
            Path.Combine(output, "backend", "model-catalog.default.json"),
            true
        );
        if (scenario == "catalog-mismatch") {
            File.WriteAllText(
                Path.Combine(output, "backend", "model-catalog.default.json"),
                "{}"
            );
        }
        File.WriteAllText(
            Path.Combine(output, "backend", "image_vector_service", "__init__.py"),
            ""
        );
        File.WriteAllText(
            Path.Combine(
                output,
                "backend",
                "image_vector_service",
                "backend_instance_lock.py"
            ),
            ""
        );
        File.WriteAllText(
            Path.Combine(
                output,
                "backend",
                "image_vector_service",
                "backend_server.py"
            ),
            ""
        );
        File.WriteAllText(
            Path.Combine(
                output,
                "backend",
                "image_vector_service",
                "image_data_uri.py"
            ),
            ""
        );
        File.WriteAllText(
            Path.Combine(
                output,
                "backend",
                "image_vector_service",
                "workspace_backup.py"
            ),
            ""
        );
        File.WriteAllText(
            Path.Combine(
                output,
                "backend",
                "image_vector_service",
                "auto_tagging_assets",
                "__init__.py"
            ),
            ""
        );
        File.WriteAllText(
            Path.Combine(
                output,
                "backend",
                "image_vector_service",
                "auto_tagging_assets",
                "v1.py"
            ),
            ""
        );
        File.WriteAllText(
            Path.Combine(
                output,
                "backend",
                "image_vector_service",
                "auto_tagging_assets",
                "v2.py"
            ),
            ""
        );
        File.WriteAllText(
            Path.Combine(
                output,
                "backend",
                "image_vector_service",
                "auto_tagging_assets",
                "v3.py"
            ),
            ""
        );
        File.WriteAllText(
            Path.Combine(output, "Zvec.Desktop.runtimeconfig.json"),
            "{\"runtimeOptions\":{\"tfm\":\"net8.0\",\"includedFrameworks\":[" +
            "{\"name\":\"Microsoft.NETCore.App\",\"version\":\"8.0.28\"}," +
            "{\"name\":\"Microsoft.WindowsDesktop.App\",\"version\":\"8.0.28\"}]}}"
        );
        return 0;
    }
}
'@
    $dotnetPath = Join-Path $fakeBin "dotnet.exe"
    Add-Type -TypeDefinition $dotnetSource `
        -OutputAssembly $dotnetPath `
        -OutputType ConsoleApplication

    $nsisSource = @'
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Threading;

public static class DesktopNsisFake {
    public static int Main(string[] args) {
        if (args.Length == 3 && args[0] == "--hold-lock") {
            using (var stream = new FileStream(
                args[1], FileMode.Open, FileAccess.Read, FileShare.None
            )) {
                File.WriteAllText(args[2], "locked");
                Thread.Sleep(3000);
            }
            return 0;
        }

        var log = Environment.GetEnvironmentVariable("ZVEC_DESKTOP_NSIS_LOG");
        File.AppendAllText(log, string.Join("|", args) + Environment.NewLine);
        string sourceDirectory = null;
        foreach (var argument in args) {
            const string sourcePrefix = "/DSOURCE_DIR=";
            if (argument.StartsWith(sourcePrefix, StringComparison.Ordinal)) {
                sourceDirectory = argument.Substring(sourcePrefix.Length).Trim('"');
            }
            const string prefix = "/DOUTPUT_FILE=";
            if (!argument.StartsWith(prefix, StringComparison.Ordinal)) continue;
            var output = argument.Substring(prefix.Length).Trim('"');
            Directory.CreateDirectory(Path.GetDirectoryName(output));
            File.WriteAllText(output, "fake nsis installer");
            if (
                Environment.GetEnvironmentVariable("ZVEC_DESKTOP_TEST_SCENARIO") ==
                    "transient-cleanup-lock" &&
                sourceDirectory != null
            ) {
                var target = Path.Combine(sourceDirectory, "Zvec.Desktop.dll");
                var signal = Path.Combine(
                    Path.GetTempPath(), "zvec-lock-" + Guid.NewGuid().ToString("N")
                );
                var helper = new ProcessStartInfo {
                    FileName = Assembly.GetExecutingAssembly().Location,
                    Arguments = "--hold-lock \"" + target + "\" \"" + signal + "\"",
                    UseShellExecute = false,
                    CreateNoWindow = true
                };
                Process.Start(helper);
                for (var attempt = 0; attempt < 100 && !File.Exists(signal); attempt++) {
                    Thread.Sleep(10);
                }
                if (!File.Exists(signal)) return 93;
                File.Delete(signal);
            }
            return 0;
        }
        return 92;
    }
}
'@
    $makeNsisPath = Join-Path $fakeBin "makensis.exe"
    Add-Type -TypeDefinition $nsisSource `
        -OutputAssembly $makeNsisPath `
        -OutputType ConsoleApplication

    $signToolSource = @'
using System;
using System.IO;

public static class DesktopSignToolFake {
    public static int Main(string[] args) {
        var log = Environment.GetEnvironmentVariable("ZVEC_DESKTOP_SIGNTOOL_LOG");
        File.AppendAllText(log, string.Join("|", args) + Environment.NewLine);
        return 0;
    }
}
'@
    $signToolPath = Join-Path $fakeBin "signtool.exe"
    Add-Type -TypeDefinition $signToolSource `
        -OutputAssembly $signToolPath `
        -OutputType ConsoleApplication

    $env:ZVEC_DESKTOP_DOTNET_LOG = $dotnetLog
    $env:ZVEC_DESKTOP_NSIS_LOG = $nsisLog
    $env:ZVEC_DESKTOP_SIGNTOOL_LOG = $signToolLog

    $qualityFixtureRoot = Join-Path `
        (Join-Path $repoRoot "artifacts") `
        (".desktop-quality-gate-test-" + [guid]::NewGuid().ToString("N"))
    $qualityFixturePython = @(
        Get-Command python -CommandType Application
    )[0].Source
    $fixtureOutput = @(
        & $qualityFixturePython `
            -m tests.search_quality.release_gate_fixture `
            --output-dir $qualityFixtureRoot `
            --repository-root $repoRoot 2>&1
    )
    if ($LASTEXITCODE -ne 0) {
        throw (
            "Could not generate formal search-quality release fixture: " +
            ($fixtureOutput -join [Environment]::NewLine)
        )
    }
    $qualityGatePath = [string]$fixtureOutput[-1]
    $qualityGate = Get-Content -LiteralPath $qualityGatePath -Raw -Encoding utf8 |
        ConvertFrom-Json
    $manifestFingerprint = [string]$qualityGate.holdout_split.manifest_fingerprint

    $missingQualityDirectory = Join-Path $testRoot "missing-quality-gate"
    $missingQuality = Invoke-Publisher -Arguments @(
        "-Version", "0.4.0",
        "-OutputDirectory", $missingQualityDirectory,
        "-DotNetPath", $dotnetPath,
        "-SkipInstaller",
        "-AllowDirty"
    )
    Assert-FailedWith -Result $missingQuality `
        -Pattern "Formal desktop publishing requires -SearchQualityGatePath" `
        -Scenario "default formal search-quality gate"

    Reset-Probe
    $dirtyProbe = Join-Path $repoRoot (
        ".desktop-publish-dirty-probe-" + [guid]::NewGuid().ToString("N")
    )
    [System.IO.File]::WriteAllText($dirtyProbe, "contract probe")
    $dirtyGateDirectory = Join-Path $testRoot "dirty-gate-release"
    $dirtyGate = Invoke-Publisher -Arguments @(
        "-Version", "0.4.0",
        "-OutputDirectory", $dirtyGateDirectory,
        "-DotNetPath", $dotnetPath,
        "-SkipInstaller",
        "-AllowUncertifiedSearchQualityPreview"
    )
    Assert-FailedWith -Result $dirtyGate `
        -Pattern "dirty Git worktree" `
        -Scenario "clean worktree gate"
    if (Test-Path -LiteralPath $dirtyGateDirectory) {
        throw "Dirty worktree gate exposed a release directory."
    }
    Reset-Probe
    $unsignedDirectory = Join-Path $testRoot "unsigned-release"
    $unsigned = Invoke-Publisher -Arguments @(
        "-Version", "0.4.0",
        "-OutputDirectory", $unsignedDirectory,
        "-DotNetPath", $dotnetPath,
        "-MakeNsisPath", $makeNsisPath,
        "-RequireInstaller",
        "-AllowDirty",
        "-AllowUncertifiedSearchQualityPreview"
    )
    Remove-Item -LiteralPath $dirtyProbe -Force
    Assert-Succeeded -Result $unsigned -Scenario "unsigned desktop release"
    $expectedUnsigned = @(
        "Zvec-Desktop-0.4.0-win-x64-unsigned.zip",
        "Zvec-Desktop-0.4.0-win-arm64-unsigned.zip",
        "Zvec-Desktop-0.4.0-win-x64-unsigned-setup.exe",
        "Zvec-Desktop-0.4.0-win-arm64-unsigned-setup.exe",
        "Zvec-Desktop-0.4.0.spdx.json",
        "Zvec-Desktop-0.4.0.provenance.json",
        "desktop-release.json",
        "SHA256SUMS.txt"
    )
    foreach ($name in $expectedUnsigned) {
        if (-not (Test-Path -LiteralPath (Join-Path $unsignedDirectory $name) -PathType Leaf)) {
            throw "Unsigned release is missing $name"
        }
    }
    $unsignedMetadata = Get-Content `
        -LiteralPath (Join-Path $unsignedDirectory "desktop-release.json") `
        -Raw -Encoding UTF8 | ConvertFrom-Json
    if (
        $unsignedMetadata.signing.status -cne "unsigned" -or
        $unsignedMetadata.installer.status -cne "built" -or
        $unsignedMetadata.git_state -cne "dirty" -or
        $unsignedMetadata.git_dirty -ne $true -or
        ([string]$unsignedMetadata.dotnet_sdk_version) -cne "8.0.422" -or
        ([string]$unsignedMetadata.bundled_frameworks.'win-x64'.'Microsoft.NETCore.App') -cne "8.0.28" -or
        ([string]$unsignedMetadata.supply_chain.sbom.format) -cne "SPDX-2.3-json" -or
        ([string]$unsignedMetadata.supply_chain.provenance.predicate_type) -cne "https://slsa.dev/provenance/v1" -or
        ([string]$unsignedMetadata.supply_chain.python_lock.file) -cne "requirements-lock.txt" -or
        ([string]$unsignedMetadata.supply_chain.model_catalog.packaged_path) -cne
            "backend/model-catalog.default.json" -or
        ([string]$unsignedMetadata.supply_chain.model_catalog.sha256) -cne (
            Get-FileHash `
                -LiteralPath (Join-Path $repoRoot "model-catalog.default.json") `
                -Algorithm SHA256
        ).Hash.ToLowerInvariant() -or
        ([string]$unsignedMetadata.supply_chain.python_runtime.mode) -cne "native-venv" -or
        $unsignedMetadata.supply_chain.python_runtime.docker_required -ne $false -or
        [int]$unsignedMetadata.schema_version -ne 3 -or
        $unsignedMetadata.search_quality.status -cne "uncertified-preview" -or
        $unsignedMetadata.search_quality.certified -ne $false -or
        [string]::IsNullOrWhiteSpace([string]$unsignedMetadata.search_quality.warning) -or
        @($unsignedMetadata.artifacts).Count -ne 6
    ) {
        throw "Unsigned release metadata is incorrect."
    }
    foreach ($supplyChainKind in @("sbom", "provenance")) {
        $entry = $unsignedMetadata.supply_chain.$supplyChainKind
        $path = Join-Path $unsignedDirectory ([string]$entry.file)
        $actualHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
        if ($actualHash -cne ([string]$entry.sha256).ToUpperInvariant()) {
            throw "Supply-chain metadata hash is incorrect for $supplyChainKind."
        }
    }
    foreach ($artifact in @($unsignedMetadata.artifacts)) {
        $path = Join-Path $unsignedDirectory ([string]$artifact.file)
        $actualHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
        if (
            $actualHash -cne ([string]$artifact.sha256).ToUpperInvariant() -or
            ([string]$artifact.sha256) -notmatch '^[0-9a-f]{64}$'
        ) {
            throw "desktop-release.json artifact hash is incorrect for $($artifact.file)."
        }
    }
    $publishedSbom = Get-Content `
        -LiteralPath (Join-Path $unsignedDirectory "Zvec-Desktop-0.4.0.spdx.json") `
        -Raw -Encoding UTF8 | ConvertFrom-Json
    $publishedFrameworks = @(
        $publishedSbom.packages |
            Where-Object { $_.primaryPackagePurpose -ceq "FRAMEWORK" }
    )
    if (
        $publishedSbom.spdxVersion -cne "SPDX-2.3" -or
        $publishedFrameworks.Count -ne 2 -or
        @($publishedFrameworks | Where-Object { $_.versionInfo -cne "8.0.28" }).Count -gt 0
    ) {
        throw "Published SPDX SBOM does not describe bundled frameworks."
    }
    $publishedProvenance = Get-Content `
        -LiteralPath (
            Join-Path $unsignedDirectory "Zvec-Desktop-0.4.0.provenance.json"
        ) -Raw -Encoding UTF8 | ConvertFrom-Json
    if (
        $publishedProvenance._type -cne "https://in-toto.io/Statement/v1" -or
        $publishedProvenance.predicate.buildDefinition.internalParameters.git_state -cne "dirty" -or
        $publishedProvenance.predicate.runDetails.builder.id -cne
            "urn:zvec:builder:publish-desktop.ps1" -or
        @($publishedProvenance.subject | Where-Object {
            $_.name -ceq "Zvec-Desktop-0.4.0-win-x64-unsigned.zip"
        }).Count -ne 1
    ) {
        throw "Published provenance statement is incomplete."
    }
    Assert-Checksums -ReleaseDirectory $unsignedDirectory
    $unsignedStatus = Get-ZipStatusText -ZipPath (
        Join-Path $unsignedDirectory "Zvec-Desktop-0.4.0-win-x64-unsigned.zip"
    )
    if ($unsignedStatus -notmatch "UNSIGNED PREVIEW BUILD") {
        throw "Unsigned ZIP was not marked as unsigned."
    }
    $expectedModelCatalog = [System.IO.File]::ReadAllText(
        (Join-Path $repoRoot "model-catalog.default.json")
    )
    $packagedModelCatalog = Get-ZipStatusText `
        -ZipPath (Join-Path $unsignedDirectory "Zvec-Desktop-0.4.0-win-x64-unsigned.zip") `
        -EntryName "model-catalog.default.json"
    if ($packagedModelCatalog -cne $expectedModelCatalog) {
        throw "Published ZIP does not contain the exact default model catalog."
    }
    $unsignedQualityStatus = Get-ZipStatusText `
        -ZipPath (Join-Path $unsignedDirectory "Zvec-Desktop-0.4.0-win-x64-unsigned.zip") `
        -EntryName "SEARCH-QUALITY-STATUS.txt"
    if ($unsignedQualityStatus -notmatch "UNCERTIFIED SEARCH QUALITY PREVIEW") {
        throw "Unsigned ZIP was not marked as search-quality uncertified."
    }
    $dotnetCalls = [System.IO.File]::ReadAllText($dotnetLog)
    if (
        $dotnetCalls -notmatch '--runtime\|win-x64' -or
        $dotnetCalls -notmatch '--runtime\|win-arm64' -or
        $dotnetCalls -notmatch '--self-contained\|true' -or
        $dotnetCalls -notmatch '-p:Version=0\.4\.0' -or
        $dotnetCalls -notmatch '-p:SatelliteResourceLanguages=zh-Hans'
    ) {
        throw "dotnet publish contract was not preserved: $dotnetCalls"
    }
    $nsisCalls = [System.IO.File]::ReadAllText($nsisLog)
    if (
        $nsisCalls -notmatch '/DRID=win-x64' -or
        $nsisCalls -notmatch '/DRID=win-arm64' -or
        $nsisCalls -notmatch '/DSIGNING_STATUS=unsigned'
    ) {
        throw "NSIS architecture contract was not preserved: $nsisCalls"
    }
    if (-not [string]::IsNullOrWhiteSpace([System.IO.File]::ReadAllText($signToolLog))) {
        throw "Unsigned release invoked SignTool."
    }

    Reset-Probe -Scenario "transient-cleanup-lock"
    $transientLockDirectory = Join-Path $testRoot "transient-lock-release"
    $transientLock = Invoke-Publisher -Arguments @(
        "-Version", "0.4.0",
        "-RuntimeIdentifiers", "win-x64",
        "-OutputDirectory", $transientLockDirectory,
        "-DotNetPath", $dotnetPath,
        "-MakeNsisPath", $makeNsisPath,
        "-RequireInstaller",
        "-AllowDirty",
        "-AllowUncertifiedSearchQualityPreview"
    )
    Assert-Succeeded `
        -Result $transientLock `
        -Scenario "transient staging-file lock cleanup"
    if (-not (Test-Path -LiteralPath $transientLockDirectory -PathType Container)) {
        throw "Transient-lock publish did not expose the completed release directory."
    }
    $transientStagingDirectories = @(
        Get-ChildItem `
            -LiteralPath $testRoot `
            -Directory `
            -Filter ".zvec-desktop-publish-*"
    )
    if ($transientStagingDirectories.Count -gt 0) {
        throw "Transient-lock publish left staging directories behind."
    }

    Reset-Probe
    $signedDirectory = Join-Path $testRoot "signed-release"
    $thumbprint = "0123456789ABCDEF0123456789ABCDEF01234567"
    $signed = Invoke-Publisher -Arguments @(
        "-Version", "0.4.0",
        "-OutputDirectory", $signedDirectory,
        "-DotNetPath", $dotnetPath,
        "-MakeNsisPath", $makeNsisPath,
        "-RequireInstaller",
        "-AllowDirty",
        "-SearchQualityGatePath", $qualityGatePath,
        "-CertificateThumbprint", $thumbprint,
        "-SignToolPath", $signToolPath
    )
    Assert-Succeeded -Result $signed -Scenario "signed desktop release"
    foreach ($name in @(
        "Zvec-Desktop-0.4.0-win-x64.zip",
        "Zvec-Desktop-0.4.0-win-arm64.zip",
        "Zvec-Desktop-0.4.0-win-x64-setup.exe",
        "Zvec-Desktop-0.4.0-win-arm64-setup.exe",
        "Zvec-Desktop-0.4.0.search-quality.json"
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $signedDirectory $name) -PathType Leaf)) {
            throw "Signed release is missing $name"
        }
    }
    $signedMetadata = Get-Content `
        -LiteralPath (Join-Path $signedDirectory "desktop-release.json") `
        -Raw -Encoding UTF8 | ConvertFrom-Json
    $signedSupplyChainArtifacts = @(
        $signedMetadata.artifacts |
            Where-Object { $_.kind -in @("spdx-sbom", "slsa-provenance") }
    )
    if (
        $signedMetadata.signing.status -cne "authenticode" -or
        $signedMetadata.search_quality.status -cne "certified" -or
        $signedMetadata.search_quality.certified -ne $true -or
        ([string]$signedMetadata.search_quality.holdout.manifest_fingerprint) -cne $manifestFingerprint -or
        $signedMetadata.supply_chain.sbom.signature_status -cne "unsigned" -or
        $signedMetadata.supply_chain.provenance.signature_status -cne "unsigned" -or
        $signedSupplyChainArtifacts.Count -ne 2 -or
        @($signedSupplyChainArtifacts | Where-Object { $_.signed -ne $false }).Count -gt 0
    ) {
        throw "Signed release metadata is incorrect."
    }
    $signedProvenance = Get-Content `
        -LiteralPath (
            Join-Path $signedDirectory "Zvec-Desktop-0.4.0.provenance.json"
        ) -Raw -Encoding UTF8 | ConvertFrom-Json
    if (
        $signedProvenance.predicate.buildDefinition.externalParameters.signing_status -cne
            "authenticode" -or
        $signedProvenance.predicate.buildDefinition.internalParameters.provenance_signature_status -cne
            "unsigned"
    ) {
        throw "Signed binary release misrepresented the sidecar signature status."
    }
    Assert-Checksums -ReleaseDirectory $signedDirectory
    $signedStatus = Get-ZipStatusText -ZipPath (
        Join-Path $signedDirectory "Zvec-Desktop-0.4.0-win-arm64.zip"
    )
    if (
        $signedStatus -notmatch "Authenticode signing and verification passed" -or
        $signedStatus -match "UNSIGNED"
    ) {
        throw "Signed ZIP has an incorrect signing marker."
    }
    $signedQualityStatus = Get-ZipStatusText `
        -ZipPath (Join-Path $signedDirectory "Zvec-Desktop-0.4.0-win-arm64.zip") `
        -EntryName "SEARCH-QUALITY-STATUS.txt"
    if ($signedQualityStatus -notmatch "SEARCH QUALITY CERTIFIED") {
        throw "Signed ZIP did not retain search-quality certification."
    }
    $signCalls = @(
        Get-Content -LiteralPath $signToolLog |
            Where-Object { $_ -match '^sign\|' }
    )
    $verifyCalls = @(
        Get-Content -LiteralPath $signToolLog |
            Where-Object { $_ -match '^verify\|' }
    )
    if ($signCalls.Count -ne 4 -or $verifyCalls.Count -ne 6) {
        throw "Unexpected SignTool call count: sign=$($signCalls.Count), verify=$($verifyCalls.Count)"
    }
    $allSignCalls = $signCalls -join "`n"
    if (
        $allSignCalls -notmatch '/fd\|SHA256' -or
        $allSignCalls -notmatch "/sha1\|$thumbprint" -or
        $allSignCalls -notmatch '/tr\|http://timestamp\.digicert\.com'
    ) {
        throw "SignTool arguments are incomplete: $allSignCalls"
    }

    Reset-Probe -Scenario "catalog-mismatch"
    $catalogMismatchDirectory = Join-Path $testRoot "catalog-mismatch-release"
    $catalogMismatch = Invoke-Publisher -Arguments @(
        "-Version", "0.4.0",
        "-RuntimeIdentifiers", "win-x64",
        "-OutputDirectory", $catalogMismatchDirectory,
        "-DotNetPath", $dotnetPath,
        "-SkipInstaller",
        "-AllowDirty",
        "-AllowUncertifiedSearchQualityPreview"
    )
    Assert-FailedWith -Result $catalogMismatch `
        -Pattern "Published default model catalog does not match" `
        -Scenario "model catalog payload integrity"
    if (Test-Path -LiteralPath $catalogMismatchDirectory) {
        throw "Model catalog mismatch exposed a partial release directory."
    }

    Reset-Probe -Scenario "extra-satellite-culture"
    $extraCultureDirectory = Join-Path $testRoot "extra-culture-release"
    $extraCulture = Invoke-Publisher -Arguments @(
        "-Version", "0.4.0",
        "-RuntimeIdentifiers", "win-x64",
        "-OutputDirectory", $extraCultureDirectory,
        "-DotNetPath", $dotnetPath,
        "-SkipInstaller",
        "-AllowDirty",
        "-AllowUncertifiedSearchQualityPreview"
    )
    Assert-FailedWith -Result $extraCulture `
        -Pattern "must contain only the zh-Hans satellite resource directory" `
        -Scenario "satellite resource language whitelist"
    if (Test-Path -LiteralPath $extraCultureDirectory) {
        throw "Extra satellite culture exposed a partial release directory."
    }

    Reset-Probe -Scenario "arm64-failure"
    $failedDirectory = Join-Path $testRoot "failed-release"
    $failed = Invoke-Publisher -Arguments @(
        "-Version", "0.4.0",
        "-OutputDirectory", $failedDirectory,
        "-DotNetPath", $dotnetPath,
        "-SkipInstaller",
        "-AllowDirty",
        "-AllowUncertifiedSearchQualityPreview"
    )
    Assert-FailedWith -Result $failed `
        -Pattern "exit code 17" `
        -Scenario "partial desktop publish"
    if (Test-Path -LiteralPath $failedDirectory) {
        throw "Failed desktop publish exposed a partial release directory."
    }
    $stagingDirectories = @(
        Get-ChildItem -LiteralPath $testRoot -Directory -Filter ".zvec-desktop-publish-*"
    )
    if ($stagingDirectories.Count -gt 0) {
        throw "Failed desktop publish left staging directories behind."
    }

    $sourceFixture = Join-Path $testRoot "source-unavailable"
    $fixtureScripts = Join-Path $sourceFixture "scripts"
    $fixtureDesktop = Join-Path $sourceFixture "desktop\Zvec.Desktop"
    New-Item -ItemType Directory `
        -Path $fixtureScripts,$fixtureDesktop `
        -Force |
        Out-Null
    $fixturePublisher = Join-Path $fixtureScripts "publish-desktop.ps1"
    Copy-Item -LiteralPath $publisher -Destination $fixturePublisher
    Copy-Item `
        -LiteralPath (Join-Path $repoRoot "scripts\generate-sbom.ps1") `
        -Destination (Join-Path $fixtureScripts "generate-sbom.ps1")
    Copy-Item `
        -LiteralPath (Join-Path $repoRoot "scripts\validate-search-quality-gate.ps1") `
        -Destination (Join-Path $fixtureScripts "validate-search-quality-gate.ps1")
    Copy-Item `
        -LiteralPath (Join-Path $repoRoot "scripts\zvec.ps1") `
        -Destination (Join-Path $fixtureScripts "zvec.ps1")
    Copy-Item `
        -LiteralPath (Join-Path $repoRoot "pyproject.toml") `
        -Destination (Join-Path $sourceFixture "pyproject.toml")
    Copy-Item `
        -LiteralPath (Join-Path $repoRoot "global.json") `
        -Destination (Join-Path $sourceFixture "global.json")
    Copy-Item `
        -LiteralPath (Join-Path $repoRoot "requirements-lock.txt") `
        -Destination (Join-Path $sourceFixture "requirements-lock.txt")
    Copy-Item `
        -LiteralPath (Join-Path $repoRoot "model-catalog.default.json") `
        -Destination (Join-Path $sourceFixture "model-catalog.default.json")
    Copy-Item `
        -LiteralPath (Join-Path $repoRoot "desktop\Zvec.Desktop\Zvec.Desktop.csproj") `
        -Destination (Join-Path $fixtureDesktop "Zvec.Desktop.csproj")
    Reset-Probe
    $unavailableBlocked = Invoke-Publisher -PublisherPath $fixturePublisher `
        -Arguments @(
            "-Version", "0.4.0",
            "-RuntimeIdentifiers", "win-x64",
            "-OutputDirectory", (Join-Path $testRoot "unavailable-blocked"),
            "-DotNetPath", $dotnetPath,
            "-SkipInstaller",
            "-AllowUncertifiedSearchQualityPreview"
        )
    Assert-FailedWith -Result $unavailableBlocked `
        -Pattern "not inside a Git worktree" `
        -Scenario "unavailable source gate"

    Reset-Probe
    $unavailableDirectory = Join-Path $testRoot "unavailable-allowed"
    $unavailableAllowed = Invoke-Publisher -PublisherPath $fixturePublisher `
        -Arguments @(
            "-Version", "0.4.0",
            "-RuntimeIdentifiers", "win-x64",
            "-OutputDirectory", $unavailableDirectory,
            "-DotNetPath", $dotnetPath,
            "-SkipInstaller",
            "-AllowDirty",
            "-AllowUncertifiedSearchQualityPreview"
        )
    Assert-Succeeded -Result $unavailableAllowed -Scenario "unavailable source override"
    $unavailableMetadata = Get-Content `
        -LiteralPath (Join-Path $unavailableDirectory "desktop-release.json") `
        -Raw -Encoding UTF8 | ConvertFrom-Json
    if (
        $unavailableMetadata.git_state -cne "unavailable" -or
        $null -ne $unavailableMetadata.git_revision -or
        $null -ne $unavailableMetadata.git_dirty -or
        ([string]$unavailableMetadata.supply_chain.sbom.file) -cne
            "Zvec-Desktop-0.4.0.spdx.json"
    ) {
        throw "Unavailable source metadata is incorrect."
    }
    $unavailableProvenance = Get-Content `
        -LiteralPath (
            Join-Path $unavailableDirectory "Zvec-Desktop-0.4.0.provenance.json"
        ) -Raw -Encoding UTF8 | ConvertFrom-Json
    if (
        $unavailableProvenance.predicate.buildDefinition.internalParameters.git_state -cne
            "unavailable" -or
        $unavailableProvenance.predicate.buildDefinition.internalParameters.git_revision -cne
            "unknown"
    ) {
        throw "Unavailable Git source was not explicit in provenance."
    }
    $unavailableZip = Get-ChildItem -LiteralPath $unavailableDirectory -Filter "*.zip" |
        Select-Object -First 1
    if ((Get-ZipStatusText -ZipPath $unavailableZip.FullName) -notmatch "UNVERIFIED SOURCE STATE") {
        throw "Unavailable source ZIP was not marked as unverifiable."
    }

    Write-Host "Desktop publish contract tests passed."
}
finally {
    $env:ZVEC_DESKTOP_TEST_SCENARIO = $previousScenario
    $env:ZVEC_DESKTOP_DOTNET_LOG = $previousDotnetLog
    $env:ZVEC_DESKTOP_NSIS_LOG = $previousNsisLog
    $env:ZVEC_DESKTOP_SIGNTOOL_LOG = $previousSignToolLog
    if ($null -ne $dirtyProbe -and (Test-Path -LiteralPath $dirtyProbe)) {
        Remove-Item -LiteralPath $dirtyProbe -Force
    }
    if ($null -ne $qualityFixtureRoot -and (Test-Path -LiteralPath $qualityFixtureRoot)) {
        $resolvedQualityFixture = (Resolve-Path -LiteralPath $qualityFixtureRoot).Path
        $artifactRoot = [System.IO.Path]::GetFullPath(
            (Join-Path $repoRoot "artifacts")
        ).TrimEnd([System.IO.Path]::DirectorySeparatorChar) +
            [System.IO.Path]::DirectorySeparatorChar
        if (
            -not $resolvedQualityFixture.StartsWith(
                $artifactRoot,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -or
            (Split-Path -Leaf $resolvedQualityFixture) -notlike ".desktop-quality-gate-test-*"
        ) {
            throw "Refusing to clean unexpected quality fixture path."
        }
        Remove-Item -LiteralPath $resolvedQualityFixture -Recurse -Force
    }
    if (Test-Path -LiteralPath $testRoot) {
        $resolved = (Resolve-Path -LiteralPath $testRoot).Path
        $tempRoot = (Resolve-Path -LiteralPath $env:TEMP).Path
        if ($resolved.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
}
