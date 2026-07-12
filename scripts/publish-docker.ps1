[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Version,

    [string]$Repository = "gelang999/zvec-image-search",

    [string[]]$Platforms = @("linux/amd64", "linux/arm64"),

    [switch]$Clean,

    [switch]$SkipLatest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
if ($Version -notmatch '^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$') {
    throw "Version must use semantic versioning, for example 0.4.0."
}
$majorVersion = $Matches[1]
$minorNumber = $Matches[2]
if ($Repository -notmatch '^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$') {
    throw "Repository must look like 'username/image-name'."
}
if ($Platforms.Count -eq 0) {
    throw "At least one platform is required."
}

$minorVersion = "$majorVersion.$minorNumber"
$tags = @(
    "$Repository`:$Version",
    "$Repository`:$minorVersion"
)
if (-not $SkipLatest) {
    $tags += "$Repository`:latest"
}

function Invoke-DockerCommand {
    param([Parameter(Mandatory = $true)][object[]]$Arguments)

    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed with exit code $LASTEXITCODE."
    }
}

function Test-LocalPlatform {
    param(
        [Parameter(Mandatory = $true)][string]$Image,
        [Parameter(Mandatory = $true)][string]$Platform
    )

    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & docker image inspect --platform $Platform $Image *> $null
        return $LASTEXITCODE -eq 0
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
}

$buildArguments = @(
    "buildx",
    "build",
    "--platform", ($Platforms -join ","),
    "--provenance=false"
)
if ($Clean) {
    $buildArguments += "--no-cache"
}
foreach ($tag in $tags) {
    $buildArguments += @("--tag", $tag)
}
$buildArguments += $repoRoot

Write-Host "Building $($Platforms -join ', ') for $Repository ..."
Invoke-DockerCommand -Arguments $buildArguments

foreach ($platform in $Platforms) {
    if (-not (Test-LocalPlatform -Image $tags[0] -Platform $platform)) {
        throw "The local image is missing platform '$platform'."
    }
}

foreach ($tag in $tags) {
    Write-Host "Pushing $tag ..."
    Invoke-DockerCommand -Arguments @("push", $tag)
}

Write-Host "Published: https://hub.docker.com/r/$Repository"
Write-Host "Tags: $($tags -join ', ')"
