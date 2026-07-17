[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$MakeNsisPath,

    [Parameter(Mandatory = $true)]
    [string]$SourceDirectory,

    [ValidateSet("win-x64", "win-arm64")]
    [string]$RuntimeIdentifier = "win-x64",

    [string]$Version = "0.4.0",

    [string]$RollbackVersion = "0.3.0"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$installerScript = Join-Path $repoRoot "installer\Zvec.Desktop.nsi"
$backendGuardScript = Join-Path $repoRoot "installer\check-persistent-backend.ps1"
$makeNsis = [System.IO.Path]::GetFullPath($MakeNsisPath)
$source = [System.IO.Path]::GetFullPath($SourceDirectory)
if (-not (Test-Path -LiteralPath $makeNsis -PathType Leaf)) {
    throw "makensis.exe was not found at $makeNsis"
}
if (-not (Test-Path -LiteralPath $source -PathType Container)) {
    throw "Installer source directory was not found at $source"
}
if (-not (Test-Path -LiteralPath $installerScript -PathType Leaf)) {
    throw "NSIS script was not found at $installerScript"
}
if (-not (Test-Path -LiteralPath $backendGuardScript -PathType Leaf)) {
    throw "Persistent backend guard was not found at $backendGuardScript"
}

$versionMatch = [regex]::Match(
    $Version,
    '^(?<major>0|[1-9][0-9]*)\.(?<minor>0|[1-9][0-9]*)\.(?<patch>0|[1-9][0-9]*)'
)
if (-not $versionMatch.Success) {
    throw "Version must start with three numeric components."
}
$fileVersion = (
    $versionMatch.Groups["major"].Value + "." +
    $versionMatch.Groups["minor"].Value + "." +
    $versionMatch.Groups["patch"].Value + ".0"
)
$rollbackVersionMatch = [regex]::Match(
    $RollbackVersion,
    '^(?<major>0|[1-9][0-9]*)\.(?<minor>0|[1-9][0-9]*)\.(?<patch>0|[1-9][0-9]*)$'
)
if (-not $rollbackVersionMatch.Success) {
    throw "RollbackVersion must contain three numeric components."
}
$rollbackFileVersion = (
    $rollbackVersionMatch.Groups["major"].Value + "." +
    $rollbackVersionMatch.Groups["minor"].Value + "." +
    $rollbackVersionMatch.Groups["patch"].Value + ".0"
)
if ([version]$rollbackFileVersion -ge [version]$fileVersion) {
    throw "RollbackVersion must be lower than Version."
}

function Invoke-Native {
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

function Invoke-InstallerProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string]$Arguments = "/S"
    )

    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $Arguments `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    return $process.ExitCode
}

function Assert-PathUnderRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    $resolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $prefix = $resolvedRoot + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolvedPath.StartsWith(
        $prefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Path escaped the installer smoke root: $resolvedPath"
    }
}

function Get-PeMachine {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [System.IO.File]::OpenRead($Path)
    $reader = New-Object System.IO.BinaryReader($stream)
    try {
        if ($reader.ReadUInt16() -ne 0x5A4D) {
            throw "Not a PE file: $Path"
        }
        $stream.Position = 0x3C
        $peOffset = $reader.ReadInt32()
        $stream.Position = $peOffset
        if ($reader.ReadUInt32() -ne 0x00004550) {
            throw "Invalid PE signature: $Path"
        }
        return $reader.ReadUInt16()
    }
    finally {
        $reader.Dispose()
        $stream.Dispose()
    }
}

if ($null -eq ("DesktopInstallerSmokeNativeMethods" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class DesktopInstallerSmokeNativeMethods {
    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool PostMessage(
        IntPtr window,
        uint message,
        IntPtr wordParameter,
        IntPtr longParameter
    );
}
'@
}

function Request-TestWindowHide {
    param(
        [Parameter(Mandatory = $true)]
        [System.Diagnostics.Process]$Process,
        [int]$TimeoutMilliseconds = 10000
    )

    $Process.Refresh()
    if ($Process.HasExited -or $Process.MainWindowHandle -eq [IntPtr]::Zero) {
        return $false
    }
    $posted = [DesktopInstallerSmokeNativeMethods]::PostMessage(
        $Process.MainWindowHandle,
        0x0010,
        [IntPtr]::Zero,
        [IntPtr]::Zero
    )
    if (-not $posted) {
        return $false
    }

    $deadline = (Get-Date).AddMilliseconds($TimeoutMilliseconds)
    do {
        Start-Sleep -Milliseconds 100
        $Process.Refresh()
        if ($Process.HasExited) {
            return $false
        }
        if ($Process.MainWindowHandle -eq [IntPtr]::Zero) {
            return $true
        }
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Request-TestApplicationExit {
    param(
        [Parameter(Mandatory = $true)]
        [System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)]
        [string]$ExecutablePath,
        [int]$TimeoutMilliseconds = 60000
    )

    $Process.Refresh()
    if ($Process.HasExited) {
        return $true
    }
    $requestProcess = Start-Process `
        -FilePath $ExecutablePath `
        -ArgumentList "--exit-running-instance" `
        -WindowStyle Hidden `
        -PassThru
    try {
        if (-not $requestProcess.WaitForExit(10000)) {
            Stop-TestOwnedProcess -Process $requestProcess
            return $false
        }
        if ($requestProcess.ExitCode -ne 0) {
            return $false
        }
    }
    finally {
        $requestProcess.Dispose()
    }
    if (-not $Process.WaitForExit($TimeoutMilliseconds)) {
        return $false
    }
    return $true
}

function Stop-TestOwnedProcess {
    param(
        [Parameter(Mandatory = $true)]
        [System.Diagnostics.Process]$Process
    )

    $Process.Refresh()
    if ($Process.HasExited) {
        return
    }
    try {
        $Process.Kill($true)
    }
    catch [System.Management.Automation.MethodException] {
        # Windows PowerShell 5.1 uses .NET Framework, which lacks Kill(bool).
        $Process.Kill()
    }
    if (-not $Process.WaitForExit(15000)) {
        throw "The test-owned Zvec Desktop process could not be terminated."
    }
}

function Start-TestBackendGuardProcess {
    $powershell = Join-Path $PSHOME "powershell.exe"
    if (-not (Test-Path -LiteralPath $powershell -PathType Leaf)) {
        throw "Windows PowerShell was not found at $powershell"
    }
    $process = Start-Process `
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
    try {
        $deadline = (Get-Date).AddSeconds(10)
        do {
            Start-Sleep -Milliseconds 50
            $process.Refresh()
            if ($process.HasExited) {
                throw "The persistent backend guard probe exited unexpectedly."
            }
            try {
                $null = $process.StartTime
                return $process
            }
            catch [System.InvalidOperationException] {
                # The process exists but Windows has not exposed its start time yet.
            }
        } while ((Get-Date) -lt $deadline)
        throw "The persistent backend guard probe did not become observable."
    }
    catch {
        Stop-TestOwnedProcess -Process $process
        $process.Dispose()
        throw
    }
}

