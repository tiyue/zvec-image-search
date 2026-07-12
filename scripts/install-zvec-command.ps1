[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$launcherPath = Join-Path $repoRoot "scripts\zvec.ps1"
$commandSourcePath = Join-Path $repoRoot "bin\zvec-command.cs"
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

$legacyCommandPath = Join-Path $installDirectory "zvec.cmd"
if (Test-Path -LiteralPath $legacyCommandPath) {
    $existingContent = [System.IO.File]::ReadAllText($legacyCommandPath)
    if (-not $existingContent.Contains("Managed by zvec-image-search")) {
        throw "Refusing to replace an existing command: $legacyCommandPath"
    }
}

$commandPath = Join-Path $installDirectory "zvec.exe"
if (Test-Path -LiteralPath $commandPath) {
    $productName = [System.Diagnostics.FileVersionInfo]::GetVersionInfo(
        $commandPath
    ).ProductName
    if ($productName -ne "zvec-image-search command launcher") {
        throw "Refusing to replace an existing command: $commandPath"
    }
}

$source = [System.IO.File]::ReadAllText($commandSourcePath)
$escapedLauncherPath = $launcherPath.Replace("\", "\\").Replace('"', '\"')
$source = $source.Replace("__ZVEC_LAUNCHER_PATH__", $escapedLauncherPath)
$temporaryCommand = Join-Path $installDirectory (
    "zvec-" + [guid]::NewGuid().ToString("N") + ".exe"
)
try {
    Add-Type -TypeDefinition $source -OutputAssembly $temporaryCommand `
        -OutputType ConsoleApplication
    Move-Item -LiteralPath $temporaryCommand -Destination $commandPath -Force
}
finally {
    if (Test-Path -LiteralPath $temporaryCommand) {
        Remove-Item -LiteralPath $temporaryCommand -Force
    }
}

if (Test-Path -LiteralPath $legacyCommandPath) {
    Remove-Item -LiteralPath $legacyCommandPath -Force
}

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
