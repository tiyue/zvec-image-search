[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$launcherPath = Join-Path $repoRoot "scripts\zvec.ps1"
$windowsApps = if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps"
}
else {
    $null
}

if (-not [string]::IsNullOrWhiteSpace($env:ZVEC_COMMAND_INSTALL_DIR)) {
    $installDirectory = [System.IO.Directory]::CreateDirectory(
        [System.IO.Path]::GetFullPath($env:ZVEC_COMMAND_INSTALL_DIR)
    ).FullName
}
elseif (
    $null -ne $windowsApps -and
    (Test-Path -LiteralPath $windowsApps -PathType Container)
) {
    $installDirectory = $windowsApps
}
else {
    $installDirectory = [System.IO.Directory]::CreateDirectory(
        (Join-Path $env:LOCALAPPDATA "zvec-image-search\bin")
    ).FullName
}

$escapedCmdPath = $launcherPath.Replace("%", "%%")
$cmdContent = @"
@echo off
rem Managed by zvec-image-search
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$escapedCmdPath" %*
exit /b %ERRORLEVEL%
"@
$encoding = New-Object System.Text.UTF8Encoding($false)
$commandPath = Join-Path $installDirectory "zvec.cmd"
if (Test-Path -LiteralPath $commandPath) {
    $existingContent = [System.IO.File]::ReadAllText($commandPath)
    if (-not $existingContent.Contains("Managed by zvec-image-search")) {
        throw "Refusing to replace an existing command: $commandPath"
    }
}
[System.IO.File]::WriteAllText(
    $commandPath,
    $cmdContent,
    $encoding
)

$pathEntries = @($env:Path -split ";")
$onCurrentPath = $false
foreach ($entry in $pathEntries) {
    if ($entry.TrimEnd("\") -ieq $installDirectory.TrimEnd("\")) {
        $onCurrentPath = $true
        break
    }
}

if (-not $onCurrentPath -and [string]::IsNullOrWhiteSpace($env:ZVEC_COMMAND_INSTALL_DIR)) {
    $currentUserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $updatedPath = if ([string]::IsNullOrWhiteSpace($currentUserPath)) {
        $installDirectory
    }
    else {
        "$currentUserPath;$installDirectory"
    }
    [Environment]::SetEnvironmentVariable("Path", $updatedPath, "User")
    Write-Host "Installed zvec to: $installDirectory"
    Write-Host "Open a new terminal, then run: zvec init <image-folder>"
}
else {
    Write-Host "Installed zvec to: $installDirectory"
    Write-Host "Run: zvec init <image-folder>"
}