function Get-TestLoopbackPort {
    $listener = [Net.Sockets.TcpListener]::new(
        [Net.IPAddress]::Loopback,
        0
    )
    try {
        $listener.Start()
        return ([Net.IPEndPoint]$listener.LocalEndpoint).Port
    }
    finally {
        $listener.Stop()
    }
}

function Start-TestUnauthorizedBackendEndpoint {
    param([Parameter(Mandatory = $true)][string]$TestRoot)

    $powershell = Join-Path $PSHOME "powershell.exe"
    $port = Get-TestLoopbackPort
    $readyPath = Join-Path $TestRoot "backend-401-$port.ready"
    $escapedReadyPath = $readyPath.Replace("'", "''")
    $commandTemplate = @'
$ErrorActionPreference = "Stop"
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, __PORT__)
try {
    $listener.Start()
    [IO.File]::WriteAllText('__READY_PATH__', 'ready')
    $client = $listener.AcceptTcpClient()
    try {
        $stream = $client.GetStream()
        $buffer = [byte[]]::new(4096)
        $null = $stream.Read($buffer, 0, $buffer.Length)
        $bytes = [Text.Encoding]::ASCII.GetBytes(
            "HTTP/1.1 401 Unauthorized`r`nContent-Length: 0`r`nConnection: close`r`n`r`n"
        )
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush()
        $stream.Dispose()
    }
    finally {
        $client.Dispose()
    }
}
finally {
    $listener.Stop()
}
'@
    $command = $commandTemplate.Replace("__PORT__", [string]$port).Replace(
        "__READY_PATH__",
        $escapedReadyPath
    )
    $encodedCommand = [Convert]::ToBase64String(
        [Text.Encoding]::Unicode.GetBytes($command)
    )
    $process = Start-Process `
        -FilePath $powershell `
        -ArgumentList @(
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            $encodedCommand
        ) `
        -WindowStyle Hidden `
        -PassThru
    try {
        $deadline = (Get-Date).AddSeconds(10)
        while (
            -not (Test-Path -LiteralPath $readyPath -PathType Leaf) -and
            (Get-Date) -lt $deadline
        ) {
            Start-Sleep -Milliseconds 50
            $process.Refresh()
            if ($process.HasExited) {
                throw "The test 401 endpoint exited before becoming ready."
            }
        }
        if (-not (Test-Path -LiteralPath $readyPath -PathType Leaf)) {
            throw "The test 401 endpoint did not become ready."
        }
        return [pscustomobject]@{
            Process = $process
            Port = $port
        }
    }
    catch {
        Stop-TestOwnedProcess -Process $process
        $process.Dispose()
        throw
    }
}

