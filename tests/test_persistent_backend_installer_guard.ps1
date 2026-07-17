[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$guardScript = Join-Path $repoRoot "installer\check-persistent-backend.ps1"
if (-not (Test-Path -LiteralPath $guardScript -PathType Leaf)) {
    throw "Persistent backend installer guard was not found: $guardScript"
}

$testRoot = Join-Path $env:TEMP (
    "zvec-installer-guard-contract-" + [guid]::NewGuid().ToString("N")
)
$backendDirectory = Join-Path $testRoot "backend"
$instancePath = Join-Path $backendDirectory "instance.json"
$runtimeConfigPath = Join-Path $backendDirectory "runtime-config.json"
$queryRoot = Join-Path $testRoot "query-staging"
$probeProcess = $null
$launchLockStream = $null
$backendLockStream = $null
$previousToken = $env:ZVEC_BACKEND_TOKEN

function Assert-GuardResult {
    param(
        [Parameter(Mandatory = $true)][int]$Expected,
        [Parameter(Mandatory = $true)][string]$Scenario
    )

    $result = @(Invoke-PersistentBackendGuard)
    if ($result.Count -ne 1 -or [int]$result[0] -ne $Expected) {
        throw (
            "Persistent backend guard returned unexpected output for $Scenario`: " +
            ($result -join " | ")
        )
    }
}

function Write-GuardDescriptor {
    param(
        [int]$ProcessId,
        [DateTime]$ProcessStartUtc = [DateTime]::UtcNow,
        [int]$Port = 49123,
        [switch]$OmitPid
    )

    $descriptor = [ordered]@{
        instance_id = "installer-guard-contract"
        host = "127.0.0.1"
        port = $Port
        process_start_utc = $ProcessStartUtc.ToUniversalTime().ToString(
            "O",
            [Globalization.CultureInfo]::InvariantCulture
        )
        config_fingerprint = "b" * 64
        query_root = [IO.Path]::GetFullPath($queryRoot).TrimEnd(
            [IO.Path]::DirectorySeparatorChar
        )
        runtime_config_path = [IO.Path]::GetFullPath($runtimeConfigPath)
        state = "ready"
        generated_utc = [DateTime]::UtcNow.ToString(
            "O",
            [Globalization.CultureInfo]::InvariantCulture
        )
    }
    if (-not $OmitPid) {
        $descriptor["wrapper_pid"] = $ProcessId
    }
    [IO.File]::WriteAllText(
        $instancePath,
        (($descriptor | ConvertTo-Json -Depth 4) + "`n"),
        [Text.UTF8Encoding]::new($false)
    )
}

function Stop-GuardProbeProcess {
    param([Parameter(Mandatory = $true)][Diagnostics.Process]$Process)

    $Process.Refresh()
    if (-not $Process.HasExited) {
        try {
            $Process.Kill($true)
        }
        catch [Management.Automation.MethodException] {
            $Process.Kill()
        }
        if (-not $Process.WaitForExit(15000)) {
            throw "The guard contract process did not stop."
        }
    }
}

try {
    New-Item -ItemType Directory `
        -Path $backendDirectory,$queryRoot `
        -Force |
        Out-Null
    [IO.File]::WriteAllText(
        $runtimeConfigPath,
        "{}",
        [Text.UTF8Encoding]::new($false)
    )
    # A secret sentinel proves that neither guard results nor failures echo the token.
    $env:ZVEC_BACKEND_TOKEN = "installer-guard-secret-sentinel"

    . $guardScript -ConfigHome $testRoot

    Remove-Item -LiteralPath $instancePath -Force -ErrorAction SilentlyContinue
    Assert-GuardResult -Expected 0 -Scenario "missing descriptor"

    $launchLockPath = Join-Path $backendDirectory "launch.lock"
    $launchLockStream = [IO.File]::Open(
        $launchLockPath,
        [IO.FileMode]::OpenOrCreate,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None
    )
    Assert-GuardResult `
        -Expected 35 `
        -Scenario "startup lock held before descriptor registration"
    $launchLockStream.Dispose()
    $launchLockStream = $null
    Assert-GuardResult -Expected 0 -Scenario "released stale startup lock"

    $backendLockPath = Join-Path $backendDirectory "backend.lock"
    $backendLockStream = [IO.File]::Open(
        $backendLockPath,
        [IO.FileMode]::OpenOrCreate,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::ReadWrite
    )
    if ($backendLockStream.Length -eq 0) {
        $backendLockStream.WriteByte(0)
        $backendLockStream.Flush()
    }
    $backendLockStream.Lock(0, 1)
    Assert-GuardResult `
        -Expected 35 `
        -Scenario "backend byte-range lock held without descriptor"
    $backendLockStream.Unlock(0, 1)
    $backendLockStream.Dispose()
    $backendLockStream = $null
    Assert-GuardResult -Expected 0 -Scenario "released stale backend lock"

    [IO.File]::WriteAllText($instancePath, "{broken json")
    Assert-GuardResult -Expected 36 -Scenario "invalid descriptor JSON"

    [IO.File]::WriteAllText($instancePath, "")
    Assert-GuardResult -Expected 36 -Scenario "empty descriptor"

    $powershell = Join-Path $PSHOME "powershell.exe"
    $probeProcess = Start-Process `
        -FilePath $powershell `
        -ArgumentList @(
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Start-Sleep -Seconds 300"
        ) `
        -WindowStyle Hidden `
        -PassThru
    Start-Sleep -Milliseconds 200
    $probeProcess.Refresh()
    if ($probeProcess.HasExited) {
        throw "The guard contract process exited unexpectedly."
    }
    $probePid = $probeProcess.Id
    $probeStartUtc = $probeProcess.StartTime.ToUniversalTime()
    Write-GuardDescriptor `
        -ProcessId $probePid `
        -ProcessStartUtc $probeStartUtc
    Assert-GuardResult -Expected 35 -Scenario "matching live PID and start time"

    Stop-GuardProbeProcess -Process $probeProcess
    $probeProcess.Dispose()
    $probeProcess = $null

    function Invoke-LoopbackHealthRequest {
        param([Parameter(Mandatory = $true)][int]$Port)
        return [pscustomobject]@{ State = "connection_refused" }
    }
    Assert-GuardResult -Expected 0 -Scenario "stale PID and refused loopback port"

    Write-GuardDescriptor -OmitPid
    function Invoke-LoopbackHealthRequest {
        param([Parameter(Mandatory = $true)][int]$Port)
        return [pscustomobject]@{
            State = "response"
            StatusCode = 401
            Body = ""
        }
    }
    Assert-GuardResult -Expected 35 -Scenario "protected loopback health endpoint"

    function Invoke-LoopbackHealthRequest {
        param([Parameter(Mandatory = $true)][int]$Port)
        return [pscustomobject]@{
            State = "response"
            StatusCode = 200
            Body = (@{
                instance_id = "installer-guard-contract"
                config_fingerprint = "b" * 64
            } | ConvertTo-Json -Compress)
        }
    }
    Assert-GuardResult -Expected 35 -Scenario "matching public health identity"

    function Invoke-LoopbackHealthRequest {
        param([Parameter(Mandatory = $true)][int]$Port)
        return [pscustomobject]@{
            State = "response"
            StatusCode = 200
            Body = (@{
                instance_id = "different-instance"
                config_fingerprint = "c" * 64
            } | ConvertTo-Json -Compress)
        }
    }
    Assert-GuardResult -Expected 0 -Scenario "reused port with different identity"

    function Invoke-LoopbackHealthRequest {
        param([Parameter(Mandatory = $true)][int]$Port)
        return [pscustomobject]@{ State = "unknown" }
    }
    Assert-GuardResult -Expected 36 -Scenario "indeterminate loopback probe"

    Write-Host "Persistent backend installer guard contract passed."
}
finally {
    if ($null -ne $backendLockStream) {
        try { $backendLockStream.Unlock(0, 1) }
        catch { }
        $backendLockStream.Dispose()
    }
    if ($null -ne $launchLockStream) {
        $launchLockStream.Dispose()
    }
    if ($null -ne $probeProcess) {
        try {
            Stop-GuardProbeProcess -Process $probeProcess
        }
        finally {
            $probeProcess.Dispose()
        }
    }
    $env:ZVEC_BACKEND_TOKEN = $previousToken
    if (Test-Path -LiteralPath $testRoot -PathType Container) {
        $resolvedTestRoot = [IO.Path]::GetFullPath($testRoot)
        $resolvedTempRoot = [IO.Path]::GetFullPath($env:TEMP).TrimEnd(
            [IO.Path]::DirectorySeparatorChar
        )
        if ($resolvedTestRoot.StartsWith(
            $resolvedTempRoot + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
        }
    }
}
