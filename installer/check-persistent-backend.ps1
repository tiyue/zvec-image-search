[CmdletBinding()]
param(
    [string]$ConfigHome
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$activeBackendExitCode = 35
$probeFailureExitCode = 36
$maximumDescriptorBytes = 64 * 1024

function Get-WindowsErrorCode {
    param([Parameter(Mandatory = $true)][Exception]$Exception)

    return ($Exception.HResult -band 0xFFFF)
}

function Get-ExclusiveFileLockState {
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        $stream = [IO.File]::Open(
            $Path,
            [IO.FileMode]::Open,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None
        )
    }
    catch [IO.FileNotFoundException] {
        return "absent"
    }
    catch [IO.DirectoryNotFoundException] {
        return "absent"
    }
    catch [IO.IOException] {
        if ((Get-WindowsErrorCode -Exception $_.Exception) -in @(32, 33)) {
            return "active"
        }
        return "unknown"
    }
    catch {
        return "unknown"
    }
    try {
        return "free"
    }
    finally {
        $stream.Dispose()
    }
}

function Get-ByteRangeLockState {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = $null
    $locked = $false
    try {
        $stream = [IO.File]::Open(
            $Path,
            [IO.FileMode]::Open,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::ReadWrite
        )
        try {
            # backend.lock is held with an OS byte-range lock by the Python service.
            # File existence is not meaningful because the lock file is retained after exit.
            $stream.Lock(0, 1)
            $locked = $true
            return "free"
        }
        catch [IO.IOException] {
            if ((Get-WindowsErrorCode -Exception $_.Exception) -in @(32, 33)) {
                return "active"
            }
            return "unknown"
        }
    }
    catch [IO.FileNotFoundException] {
        return "absent"
    }
    catch [IO.DirectoryNotFoundException] {
        return "absent"
    }
    catch [IO.IOException] {
        if ((Get-WindowsErrorCode -Exception $_.Exception) -in @(32, 33)) {
            return "active"
        }
        return "unknown"
    }
    catch {
        return "unknown"
    }
    finally {
        if ($null -ne $stream) {
            if ($locked) {
                try { $stream.Unlock(0, 1) }
                catch { }
            }
            $stream.Dispose()
        }
    }
}

function Get-BackendLockGuardResult {
    param([Parameter(Mandatory = $true)][string]$ResolvedConfigHome)

    $backendDirectory = Join-Path $ResolvedConfigHome "backend"
    $launchState = Get-ExclusiveFileLockState -Path (
        Join-Path $backendDirectory "launch.lock"
    )
    if ($launchState -eq "active") {
        # The desktop owns package files while preparing or attaching the backend.
        # Block upgrades even before instance.json has been written.
        return $activeBackendExitCode
    }
    if ($launchState -eq "unknown") {
        return $probeFailureExitCode
    }

    $backendState = Get-ByteRangeLockState -Path (
        Join-Path $backendDirectory "backend.lock"
    )
    if ($backendState -eq "active") {
        return $activeBackendExitCode
    }
    if ($backendState -eq "unknown") {
        return $probeFailureExitCode
    }
    return 0
}

function Resolve-ConfigHome {
    if (-not [string]::IsNullOrWhiteSpace($ConfigHome)) {
        $configured = $ConfigHome
    }
    else {
        $configured = [Environment]::GetEnvironmentVariable("ZVEC_CONFIG_HOME")
        if ([string]::IsNullOrWhiteSpace($configured)) {
            $configured = [Environment]::GetEnvironmentVariable(
                "ZVEC_DOCKER_CONFIG_HOME"
            )
        }
    }

    if ([string]::IsNullOrWhiteSpace($configured)) {
        $localAppData = [Environment]::GetFolderPath(
            [Environment+SpecialFolder]::LocalApplicationData
        )
        return [IO.Path]::GetFullPath(
            (Join-Path $localAppData "zvec-image-search")
        )
    }

    $expanded = [Environment]::ExpandEnvironmentVariables($configured.Trim())
    return [IO.Path]::GetFullPath($expanded)
}

function Get-JsonPropertyValue {
    param(
        [Parameter(Mandatory = $true)][psobject]$InputObject,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }
    return $property.Value
}