function Write-TestBackendInstanceDescriptor {
    param(
        [Parameter(Mandatory = $true)][string]$ConfigHome,
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)][int]$Port,
        [switch]$OmitPid
    )

    $backendDirectory = Join-Path $ConfigHome "backend"
    $queryRoot = Join-Path $ConfigHome "query-staging"
    New-Item -ItemType Directory -Path $backendDirectory,$queryRoot -Force |
        Out-Null
    $runtimeConfigPath = Join-Path $backendDirectory "runtime-config.test.json"
    [System.IO.File]::WriteAllText(
        $runtimeConfigPath,
        "{}",
        [System.Text.UTF8Encoding]::new($false)
    )

    $Process.Refresh()
    $processStartUtc = $Process.StartTime.ToUniversalTime()
    $generatedUtc = [DateTime]::UtcNow
    if ($generatedUtc -lt $processStartUtc) {
        $generatedUtc = $processStartUtc
    }
    $descriptor = [ordered]@{
        instance_id = "installer-guard-$($Process.Id)"
        host = "127.0.0.1"
        port = $Port
        process_start_utc = $processStartUtc.ToString(
            "O",
            [Globalization.CultureInfo]::InvariantCulture
        )
        config_fingerprint = "a" * 64
        query_root = [System.IO.Path]::GetFullPath($queryRoot).TrimEnd(
            [System.IO.Path]::DirectorySeparatorChar
        )
        runtime_config_path = [System.IO.Path]::GetFullPath($runtimeConfigPath)
        state = "ready"
        generated_utc = $generatedUtc.ToString(
            "O",
            [Globalization.CultureInfo]::InvariantCulture
        )
    }
    if (-not $OmitPid) {
        $descriptor["wrapper_pid"] = $Process.Id
    }
    $instancePath = Join-Path $backendDirectory "instance.json"
    [System.IO.File]::WriteAllText(
        $instancePath,
        (($descriptor | ConvertTo-Json -Depth 4) + "`n"),
        [System.Text.UTF8Encoding]::new($false)
    )
    return $instancePath
}

if (@(Get-Process -Name "Zvec.Desktop" -ErrorAction SilentlyContinue).Count -gt 0) {
    throw "Close existing Zvec.Desktop processes before running installer smoke tests."
}

