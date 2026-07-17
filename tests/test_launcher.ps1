Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $repoRoot "scripts\zvec.ps1"
$installer = Join-Path $repoRoot "scripts\install-zvec-command.cmd"
$testRoot = Join-Path $env:TEMP (
    "zvec-native-launcher-test-" + [guid]::NewGuid().ToString("N")
)
$configRoot = Join-Path $testRoot "config"
$installRoot = Join-Path $testRoot "installed"
$imageRoot = Join-Path $testRoot "图库 space"
$workspace = Join-Path $testRoot "workspace"
$results = Join-Path $testRoot "results"
$secondImageRoot = Join-Path $testRoot "archive-images"
$secondWorkspace = Join-Path $testRoot "archive-workspace"

$previousConfigHome = $env:ZVEC_CONFIG_HOME
$previousLegacyConfigHome = $env:ZVEC_DOCKER_CONFIG_HOME
$previousApiKey = $env:DASHSCOPE_API_KEY
$previousInstallDirectory = $env:ZVEC_COMMAND_INSTALL_DIR
$previousSourceMode = $env:ZVEC_NATIVE_USE_SOURCE
$previousNoOpen = $env:ZVEC_NO_OPEN
$previousUtf8Output = $env:ZVEC_UTF8_OUTPUT

function Invoke-Launcher {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Values)

    $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass `
        -File $launcher @Values 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Launcher failed: $($output -join [Environment]::NewLine)"
    }
    return $output
}

try {
    New-Item -ItemType Directory -Path `
        $configRoot,$installRoot,$imageRoot,$workspace,$results,`
        $secondImageRoot,$secondWorkspace -Force | Out-Null

    $env:ZVEC_CONFIG_HOME = $configRoot
    $env:ZVEC_DOCKER_CONFIG_HOME = $null
    $env:DASHSCOPE_API_KEY = "test-key-not-real"
    $env:ZVEC_COMMAND_INSTALL_DIR = $installRoot
    $env:ZVEC_NATIVE_USE_SOURCE = "1"
    $env:ZVEC_NO_OPEN = "1"
    $env:ZVEC_UTF8_OUTPUT = "1"

    $initOutput = Invoke-Launcher init $imageRoot --workspace $workspace `
        --results $results --skip-key
    if (($initOutput -join "`n") -notmatch "native Python") {
        throw "init did not report the native runtime."
    }
    $savedConfig = Get-Content -LiteralPath (Join-Path $configRoot "config.json") `
        -Raw -Encoding UTF8 | ConvertFrom-Json
    $savedLibrary = @($savedConfig.libraries)[0]
    if (
        [int]$savedConfig.schema_version -ne 3 -or
        [string]::IsNullOrWhiteSpace([string]$savedLibrary.workspace_directory) -or
        $savedLibrary.PSObject.Properties.Name -contains "workspace_type" -or
        $savedLibrary.PSObject.Properties.Name -contains "workspace_source" -or
        $savedConfig.PSObject.Properties.Name -contains "image_name"
    ) {
        throw "init did not create the native schema v3 configuration."
    }

    $doctorOutput = Invoke-Launcher doctor
    if (($doctorOutput -join "`n") -notmatch "Docker Desktop.*not required") {
        throw "doctor did not confirm that Docker is unnecessary."
    }
    if (($doctorOutput -join "`n") -match "Docker CLI not found") {
        throw "doctor still treats Docker as a runtime dependency."
    }

    $statsOutput = Invoke-Launcher stats
    $statsText = $statsOutput -join "`n"
    $statsJsonStart = $statsText.IndexOf("{")
    if ($statsJsonStart -lt 0) {
        throw "stats did not return JSON: $statsText"
    }
    $stats = $statsText.Substring($statsJsonStart) | ConvertFrom-Json
    if ([int]$stats.collection_stats.doc_count -ne 0) {
        throw "A new native workspace should start with zero documents."
    }
    $null = Invoke-Launcher roots
    $migrationOutput = Invoke-Launcher migrate-schema --dry-run
    if (($migrationOutput -join "`n") -notmatch '"api_requests": 0') {
        throw "Schema migration did not preserve the zero-API contract."
    }
    $buildOutput = Invoke-Launcher build --clean
    if (($buildOutput -join "`n") -notmatch "Docker.*no longer required") {
        throw "The compatibility build command did not describe native behavior."
    }
    $ensureOutput = Invoke-Launcher ensure-docker
    if (($ensureOutput -join "`n") -notmatch "Docker.*no longer required") {
        throw "ensure-docker compatibility command still requires an engine."
    }
    $openedResults = Invoke-Launcher results
    if (($openedResults -join "`n") -notmatch [regex]::Escape($results)) {
        throw "results did not resolve the configured host directory."
    }

    $null = Invoke-Launcher library-add Archive $secondImageRoot `
        --workspace $secondWorkspace
    $libraryState = (Invoke-Launcher library-list) -join "`n" | ConvertFrom-Json
    if (@($libraryState.libraries).Count -ne 2) {
        throw "library-add did not persist a second native library."
    }
    $archive = @($libraryState.libraries) | Where-Object { $_.name -eq "Archive" }
    if ($null -eq $archive) {
        throw "library-list did not return the added library."
    }
    if ([string]$archive.workspace_directory -ne $secondWorkspace) {
        throw "library-add did not preserve the host workspace directory."
    }
    $null = Invoke-Launcher library-rename $archive.id RenamedArchive
    $null = Invoke-Launcher library-disable $archive.id
    $null = Invoke-Launcher library-remove $archive.id

    & cmd.exe /d /c $installer *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Command installer failed."
    }
    $installedCommand = Join-Path $installRoot "zvec.exe"
    $helpOutput = & $installedCommand help 2>&1
    if (
        $LASTEXITCODE -ne 0 -or
        ($helpOutput -join "`n") -notmatch "Zvec native launcher"
    ) {
        throw "Installed native zvec command did not run."
    }

    Write-Host "Native launcher tests passed."
}
finally {
    $env:ZVEC_CONFIG_HOME = $previousConfigHome
    $env:ZVEC_DOCKER_CONFIG_HOME = $previousLegacyConfigHome
    $env:DASHSCOPE_API_KEY = $previousApiKey
    $env:ZVEC_COMMAND_INSTALL_DIR = $previousInstallDirectory
    $env:ZVEC_NATIVE_USE_SOURCE = $previousSourceMode
    $env:ZVEC_NO_OPEN = $previousNoOpen
    $env:ZVEC_UTF8_OUTPUT = $previousUtf8Output
    if (Test-Path -LiteralPath $testRoot) {
        $resolved = (Resolve-Path -LiteralPath $testRoot).Path
        $tempRoot = (Resolve-Path -LiteralPath $env:TEMP).Path
        if (
            $resolved.StartsWith(
                $tempRoot + [System.IO.Path]::DirectorySeparatorChar,
                [System.StringComparison]::OrdinalIgnoreCase
            )
        ) {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
}