function Get-RegisteredProcessState {
    param([Parameter(Mandatory = $true)][psobject]$Descriptor)

    $startText = [string](Get-JsonPropertyValue `
        -InputObject $Descriptor `
        -Name "process_start_utc")
    $expectedStart = [DateTimeOffset]::MinValue
    $parsed = [DateTimeOffset]::TryParse(
        $startText,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AssumeUniversal -bor
            [Globalization.DateTimeStyles]::AdjustToUniversal,
        [ref]$expectedStart
    )
    if (-not $parsed) {
        return "unknown"
    }

    $candidatePids = [Collections.Generic.HashSet[int]]::new()
    foreach ($name in @("wrapper_pid", "server_pid")) {
        $value = Get-JsonPropertyValue -InputObject $Descriptor -Name $name
        if ($null -eq $value) {
            continue
        }
        $parsedPid = 0
        if ([int]::TryParse(
            [string]$value,
            [Globalization.NumberStyles]::Integer,
            [Globalization.CultureInfo]::InvariantCulture,
            [ref]$parsedPid
        ) -and $parsedPid -gt 0) {
            $null = $candidatePids.Add($parsedPid)
        }
    }
    if ($candidatePids.Count -eq 0) {
        return "unknown"
    }

    $accessFailed = $false
    foreach ($processId in $candidatePids) {
        try {
            $process = [Diagnostics.Process]::GetProcessById($processId)
        }
        catch [ArgumentException] {
            # The registered PID has exited. A stale descriptor must not block forever.
            continue
        }
        catch {
            $accessFailed = $true
            continue
        }
        try {
            try {
                $actualStart = $process.StartTime.ToUniversalTime()
                $difference = [Math]::Abs(
                    ($actualStart - $expectedStart.UtcDateTime).TotalSeconds
                )
                if ($difference -lt 2) {
                    return "active"
                }
            }
            catch {
                $accessFailed = $true
            }
            finally {
                $process.Dispose()
            }
        }
        catch { $accessFailed = $true }
    }
    if ($accessFailed) {
        return "unknown"
    }
    # Every registered PID either exited or now belongs to a process with a different
    # start time. This is the expected stale/PID-reuse case.
    return "stale"
}

function Invoke-LoopbackHealthRequest {
    param([Parameter(Mandatory = $true)][int]$Port)

    $client = [Net.Sockets.TcpClient]::new()
    try {
        # Connect directly to the registered loopback port so a refused connection can
        # be distinguished from a timeout. No proxy, credential, or token is involved.
        $asyncResult = $client.BeginConnect(
            [Net.IPAddress]::Loopback,
            $Port,
            $null,
            $null
        )
        try {
            if (-not $asyncResult.AsyncWaitHandle.WaitOne(2000)) {
                return [pscustomobject]@{ State = "unknown" }
            }
            $client.EndConnect($asyncResult)
        }
        catch [Net.Sockets.SocketException] {
            if ($_.Exception.SocketErrorCode -eq [Net.Sockets.SocketError]::ConnectionRefused) {
                return [pscustomobject]@{ State = "connection_refused" }
            }
            return [pscustomobject]@{ State = "unknown" }
        }
        finally {
            $asyncResult.AsyncWaitHandle.Dispose()
        }

        $stream = $client.GetStream()
        $stream.ReadTimeout = 2000
        $stream.WriteTimeout = 2000
        $requestBytes = [Text.Encoding]::ASCII.GetBytes(
            "GET /health HTTP/1.1`r`nHost: 127.0.0.1:$Port`r`n" +
            "Accept: application/json`r`nConnection: close`r`n`r`n"
        )
        $stream.Write($requestBytes, 0, $requestBytes.Length)
        $stream.Flush()

        $maximumResponseBytes = 64 * 1024
        $responseBytes = [IO.MemoryStream]::new()
        $buffer = [byte[]]::new(4096)
        while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            if ($responseBytes.Length + $read -gt $maximumResponseBytes) {
                return [pscustomobject]@{ State = "unknown" }
            }
            $responseBytes.Write($buffer, 0, $read)
        }
        $responseText = [Text.Encoding]::UTF8.GetString($responseBytes.ToArray())
        $headerEnd = $responseText.IndexOf("`r`n`r`n", [StringComparison]::Ordinal)
        if ($headerEnd -lt 0) {
            return [pscustomobject]@{ State = "unknown" }
        }
        $headerText = $responseText.Substring(0, $headerEnd)
        $statusMatch = [regex]::Match(
            $headerText,
            '^HTTP/\d+(?:\.\d+)?\s+(?<status>\d{3})(?:\s|$)',
            [Text.RegularExpressions.RegexOptions]::CultureInvariant
        )
        if (-not $statusMatch.Success) {
            return [pscustomobject]@{ State = "unknown" }
        }
        return [pscustomobject]@{
            State = "response"
            StatusCode = [int]$statusMatch.Groups["status"].Value
            Body = $responseText.Substring($headerEnd + 4)
        }
    }
    catch {
        return [pscustomobject]@{ State = "unknown" }
    }
    finally {
        $client.Dispose()
    }
}