$testId = [guid]::NewGuid().ToString("N")
$testRoot = Join-Path $env:TEMP "zvec-installer-smoke-$testId"
$installDirectory = Join-Path $testRoot "fixed-install"
$overrideDirectory = Join-Path $testRoot "override-must-not-be-used"
$installerPath = Join-Path $testRoot "Zvec-Desktop-Smoke-$RuntimeIdentifier.exe"
$rollbackInstallerPath = Join-Path $testRoot (
    "Zvec-Desktop-Smoke-$RuntimeIdentifier-rollback.exe"
)
$registryKey = "Software\Zvec\DesktopSmoke\$testId"
$uninstallRegistryKey = (
    "Software\Microsoft\Windows\CurrentVersion\Uninstall\" +
    "ZvecDesktopSmoke-$testId"
)
$shortcutFileName = "Zvec Desktop Smoke $testId.lnk"
$shortcutDefine = '$SMPROGRAMS\' + $shortcutFileName
$shortcutPath = Join-Path (
    [Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)
) $shortcutFileName
$launchedProcess = $null
$backendProbeProcess = $null
$backendLockStream = $null
$previousConfigHome = $env:ZVEC_CONFIG_HOME
$previousLegacyConfigHome = $env:ZVEC_DOCKER_CONFIG_HOME
$previousRepoRoot = $env:ZVEC_REPO_ROOT
$previousApiKey = $env:DASHSCOPE_API_KEY
$previousApiUrl = $env:DASHSCOPE_API_URL

