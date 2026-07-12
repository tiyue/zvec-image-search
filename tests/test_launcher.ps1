Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $repoRoot "scripts\zvec.ps1"
$installer = Join-Path $repoRoot "scripts\install-zvec-command.cmd"
$testRoot = Join-Path $env:TEMP (
    "zvec-launcher-test-" + [guid]::NewGuid().ToString("N")
)
$fakeBin = Join-Path $testRoot "bin"
$configRoot = Join-Path $testRoot "config"
$installRoot = Join-Path $testRoot "installed"
$imageFolderName = ([char]0x56FE).ToString() + [char]0x5E93 + " space"
$queryFolderName = ([char]0x67E5).ToString() + [char]0x8BE2 + " space"
$queryFileName = ([char]0x6837).ToString() + [char]0x4F8B + ".jpg"
$queryText = ([char]0x6D77).ToString() + [char]0x8FB9 + [char]0x65E5 + [char]0x843D
$imageRoot = Join-Path $testRoot $imageFolderName
$queryRoot = Join-Path $testRoot $queryFolderName
$queryFile = Join-Path $queryRoot $queryFileName
$previousPath = $env:PATH
$previousConfigHome = $env:ZVEC_DOCKER_CONFIG_HOME
$previousApiKey = $env:DASHSCOPE_API_KEY
$previousInstallDirectory = $env:ZVEC_COMMAND_INSTALL_DIR

function Invoke-Launcher {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass `
        -File $launcher @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Launcher failed: $($output -join [Environment]::NewLine)"
    }
    return $output
}

try {
    New-Item -ItemType Directory -Path `
        $fakeBin,$configRoot,$installRoot,$imageRoot,$queryRoot -Force | Out-Null
    [System.IO.File]::WriteAllBytes($queryFile, [byte[]]@(0))

    $source = @'
using System;
public static class Program {
    public static int Main(string[] args) {
        if (args.Length > 0 && args[0] == "version") {
            Console.WriteLine("99.0.0");
            return 0;
        }
        if (args.Length > 1 && args[0] == "image" && args[1] == "inspect") {
            return 0;
        }
        if (args.Length > 0 && args[0] == "run" &&
            Array.IndexOf(args, "--entrypoint") >= 0 &&
            Array.IndexOf(args, "id") >= 0) {
            Console.WriteLine("10001");
            return 0;
        }
        if (Array.IndexOf(args, "force-fail") >= 0) {
            Console.Error.WriteLine("forced failure");
            return 23;
        }
        Console.WriteLine("DOCKER_ARGS: " + string.Join("|", args));
        return 0;
    }
}
'@
    Add-Type -TypeDefinition $source `
        -OutputAssembly (Join-Path $fakeBin "docker.exe") `
        -OutputType ConsoleApplication

    $env:PATH = "$fakeBin;$previousPath"
    $env:ZVEC_DOCKER_CONFIG_HOME = $configRoot
    $env:DASHSCOPE_API_KEY = "test-key-not-real"
    $env:ZVEC_COMMAND_INSTALL_DIR = $installRoot

    $null = Invoke-Launcher init $imageRoot --no-build
    $doctorOutput = Invoke-Launcher doctor
    $buildOutput = Invoke-Launcher build --clean
    $searchOutput = Invoke-Launcher search $queryText --top-k 3
    $imageOutput = Invoke-Launcher search-image $queryFile --top-k 2

    if (($doctorOutput -join "`n") -notmatch "Container mounts have the expected permissions") {
        throw "Doctor did not verify container mount permissions."
    }
    if (($doctorOutput -join "`n") -notmatch "Container runtime UID: 10001") {
        throw "Doctor did not verify the runtime UID."
    }
    if (($buildOutput -join "`n") -notmatch "build[|].*--no-cache") {
        throw "Clean build arguments were not translated."
    }
    if (($buildOutput -join "`n") -notmatch "Verified .*UID 10001") {
        throw "Build did not verify the image contract."
    }
    if (($searchOutput -join "`n") -notmatch "search[|]--text[|]$queryText") {
        throw "Text search arguments were not translated."
    }
    if (($imageOutput -join "`n") -notmatch "--image[|]/data/query/") {
        throw "Query image arguments were not translated."
    }

    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $failureOutput = & powershell.exe -NoProfile -ExecutionPolicy Bypass `
            -File $launcher raw force-fail 2>&1
        $failureExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($failureExitCode -ne 23) {
        throw "Launcher did not preserve Docker exit code 23: $failureOutput"
    }

    & cmd.exe /d /c $installer *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Command installer failed."
    }
    $installedCommand = Join-Path $installRoot "zvec.cmd"
    $helpOutput = & cmd.exe /d /c $installedCommand help 2>&1
    if ($LASTEXITCODE -ne 0 -or ($helpOutput -join "`n") -notmatch "Zvec Docker launcher") {
        throw "Installed zvec command did not run."
    }

    Write-Host "Launcher tests passed."
}
finally {
    $env:PATH = $previousPath
    $env:ZVEC_DOCKER_CONFIG_HOME = $previousConfigHome
    $env:DASHSCOPE_API_KEY = $previousApiKey
    $env:ZVEC_COMMAND_INSTALL_DIR = $previousInstallDirectory
    if (Test-Path -LiteralPath $testRoot) {
        $resolved = (Resolve-Path -LiteralPath $testRoot).Path
        $tempRoot = (Resolve-Path -LiteralPath $env:TEMP).Path
        if ($resolved.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
}