function Get-RegisteredEndpointState {
    param([Parameter(Mandatory = $true)][psobject]$Descriptor)

    $instanceId = [string](Get-JsonPropertyValue `
        -InputObject $Descriptor `
        -Name "instance_id")
    $fingerprint = [string](Get-JsonPropertyValue `
        -InputObject $Descriptor `
        -Name "config_fingerprint")
    $portText = [string](Get-JsonPropertyValue `
        -InputObject $Descriptor `
        -Name "port")
    $port = 0
    if (
        [string]::IsNullOrWhiteSpace($instanceId) -or
        $instanceId.Length -gt 200 -or
        $fingerprint -notmatch '^[0-9a-f]{64}$' -or
        -not [int]::TryParse(
            $portText,
            [Globalization.NumberStyles]::Integer,
            [Globalization.CultureInfo]::InvariantCulture,
            [ref]$port
        ) -or
        $port -lt 1 -or
        $port -gt 65535
    ) {
        return "unknown"
    }

    $probe = Invoke-LoopbackHealthRequest -Port $port
    switch ([string]$probe.State) {
        "connection_refused" { return "stale" }
        "response" { }
        default { return "unknown" }
    }
    $statusCode = [int]$probe.StatusCode
    if ($statusCode -eq 401) {
            # The current backend protects /health. A 401 from the registered loopback
            # port proves that a service is alive without exposing or loading its token.
        return "active"
    }
    if ($statusCode -ne 200) {
        return "unknown"
    }

    try {
        $health = ([string]$probe.Body) | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        return "unknown"
    }
    $actualInstanceId = [string](Get-JsonPropertyValue `
        -InputObject $health `
        -Name "instance_id")
    if ($actualInstanceId -cne $instanceId) {
        return "stale"
    }
    $actualFingerprint = [string](Get-JsonPropertyValue `
        -InputObject $health `
        -Name "config_fingerprint")
    if ($actualFingerprint -ceq $fingerprint) {
        return "active"
    }
    return "stale"
}

function Invoke-PersistentBackendGuard {
    try {
        $resolvedConfigHome = Resolve-ConfigHome
        $lockResult = Get-BackendLockGuardResult `
            -ResolvedConfigHome $resolvedConfigHome
        if ($lockResult -ne 0) {
            return $lockResult
        }
        $instancePath = Join-Path $resolvedConfigHome "backend\instance.json"
        try {
            $instanceFile = Get-Item -LiteralPath $instancePath -ErrorAction Stop
        }
        catch [System.Management.Automation.ItemNotFoundException] {
            return 0
        }
        catch [System.IO.FileNotFoundException] {
            return 0
        }
        catch [System.IO.DirectoryNotFoundException] {
            return 0
        }
        if (
            $instanceFile.Length -le 0 -or
            $instanceFile.Length -gt $maximumDescriptorBytes
        ) {
            return $probeFailureExitCode
        }
        try {
            $descriptor = Get-Content `
                -LiteralPath $instancePath `
                -Raw `
                -Encoding utf8 `
                -ErrorAction Stop |
                ConvertFrom-Json -ErrorAction Stop
        }
        catch {
            return $probeFailureExitCode
        }
        if ($null -eq $descriptor) {
            return $probeFailureExitCode
        }

        $processState = Get-RegisteredProcessState -Descriptor $descriptor
        if ($processState -eq "active") {
            return $activeBackendExitCode
        }
        $endpointState = Get-RegisteredEndpointState -Descriptor $descriptor
        switch ($endpointState) {
            "active" { return $activeBackendExitCode }
            "stale" { return 0 }
            default { return $probeFailureExitCode }
        }
    }
    catch [System.IO.FileNotFoundException] {
        return 0
    }
    catch [System.IO.DirectoryNotFoundException] {
        return 0
    }
    catch {
        # Unexpected access or platform errors mean the installer cannot prove that an
        # active backend is absent. Fail closed without printing descriptor contents.
        return $probeFailureExitCode
    }
}

if ($MyInvocation.InvocationName -ne ".") {
    exit (Invoke-PersistentBackendGuard)
}