try {
    New-Item -ItemType Directory -Path $testRoot,$overrideDirectory -Force |
        Out-Null
    $isolatedConfigHome = Join-Path $testRoot "isolated-config"
    New-Item -ItemType Directory -Path $isolatedConfigHome -Force | Out-Null
    $env:ZVEC_CONFIG_HOME = $isolatedConfigHome
    $env:ZVEC_DOCKER_CONFIG_HOME = $isolatedConfigHome
    Remove-Item Env:ZVEC_REPO_ROOT -ErrorAction SilentlyContinue
    Remove-Item Env:DASHSCOPE_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:DASHSCOPE_API_URL -ErrorAction SilentlyContinue
    $overrideSentinel = Join-Path $overrideDirectory "must-survive.txt"
    [System.IO.File]::WriteAllText($overrideSentinel, "do not delete")

    Invoke-Native `
        -FilePath $makeNsis `
        -Arguments @(
            "/DVERSION=$Version",
            "/DFILE_VERSION=$fileVersion",
            "/DRID=$RuntimeIdentifier",
            "/DSOURCE_DIR=$source",
            "/DOUTPUT_FILE=$installerPath",
            "/DSIGNING_STATUS=unsigned",
            "/DINSTALL_DIR=$installDirectory",
            "/DPRODUCT_REG_KEY=$registryKey",
            "/DPRODUCT_UNINSTALL_KEY=$uninstallRegistryKey",
            "/DSTART_MENU_SHORTCUT=$shortcutDefine",
            $installerScript
        ) `
        -DisplayName "NSIS smoke installer compilation"
    Invoke-Native `
        -FilePath $makeNsis `
        -Arguments @(
            "/DVERSION=$RollbackVersion",
            "/DFILE_VERSION=$rollbackFileVersion",
            "/DRID=$RuntimeIdentifier",
            "/DSOURCE_DIR=$source",
            "/DOUTPUT_FILE=$rollbackInstallerPath",
            "/DSIGNING_STATUS=unsigned",
            "/DINSTALL_DIR=$installDirectory",
            "/DPRODUCT_REG_KEY=$registryKey",
            "/DPRODUCT_UNINSTALL_KEY=$uninstallRegistryKey",
            "/DSTART_MENU_SHORTCUT=$shortcutDefine",
            $installerScript
        ) `
        -DisplayName "NSIS rollback installer compilation"

    $unauthorizedEndpoint = Start-TestUnauthorizedBackendEndpoint -TestRoot $testRoot
    $backendProbeProcess = $unauthorizedEndpoint.Process
    $null = Write-TestBackendInstanceDescriptor `
        -ConfigHome $isolatedConfigHome `
        -Process $backendProbeProcess `
        -Port $unauthorizedEndpoint.Port `
        -OmitPid
    $unauthorizedBackendInstallExit = Invoke-InstallerProcess -FilePath $installerPath
    if ($unauthorizedBackendInstallExit -ne 35) {
        throw (
            "Authenticated persistent backend endpoint guard returned " +
            "$unauthorizedBackendInstallExit instead of 35."
        )
    }
    if (-not $backendProbeProcess.WaitForExit(10000)) {
        throw "The test 401 endpoint did not finish after the guard probe."
    }
    $backendProbeProcess.Dispose()
    $backendProbeProcess = $null

    $backendProbeProcess = Start-TestBackendGuardProcess
    $staleProbePort = Get-TestLoopbackPort
    $instancePath = Write-TestBackendInstanceDescriptor `
        -ConfigHome $isolatedConfigHome `
        -Process $backendProbeProcess `
        -Port $staleProbePort
    $activeBackendInstallExit = Invoke-InstallerProcess -FilePath $installerPath
    if ($activeBackendInstallExit -ne 35) {
        throw (
            "Active persistent backend guard returned " +
            "$activeBackendInstallExit instead of 35 during install."
        )
    }
    if (Test-Path -LiteralPath $installDirectory) {
        throw "Active persistent backend guard modified the install directory."
    }
    Stop-TestOwnedProcess -Process $backendProbeProcess
    $backendProbeProcess.Dispose()
    $backendProbeProcess = $null
    if (-not (Test-Path -LiteralPath $instancePath -PathType Leaf)) {
        throw "Persistent backend guard test did not retain its stale descriptor."
    }
    # Connection-refused and stale-PID semantics are covered without installing by
    # test_persistent_backend_installer_guard.ps1. Remove the synthetic descriptor so
    # this broader smoke does not depend on local firewall refusal behavior.
    Remove-Item -LiteralPath $instancePath -Force

    New-Item -ItemType Directory -Path $installDirectory -Force | Out-Null
    $unownedSentinel = Join-Path $installDirectory "unowned-sentinel.txt"
    [System.IO.File]::WriteAllText($unownedSentinel, "must survive refusal")
    $unownedExit = Invoke-InstallerProcess -FilePath $installerPath
    if ($unownedExit -ne 33) {
        throw "Unowned directory guard returned $unownedExit instead of 33."
    }
    if (-not (Test-Path -LiteralPath $unownedSentinel -PathType Leaf)) {
        throw "Installer deleted an unowned sentinel file."
    }
    Assert-PathUnderRoot -Path $installDirectory -Root $testRoot
    Remove-Item -LiteralPath $installDirectory -Recurse -Force

    $installExit = Invoke-InstallerProcess `
        -FilePath $installerPath `
        -Arguments ("/S /D=" + $overrideDirectory)
    if ($installExit -ne 0) {
        throw "Initial installer returned exit code $installExit."
    }
    if (-not (Test-Path -LiteralPath $overrideSentinel -PathType Leaf)) {
        throw "Installer honored /D and modified the caller-selected directory."
    }
    $markerPath = Join-Path $installDirectory ".zvec-desktop-install.ini"
    if (
        -not (Test-Path -LiteralPath $markerPath -PathType Leaf) -or
        [System.IO.File]::ReadAllText($markerPath) -notmatch
            '68CDA461-FA06-44DC-A735-2A9C42F46D13'
    ) {
        throw "Installer ownership marker was not written."
    }
    $installedExe = Join-Path $installDirectory "Zvec.Desktop.exe"
    $expectedMachine = if ($RuntimeIdentifier -eq "win-arm64") {
        0xAA64
    }
    else {
        0x8664
    }
    if ((Get-PeMachine -Path $installedExe) -ne $expectedMachine) {
        throw "Installed payload architecture does not match $RuntimeIdentifier."
    }
    if (-not (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) {
        throw "Per-user Start Menu shortcut was not created."
    }
    if (-not (Test-Path "Registry::HKEY_CURRENT_USER\$uninstallRegistryKey")) {
        throw "Per-user uninstall registration was not created."
    }

    $launchedProcess = Start-Process `
        -FilePath $installedExe `
        -WindowStyle Hidden `
        -PassThru
    $windowDeadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 250
        $launchedProcess.Refresh()
        if ($launchedProcess.HasExited) {
            throw "Installed Zvec Desktop exited before opening its window."
        }
    } while (
        $launchedProcess.MainWindowHandle -eq [IntPtr]::Zero -and
        (Get-Date) -lt $windowDeadline
    )
    if ($launchedProcess.MainWindowHandle -eq [IntPtr]::Zero) {
        throw "Installed Zvec Desktop did not create a window within 20 seconds."
    }
    # Allow asynchronous Window_Loaded work to finish so a close request cannot
    # be intercepted by the application's busy-operation confirmation dialog.
    Start-Sleep -Seconds 5

    $blockedSentinel = Join-Path $installDirectory "upgrade-blocked-sentinel.txt"
    [System.IO.File]::WriteAllText($blockedSentinel, "must survive running-app refusal")
    $runningUpgradeExit = Invoke-InstallerProcess -FilePath $installerPath
    if ($runningUpgradeExit -ne 32) {
        throw "Running-app upgrade guard returned $runningUpgradeExit instead of 32."
    }
    if (-not (Test-Path -LiteralPath $blockedSentinel -PathType Leaf)) {
        throw "Blocked upgrade modified the installed application."
    }

    if (-not (Request-TestWindowHide -Process $launchedProcess)) {
        throw "WM_CLOSE did not hide Zvec Desktop while keeping the process alive."
    }
    $hiddenUpgradeExit = Invoke-InstallerProcess -FilePath $installerPath
    if ($hiddenUpgradeExit -ne 32) {
        throw (
            "Tray-hidden running-app guard returned $hiddenUpgradeExit instead of 32."
        )
    }
    if (-not (Test-Path -LiteralPath $blockedSentinel -PathType Leaf)) {
        throw "Tray-hidden blocked upgrade modified the installed application."
    }

    $activationProcess = Start-Process `
        -FilePath $installedExe `
        -WindowStyle Hidden `
        -PassThru
    try {
        if (-not $activationProcess.WaitForExit(10000)) {
            Stop-TestOwnedProcess -Process $activationProcess
            throw "Second Zvec instance did not exit after requesting activation."
        }
        if ($activationProcess.ExitCode -ne 0) {
            throw (
                "Second Zvec instance could not restore the tray window; exit code " +
                "$($activationProcess.ExitCode)."
            )
        }
    }
    finally {
        $activationProcess.Dispose()
    }
    $restoreDeadline = (Get-Date).AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 100
        $launchedProcess.Refresh()
        if ($launchedProcess.HasExited) {
            throw "Primary Zvec instance exited while restoring its tray window."
        }
    } while (
        $launchedProcess.MainWindowHandle -eq [IntPtr]::Zero -and
        (Get-Date) -lt $restoreDeadline
    )
    if ($launchedProcess.MainWindowHandle -eq [IntPtr]::Zero) {
        throw "Launching Zvec again did not restore the tray-hidden window."
    }
    if (-not (Request-TestWindowHide -Process $launchedProcess)) {
        throw "Restored Zvec window could not return to the system tray."
    }

    if (-not (Request-TestApplicationExit `
        -Process $launchedProcess `
        -ExecutablePath $installedExe `
        -TimeoutMilliseconds 10000
    )) {
        throw (
            "Zvec Desktop did not finish its explicit exit within 10 seconds."
        )
    }
    $launchedProcess.Dispose()
    $launchedProcess = $null

    $noInstanceExitRequest = Start-Process `
        -FilePath $installedExe `
        -ArgumentList "--exit-running-instance" `
        -WindowStyle Hidden `
        -PassThru
    try {
        if (-not $noInstanceExitRequest.WaitForExit(10000)) {
            Stop-TestOwnedProcess -Process $noInstanceExitRequest
            throw "No-instance exit request did not finish."
        }
        if ($noInstanceExitRequest.ExitCode -ne 3) {
            throw (
                "No-instance exit request returned " +
                "$($noInstanceExitRequest.ExitCode) instead of 3."
            )
        }
    }
    finally {
        $noInstanceExitRequest.Dispose()
    }

    $upgradeSentinel = Join-Path $installDirectory "upgrade-cleanup-sentinel.txt"
    [System.IO.File]::WriteAllText($upgradeSentinel, "owned directory stale file")
    $upgradeExit = Invoke-InstallerProcess -FilePath $installerPath
    if ($upgradeExit -ne 0) {
        throw "Same-version upgrade returned exit code $upgradeExit."
    }
    if (Test-Path -LiteralPath $upgradeSentinel) {
        throw "Validated same-version upgrade did not clean the old product directory."
    }
    if (-not (Test-Path -LiteralPath $installedExe -PathType Leaf)) {
        throw "Same-version upgrade did not restore the application payload."
    }

    $rollbackExit = Invoke-InstallerProcess -FilePath $rollbackInstallerPath
    if ($rollbackExit -ne 0) {
        throw "Explicit rollback returned exit code $rollbackExit."
    }
    $rollbackMarker = [System.IO.File]::ReadAllText($markerPath)
    if ($rollbackMarker -notmatch "(?m)^Version=$([regex]::Escape($RollbackVersion))`r?$") {
        throw "Rollback did not update the ownership marker to $RollbackVersion."
    }
    if (
        -not (Test-Path -LiteralPath $installedExe -PathType Leaf) -or
        (Get-PeMachine -Path $installedExe) -ne $expectedMachine -or
        -not (Test-Path -LiteralPath $shortcutPath -PathType Leaf) -or
        -not (Test-Path "Registry::HKEY_CURRENT_USER\$uninstallRegistryKey")
    ) {
        throw "Rollback did not preserve the installed payload and registration."
    }
    $rollbackRegistration = Get-ItemProperty (
        "Registry::HKEY_CURRENT_USER\$uninstallRegistryKey"
    )
    if (([string]$rollbackRegistration.DisplayVersion) -cne $RollbackVersion) {
        throw "Rollback registration version was not updated."
    }

    $restoreExit = Invoke-InstallerProcess -FilePath $installerPath
    if ($restoreExit -ne 0) {
        throw "Restoring the current version returned exit code $restoreExit."
    }
    $restoredMarker = [System.IO.File]::ReadAllText($markerPath)
    if ($restoredMarker -notmatch "(?m)^Version=$([regex]::Escape($Version))`r?$") {
        throw "Current version restore did not update the ownership marker to $Version."
    }
    $restoredRegistration = Get-ItemProperty (
        "Registry::HKEY_CURRENT_USER\$uninstallRegistryKey"
    )
    if (
        ([string]$restoredRegistration.DisplayVersion) -cne $Version -or
        -not (Test-Path -LiteralPath $installedExe -PathType Leaf)
    ) {
        throw "Current version restore did not restore payload and registration."
    }

    $uninstaller = Join-Path $installDirectory "Uninstall.exe"
    # A real persistent backend always owns backend.lock.  Exercise that primary
    # uninstall boundary here instead of relying on PID/start-time formatting,
    # which can vary between Windows runner images.  Live-descriptor fallback is
    # already covered above during install and by the focused guard contract.
    $backendDirectory = Join-Path $isolatedConfigHome "backend"
    New-Item -ItemType Directory -Path $backendDirectory -Force | Out-Null
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
    # NSIS normally starts a temporary uninstaller copy and lets the original
    # bootstrap process return 0.  _?= runs the installed uninstaller directly,
    # so this boundary test observes the guard's real exit code without racing
    # the temporary child process.  It must be the final command-line argument.
    $activeBackendUninstallExit = Invoke-InstallerProcess `
        -FilePath $uninstaller `
        -Arguments ("/S _?=" + $installDirectory)
    if ($activeBackendUninstallExit -ne 35) {
        throw (
            "Active persistent backend guard returned " +
            "$activeBackendUninstallExit instead of 35 during uninstall."
        )
    }
    if (
        -not (Test-Path -LiteralPath $installedExe -PathType Leaf) -or
        -not (Test-Path -LiteralPath $markerPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $shortcutPath -PathType Leaf) -or
        -not (Test-Path "Registry::HKEY_CURRENT_USER\$uninstallRegistryKey")
    ) {
        throw "Blocked uninstall modified the installed application."
    }
    $backendLockStream.Unlock(0, 1)
    $backendLockStream.Dispose()
    $backendLockStream = $null

    $uninstallExit = Invoke-InstallerProcess -FilePath $uninstaller
    if ($uninstallExit -ne 0) {
        throw "Uninstaller returned exit code $uninstallExit."
    }
    $removeDeadline = (Get-Date).AddSeconds(10)
    while ((Test-Path -LiteralPath $installDirectory) -and (Get-Date) -lt $removeDeadline) {
        Start-Sleep -Milliseconds 250
    }
    if (Test-Path -LiteralPath $installDirectory) {
        throw "Uninstaller left the product directory behind."
    }
    if (Test-Path -LiteralPath $shortcutPath) {
        throw "Uninstaller left the Start Menu shortcut behind."
    }
    if (Test-Path "Registry::HKEY_CURRENT_USER\$uninstallRegistryKey") {
        throw "Uninstaller left its registration behind."
    }
    if (-not (Test-Path -LiteralPath $overrideSentinel -PathType Leaf)) {
        throw "Installer smoke modified the caller-selected override directory."
    }

    Write-Host (
        "Desktop installer persistent-backend guard, install, launch, upgrade, " +
        "rollback, restore and uninstall smoke passed."
    )
}
finally {
    if ($null -ne $backendLockStream) {
        try { $backendLockStream.Unlock(0, 1) }
        catch { }
        $backendLockStream.Dispose()
    }
    if ($null -ne $backendProbeProcess) {
        try {
            Stop-TestOwnedProcess -Process $backendProbeProcess
        }
        finally {
            $backendProbeProcess.Dispose()
        }
    }
    if ($null -ne $launchedProcess) {
        try {
            if (-not $launchedProcess.HasExited) {
                $closed = Request-TestApplicationExit `
                    -Process $launchedProcess `
                    -ExecutablePath $installedExe `
                    -TimeoutMilliseconds 10000
                if (-not $closed) {
                    Stop-TestOwnedProcess -Process $launchedProcess
                }
            }
        }
        finally {
            $launchedProcess.Dispose()
        }
    }
    Remove-Item -LiteralPath $shortcutPath -Force -ErrorAction SilentlyContinue
    Remove-Item `
        -LiteralPath "Registry::HKEY_CURRENT_USER\$uninstallRegistryKey" `
        -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item `
        -LiteralPath "Registry::HKEY_CURRENT_USER\$registryKey" `
        -Recurse -Force -ErrorAction SilentlyContinue
    $env:ZVEC_CONFIG_HOME = $previousConfigHome
    $env:ZVEC_DOCKER_CONFIG_HOME = $previousLegacyConfigHome
    $env:ZVEC_REPO_ROOT = $previousRepoRoot
    $env:DASHSCOPE_API_KEY = $previousApiKey
    $env:DASHSCOPE_API_URL = $previousApiUrl
    if (Test-Path -LiteralPath $testRoot) {
        $resolved = (Resolve-Path -LiteralPath $testRoot).Path
        $tempRoot = (Resolve-Path -LiteralPath $env:TEMP).Path
        if ($resolved.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
