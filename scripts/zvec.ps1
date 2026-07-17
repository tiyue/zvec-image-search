[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Position = 0)]
    [string]$Command = "help",

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Arguments = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($env:ZVEC_UTF8_OUTPUT -eq "1") {
    $utf8Output = New-Object System.Text.UTF8Encoding($false)
    [Console]::OutputEncoding = $utf8Output
    $OutputEncoding = $utf8Output
}

$scriptRoot = Split-Path -Parent $PSCommandPath
$repoRoot = Split-Path -Parent $scriptRoot
$packagedBackendRoot = Join-Path $repoRoot "backend"
$sourceRoot = if (
    Test-Path -LiteralPath (Join-Path $packagedBackendRoot "pyproject.toml") `
        -PathType Leaf
) {
    [System.IO.Path]::GetFullPath($packagedBackendRoot)
}
else {
    [System.IO.Path]::GetFullPath($repoRoot)
}
$runtimeResultPrefix = "@@ZVEC_RUNTIME_RESULT@@"
$runtimeCommand = $Command -in @("runtime-bootstrap", "runtime-doctor")
$script:RuntimeWasCreated = $false
$script:RuntimeWasUpdated = $false

function Test-PythonCommand {
    param([Parameter(Mandatory = $true)]$Python)

    try {
        & $Python.File @($Python.Prefix) -c (
            "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
        ) *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Get-PythonArchitectureState {
    param([Parameter(Mandatory = $true)]$Python)

    try {
        $probe = @(
            & $Python.File @($Python.Prefix) -c `
                "import platform; print(platform.system(),platform.machine(),sep=chr(124))" `
                2>$null
        )
        if ($LASTEXITCODE -ne 0) {
            return "probe_failed"
        }
        $parts = (($probe -join "").Trim() -split "\|", 2)
        if ($parts.Count -ne 2) {
            return "probe_failed"
        }
        if (
            $parts[0] -ieq "Windows" -and
            $parts[1] -in @("ARM64", "AArch64")
        ) {
            return "windows_arm64"
        }
        return "supported"
    }
    catch {
        return "probe_failed"
    }
}

function Throw-WindowsArm64PythonUnsupported {
    Throw-RuntimeError -Code "windows_arm64_python_unsupported" `
        -Message (
            "zvec 0.5.1 暂无 Windows ARM64 Python wheel，原生 ARM64 " +
            "解释器无法运行此后端。"
        ) `
        -RecommendedAction (
            "请安装 x64 CPython 3.10 或更高版本，选择其 python.exe 后重试；" +
            "Windows ARM64 会通过 x64 仿真运行后端。"
        )
}

function Test-PythonPip {
    param([Parameter(Mandatory = $true)]$Python)

    try {
        & $Python.File @($Python.Prefix) -m pip --version *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Resolve-RecoveryPythonCommand {
    param(
        [Parameter(Mandatory = $true)][string]$ExcludedPython,
        $PreferredPython
    )

    $excluded = [System.IO.Path]::GetFullPath($ExcludedPython)
    $candidates = New-Object System.Collections.Generic.List[object]
    if ($null -ne $PreferredPython) {
        $candidates.Add($PreferredPython)
    }
    foreach ($name in @("python", "python3")) {
        foreach ($candidate in @(
            Get-Command $name -All -ErrorAction SilentlyContinue
        )) {
            $candidates.Add([pscustomobject]@{
                File = $candidate.Source
                Prefix = @()
            })
        }
    }
    foreach ($py in @(Get-Command "py" -All -ErrorAction SilentlyContinue)) {
        # Prefer the classic launcher's x64 selector on Windows ARM64, then fall back
        # to its default Python 3 selection for launcher versions without that selector.
        $candidates.Add([pscustomobject]@{
            File = $py.Source
            Prefix = @("-3-64")
        })
        $candidates.Add([pscustomobject]@{ File = $py.Source; Prefix = @("-3") })
    }

    $arm64CandidateFound = $false
    foreach ($candidate in $candidates) {
        $candidatePath = $candidate.File
        if ([System.IO.Path]::IsPathRooted($candidatePath)) {
            $candidatePath = [System.IO.Path]::GetFullPath($candidatePath)
            if ($candidatePath -ieq $excluded) {
                continue
            }
        }
        if (-not (Test-PythonCommand -Python $candidate)) {
            continue
        }
        $architectureState = Get-PythonArchitectureState -Python $candidate
        if ($architectureState -eq "windows_arm64") {
            $arm64CandidateFound = $true
            continue
        }
        if (
            $architectureState -eq "supported" -and
            (Test-PythonPip -Python $candidate)
        ) {
            return $candidate
        }
    }
    if ($arm64CandidateFound) {
        Throw-WindowsArm64PythonUnsupported
    }
    return $null
}

function Throw-RuntimeError {
    param(
        [Parameter(Mandatory = $true)][string]$Code,
        [Parameter(Mandatory = $true)][string]$Message,
        [Parameter(Mandatory = $true)][string]$RecommendedAction
    )

    $exception = New-Object System.InvalidOperationException($Message)
    $exception.Data["ZvecRuntimeCode"] = $Code
    $exception.Data["ZvecRecommendedAction"] = $RecommendedAction
    throw $exception
}

function Write-RuntimeResult {
    param(
        [Parameter(Mandatory = $true)][bool]$Success,
        [Parameter(Mandatory = $true)][string]$Status,
        [Parameter(Mandatory = $true)][string]$Code,
        [Parameter(Mandatory = $true)][string]$Message,
        [string]$RecommendedAction,
        [string]$PythonExecutable,
        [string]$PythonVersion,
        [string]$PythonArchitecture,
        [string]$RuntimeDirectory,
        [object[]]$Dependencies = @(),
        [bool]$Changed = $false
    )

    $payload = [ordered]@{
        schema_version = 1
        success = $Success
        status = $Status
        changed = $Changed
        code = $Code
        message = $Message
        recommended_action = $RecommendedAction
        python_executable = $PythonExecutable
        python_version = $PythonVersion
        python_architecture = $PythonArchitecture
        runtime_directory = $RuntimeDirectory
        dependencies = @($Dependencies)
    }
    $json = $payload | ConvertTo-Json -Compress -Depth 5
    [Console]::Out.WriteLine($runtimeResultPrefix + $json)
}

function Get-ConfigHome {
    if (-not [string]::IsNullOrWhiteSpace($env:ZVEC_CONFIG_HOME)) {
        return [System.IO.Path]::GetFullPath($env:ZVEC_CONFIG_HOME)
    }
    # Retain the old variable while existing installations migrate to schema v3.
    if (-not [string]::IsNullOrWhiteSpace($env:ZVEC_DOCKER_CONFIG_HOME)) {
        return [System.IO.Path]::GetFullPath($env:ZVEC_DOCKER_CONFIG_HOME)
    }
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        return Join-Path $env:LOCALAPPDATA "zvec-image-search"
    }
    return Join-Path $HOME ".zvec-image-search"
}

function Resolve-PythonCommand {
    $arm64CandidateFound = $false
    if (-not [string]::IsNullOrWhiteSpace($env:ZVEC_PYTHON)) {
        $configured = Get-Command $env:ZVEC_PYTHON -ErrorAction SilentlyContinue
        if ($null -eq $configured) {
            Throw-RuntimeError -Code "configured_python_not_found" `
                -Message "找不到已配置的 Python：$($env:ZVEC_PYTHON)" `
                -RecommendedAction (
                    "请选择现有的 x64 Python 3.10 或更高版本；也可以清除无效的 " +
                    "Python 路径后重试。"
                )
        }
        $configuredCommand = [pscustomobject]@{
            File = $configured.Source
            Prefix = @()
        }
        if (Test-PythonCommand -Python $configuredCommand) {
            $architectureState = Get-PythonArchitectureState -Python $configuredCommand
            if ($architectureState -eq "windows_arm64") {
                # An explicit interpreter choice must never silently fall back.
                Throw-WindowsArm64PythonUnsupported
            }
            if ($architectureState -eq "probe_failed") {
                Throw-RuntimeError -Code "python_probe_failed" `
                    -Message "无法检查已配置 Python 的系统和 CPU 架构。" `
                    -RecommendedAction "请选择可用的 x64 Python 3.10 或更高版本。"
            }
            return $configuredCommand
        }
        if ($Command -ine "runtime-bootstrap") {
            Throw-RuntimeError -Code "configured_python_invalid" `
                -Message "已配置的 Python 无法运行或版本低于 3.10：$($configured.Source)" `
                -RecommendedAction "请选择可用的 x64 Python 3.10 或更高版本，然后重试。"
        }
        # Automatic repair may recover from a stale saved interpreter by using the
        # healthy app venv or another valid system Python below.
    }
    # Prefer the app-owned interpreter once it exists. It is the environment that will
    # actually run the launcher and it remains usable if a system Python is later removed.
    $runtimePython = (Get-NativeRuntimePaths).Python
    if (Test-Path -LiteralPath $runtimePython -PathType Leaf) {
        $runtimeCommandValue = [pscustomobject]@{ File = $runtimePython; Prefix = @() }
        if (Test-PythonCommand -Python $runtimeCommandValue) {
            $architectureState = Get-PythonArchitectureState -Python $runtimeCommandValue
            if ($architectureState -eq "supported") {
                return $runtimeCommandValue
            }
            if ($architectureState -eq "windows_arm64") {
                $arm64CandidateFound = $true
            }
        }
    }
    foreach ($name in @("python", "python3")) {
        foreach ($candidate in @(
            Get-Command $name -All -ErrorAction SilentlyContinue
        )) {
            $candidateCommand = [pscustomobject]@{
                File = $candidate.Source
                Prefix = @()
            }
            if (Test-PythonCommand -Python $candidateCommand) {
                $architectureState = Get-PythonArchitectureState -Python $candidateCommand
                if ($architectureState -eq "supported") {
                    return $candidateCommand
                }
                if ($architectureState -eq "windows_arm64") {
                    $arm64CandidateFound = $true
                }
            }
        }
    }
    foreach ($py in @(Get-Command "py" -All -ErrorAction SilentlyContinue)) {
        foreach ($selector in @("-3-64", "-3")) {
            $pyCommand = [pscustomobject]@{
                File = $py.Source
                Prefix = @($selector)
            }
            if (Test-PythonCommand -Python $pyCommand) {
                $architectureState = Get-PythonArchitectureState -Python $pyCommand
                if ($architectureState -eq "supported") {
                    return $pyCommand
                }
                if ($architectureState -eq "windows_arm64") {
                    $arm64CandidateFound = $true
                }
            }
        }
    }
    if ($arm64CandidateFound) {
        Throw-WindowsArm64PythonUnsupported
    }
    Throw-RuntimeError -Code "python_not_found" `
        -Message "找不到 Python 3.10 或更高版本。" `
        -RecommendedAction (
            "请从 python.org 安装 x64 CPython 3.10 或更高版本，勾选 Add Python " +
            "to PATH；如 Windows Store 的 App Execution Alias 拦截 python.exe，" +
            "请关闭该别名，然后点击【修复运行环境】。"
        )
}

function Invoke-Python {
    param(
        [Parameter(Mandatory = $true)]$Python,
        [Parameter(Mandatory = $true)][string[]]$PythonArguments
    )
    & $Python.File @($Python.Prefix) @PythonArguments | Out-Host
    $exitCode = $LASTEXITCODE
    return $exitCode
}

function Invoke-PythonCaptured {
    param(
        [Parameter(Mandatory = $true)]$Python,
        [Parameter(Mandatory = $true)][string[]]$PythonArguments
    )

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # Windows PowerShell promotes redirected native stderr to ErrorRecord. Keep it
        # as diagnostic text instead of letting the script-wide Stop preference abort
        # before the structured recovery code can be selected.
        $ErrorActionPreference = "Continue"
        $output = @(
            & $Python.File @($Python.Prefix) @PythonArguments 2>&1
        )
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $outputText = @($output | ForEach-Object { [string]$_ })
    foreach ($line in $outputText) {
        # Redirected native stderr arrives as ErrorRecord objects in Windows
        # PowerShell. Writing plain text avoids rethrowing it under ErrorAction=Stop.
        [Console]::Out.WriteLine($line)
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $outputText
    }
}

function Throw-RuntimeCommandFailure {
    param(
        [Parameter(Mandatory = $true)]
        [AllowNull()]
        [AllowEmptyCollection()]
        [object[]]$Output,
        [Parameter(Mandatory = $true)][string]$DefaultCode,
        [Parameter(Mandatory = $true)][string]$DefaultMessage,
        [Parameter(Mandatory = $true)][string]$DefaultRecommendedAction
    )

    $details = (@($Output) | ForEach-Object { [string]$_ }) -join "`n"
    if ($details -match (
        '(?i)(no space left on device|there is not enough space|' +
        'not enough space (?:on|available)|disk (?:is )?full|' +
        'winerror\s*112|errno\s*28)'
    )) {
        Throw-RuntimeError -Code "runtime_disk_space_insufficient" `
            -Message "磁盘空间不足，运行环境未能安装完成。" `
            -RecommendedAction (
                "请释放 ZVEC_CONFIG_HOME 所在磁盘空间（建议至少 1 GB），再点击" +
                "【修复运行环境】。未完成的临时文件会在重试时自动清理。"
            )
    }
    if ($details -match (
        '(?i)(temporary failure in name resolution|nameresolutionerror|' +
        'getaddrinfo failed|failed to establish a new connection|' +
        'network is unreachable|connection (?:aborted|refused|reset)|' +
        'proxyerror|readtimeout|connecttimeout|max retries exceeded|' +
        'could not resolve host)'
    )) {
        Throw-RuntimeError -Code "runtime_network_unavailable" `
            -Message "无法连接 Python 软件包源，运行环境未能安装完成。" `
            -RecommendedAction (
                "请连接网络或检查 Python 软件包源和代理设置，再点击" +
                "【修复运行环境】；已有图库和索引不会被修改。"
            )
    }
    Throw-RuntimeError -Code $DefaultCode `
        -Message $DefaultMessage `
        -RecommendedAction $DefaultRecommendedAction
}

function Get-Sha256FileHash {
    param([Parameter(Mandatory = $true)][string]$Path)

    # Get-FileHash is exported by a PowerShell module and may not be discoverable
    # when a managed host intentionally supplies a minimal process environment.
    # Hash through .NET so bootstrap depends only on the runtime already executing it.
    $stream = $null
    $sha256 = $null
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        return ([System.BitConverter]::ToString(
            $sha256.ComputeHash($stream)
        )).Replace("-", "")
    }
    finally {
        if ($null -ne $sha256) {
            $sha256.Dispose()
        }
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

function Get-SourceFingerprint {
    $files = @(
        (Join-Path $sourceRoot "pyproject.toml"),
        (Join-Path $sourceRoot "requirements.txt"),
        (Join-Path $sourceRoot "requirements-lock.txt"),
        (Join-Path $sourceRoot "model-catalog.default.json"),
        (Join-Path $sourceRoot "README.md"),
        (Join-Path $sourceRoot "image_service.py"),
        (Join-Path $sourceRoot "zvec_launcher.py"),
        (Join-Path $sourceRoot "zvec_logging.py")
    )
    $files += @(
        Get-ChildItem -LiteralPath (Join-Path $sourceRoot "image_vector_service") `
            -Filter "*.py" -File -Recurse |
            Sort-Object FullName |
            ForEach-Object FullName
    )
    $builder = New-Object System.Text.StringBuilder
    foreach ($file in $files) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
            Throw-RuntimeError -Code "runtime_source_missing" `
                -Message "原生运行环境文件缺失：$file" `
                -RecommendedAction (
                    "请修复或重新安装包含完整 backend 目录的桌面应用，然后重试。"
                )
        }
        $hash = Get-Sha256FileHash -Path $file
        [void]$builder.Append($file).Append(":").Append($hash).Append("`n")
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($builder.ToString())
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString(
            $sha256.ComputeHash($bytes)
        )).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
    }
}

function Get-NativeRuntimePaths {
    param([switch]$DoNotCreate)

    try {
        $configHomePath = [System.IO.Path]::GetFullPath((Get-ConfigHome))
        $configHome = if ($DoNotCreate) {
            $configHomePath
        }
        else {
            [System.IO.Directory]::CreateDirectory($configHomePath).FullName
        }
        $runtimeRootPath = Join-Path $configHome "runtime"
        $runtimeRoot = if ($DoNotCreate) {
            [System.IO.Path]::GetFullPath($runtimeRootPath)
        }
        else {
            [System.IO.Directory]::CreateDirectory($runtimeRootPath).FullName
        }
    }
    catch {
        if ($DoNotCreate) {
            throw
        }
        Throw-RuntimeError -Code "config_directory_unavailable" `
            -Message "无法创建或访问用户运行环境目录：$($_.Exception.Message)" `
            -RecommendedAction (
                "请确认 ZVEC_CONFIG_HOME 指向可写目录，并检查磁盘空间与目录权限后重试。"
            )
    }
    $venvRoot = Join-Path $runtimeRoot "venv"
    $venvPython = if ($env:OS -eq "Windows_NT") {
        Join-Path $venvRoot "Scripts\python.exe"
    }
    else {
        Join-Path $venvRoot "bin/python"
    }
    return [pscustomobject]@{
        ConfigHome = $configHome
        RuntimeRoot = $runtimeRoot
        VenvRoot = $venvRoot
        Python = $venvPython
        Stamp = Join-Path $runtimeRoot "source.sha256"
        Lock = Join-Path $runtimeRoot "bootstrap.lock"
    }
}

function Get-RuntimeDirectoryForFailure {
    try {
        return (Get-NativeRuntimePaths -DoNotCreate).RuntimeRoot
    }
    catch {
        return ""
    }
}

function Enter-NativeRuntimeLock {
    param([Parameter(Mandatory = $true)]$Paths)

    $timeoutSeconds = 120
    if (-not [string]::IsNullOrWhiteSpace($env:ZVEC_RUNTIME_LOCK_TIMEOUT_SECONDS)) {
        $parsedTimeout = 0
        if (
            [int]::TryParse(
                $env:ZVEC_RUNTIME_LOCK_TIMEOUT_SECONDS,
                [ref]$parsedTimeout
            ) -and
            $parsedTimeout -ge 1 -and
            $parsedTimeout -le 600
        ) {
            $timeoutSeconds = $parsedTimeout
        }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds($timeoutSeconds)
    while ($true) {
        $stream = $null
        try {
            $stream = [System.IO.File]::Open(
                $Paths.Lock,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
            $owner = [System.Text.Encoding]::UTF8.GetBytes(
                "pid=$PID started=$([DateTime]::UtcNow.ToString('O'))`n"
            )
            $stream.SetLength(0)
            $stream.Write($owner, 0, $owner.Length)
            $stream.Flush()
            return $stream
        }
        catch [System.IO.IOException] {
            if ($null -ne $stream) {
                $stream.Dispose()
            }
            if ([DateTime]::UtcNow -ge $deadline) {
                Throw-RuntimeError -Code "runtime_lock_timeout" `
                    -Message "另一个窗口或进程正在准备 Python 运行环境，等待已超时。" `
                    -RecommendedAction (
                        "请等待另一个 Zvec 窗口完成；若确认没有任务在运行，请关闭其他 " +
                        "Zvec 实例后重试。"
                    )
            }
            [System.Threading.Thread]::Sleep(200)
        }
        catch {
            if ($null -ne $stream) {
                $stream.Dispose()
            }
            throw
        }
    }
}

function Reset-NativeRuntimeVenv {
    param([Parameter(Mandatory = $true)]$Paths)

    $runtimeRoot = [System.IO.Path]::GetFullPath($Paths.RuntimeRoot).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $venvRoot = [System.IO.Path]::GetFullPath($Paths.VenvRoot)
    $expectedPrefix = $runtimeRoot + [System.IO.Path]::DirectorySeparatorChar
    if (-not $venvRoot.StartsWith(
        $expectedPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        Throw-RuntimeError -Code "unsafe_runtime_path" `
            -Message "拒绝重建配置目录之外的 Python 环境：$venvRoot" `
            -RecommendedAction "请检查 ZVEC_CONFIG_HOME 配置后重试。"
    }
    try {
        if (Test-Path -LiteralPath $venvRoot) {
            Remove-Item -LiteralPath $venvRoot -Recurse -Force
        }
        if (Test-Path -LiteralPath $Paths.Stamp -PathType Leaf) {
            Remove-Item -LiteralPath $Paths.Stamp -Force
        }
    }
    catch {
        Throw-RuntimeError -Code "venv_reset_failed" `
            -Message "损坏的 Python 隔离环境无法安全重建：$($_.Exception.Message)" `
            -RecommendedAction "请关闭占用该目录的程序，确认配置目录可写，然后重试。"
    }
}

function Remove-StaleNativeRuntimeWheels {
    param([Parameter(Mandatory = $true)]$Paths)

    $runtimeRoot = [System.IO.Path]::GetFullPath($Paths.RuntimeRoot).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    foreach ($directory in @(
        Get-ChildItem -LiteralPath $runtimeRoot -Directory -Filter "wheel-*" `
            -ErrorAction Stop
    )) {
        # The launcher creates only wheel-<32 lowercase hex> directories. Ignore every
        # other name and every reparse point so cleanup cannot cross the owned runtime root.
        if (
            $directory.Name -notmatch '^wheel-[0-9a-f]{32}$' -or
            ($directory.Attributes -band [System.IO.FileAttributes]::ReparsePoint)
        ) {
            continue
        }
        $fullPath = [System.IO.Path]::GetFullPath($directory.FullName)
        $expectedPrefix = $runtimeRoot + [System.IO.Path]::DirectorySeparatorChar
        if (-not $fullPath.StartsWith(
            $expectedPrefix,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            continue
        }
        try {
            Remove-Item -LiteralPath $fullPath -Recurse -Force
        }
        catch {
            Throw-RuntimeError -Code "runtime_cleanup_failed" `
                -Message "无法清理上次取消后遗留的临时 wheel 目录：$fullPath" `
                -RecommendedAction "请关闭占用该目录的程序，然后重新点击【修复运行环境】。"
        }
    }
}

function Get-NativeRuntimeProbe {
    param([Parameter(Mandatory = $true)][string]$RuntimePython)

    if (-not (Test-Path -LiteralPath $RuntimePython -PathType Leaf)) {
        Throw-RuntimeError -Code "runtime_not_installed" `
            -Message "尚未安装隔离的 Python 运行环境。" `
            -RecommendedAction "请点击【修复运行环境】创建隔离环境。"
    }

    $probeCode = @"
import importlib.metadata as metadata
import json
import platform
import sys
import zvec
import PIL
import numpy
payload = {
    "python_executable": sys.executable,
    "python_version": platform.python_version(),
    "python_architecture": platform.machine(),
    "dependencies": [
        {"name": "zvec", "version": metadata.version("zvec"), "imported": True},
        {"name": "Pillow", "version": metadata.version("Pillow"), "imported": True},
        {"name": "numpy", "version": metadata.version("numpy"), "imported": True},
    ],
}
print("@@ZVEC_PYTHON_PROBE@@" + json.dumps(payload, ensure_ascii=False))
"@
    # Base64 keeps quotes and non-ASCII source intact across Windows PowerShell's native
    # command-line quoting rules.
    $encodedProbe = [Convert]::ToBase64String(
        [System.Text.Encoding]::UTF8.GetBytes($probeCode)
    )
    $probeOutput = @(
        & $RuntimePython -c `
            "import base64,sys;exec(base64.b64decode(sys.argv[1]))" `
            $encodedProbe 2>&1
    )
    if ($LASTEXITCODE -ne 0) {
        $details = ($probeOutput -join [Environment]::NewLine).Trim()
        Throw-RuntimeError -Code "dependency_import_failed" `
            -Message ("隔离环境中的 Python 依赖无法导入。" + $details) `
            -RecommendedAction (
                "请点击【修复运行环境】。若仍失败，请检查 Python 软件包源的网络连接，" +
                "并打开安装日志查看详情。"
            )
    }
    $probeLine = $probeOutput |
        Where-Object { $_ -is [string] -and $_.StartsWith("@@ZVEC_PYTHON_PROBE@@") } |
        Select-Object -Last 1
    if ([string]::IsNullOrWhiteSpace($probeLine)) {
        Throw-RuntimeError -Code "dependency_probe_invalid" `
            -Message "隔离环境依赖检查返回了无效结果。" `
            -RecommendedAction "请点击【修复运行环境】，然后重新检查。"
    }
    try {
        return $probeLine.Substring("@@ZVEC_PYTHON_PROBE@@".Length) |
            ConvertFrom-Json
    }
    catch {
        Throw-RuntimeError -Code "dependency_probe_invalid" `
            -Message "隔离环境依赖检查返回的 JSON 格式错误。" `
            -RecommendedAction "请点击【修复运行环境】，然后重新检查。"
    }
}

function Initialize-NativeRuntime {
    param([Parameter(Mandatory = $true)]$BootstrapPython)

    $paths = Get-NativeRuntimePaths
    $runtimeLock = Enter-NativeRuntimeLock -Paths $paths
    try {
    Remove-StaleNativeRuntimeWheels -Paths $paths
    $runtimeRoot = $paths.RuntimeRoot
    $venvRoot = $paths.VenvRoot
    $venvPython = $paths.Python
    $stampPath = $paths.Stamp
    $requirementsLock = Join-Path $sourceRoot "requirements-lock.txt"
    $fingerprint = Get-SourceFingerprint
    $installedFingerprint = if (Test-Path -LiteralPath $stampPath -PathType Leaf) {
        [System.IO.File]::ReadAllText($stampPath).Trim()
    }
    else {
        ""
    }

    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $venvCommand = [pscustomobject]@{ File = $venvPython; Prefix = @() }
        if (-not (Test-PythonCommand -Python $venvCommand)) {
            Reset-NativeRuntimeVenv -Paths $paths
            $installedFingerprint = ""
            $script:RuntimeWasUpdated = $true
        }
        elseif ((Get-PythonArchitectureState -Python $venvCommand) -eq "windows_arm64") {
            $recoveryPython = Resolve-RecoveryPythonCommand `
                -ExcludedPython $venvPython -PreferredPython $BootstrapPython
            if ($null -eq $recoveryPython) {
                Throw-WindowsArm64PythonUnsupported
            }
            Reset-NativeRuntimeVenv -Paths $paths
            $BootstrapPython = $recoveryPython
            $installedFingerprint = ""
            $script:RuntimeWasUpdated = $true
        }
        elseif (-not (Test-PythonPip -Python $venvCommand)) {
            $recoveryPython = Resolve-RecoveryPythonCommand `
                -ExcludedPython $venvPython -PreferredPython $BootstrapPython
            if ($null -eq $recoveryPython) {
                Throw-RuntimeError -Code "venv_pip_unusable" `
                    -Message "隔离环境中的 pip 已损坏，且找不到可用于重建的系统 Python。" `
                    -RecommendedAction (
                        "请安装带 pip 的 x64 Python 3.10 或更高版本，然后点击" +
                        "【修复运行环境】。"
                    )
            }
            Reset-NativeRuntimeVenv -Paths $paths
            $BootstrapPython = $recoveryPython
            $installedFingerprint = ""
            $script:RuntimeWasUpdated = $true
        }
    }

    if (
        (Test-Path -LiteralPath $venvRoot) -and
        -not (Test-Path -LiteralPath $venvPython -PathType Leaf)
    ) {
        # A cancelled installer can leave a directory without a runnable interpreter.
        # Reusing it may preserve a partially installed site-packages tree, so rebuild it
        # under the runtime lock before the next attempt.
        Reset-NativeRuntimeVenv -Paths $paths
        $installedFingerprint = ""
        $script:RuntimeWasUpdated = $true
    }

    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        $venvCreation = Invoke-PythonCaptured -Python $BootstrapPython `
            -PythonArguments @("-m", "venv", $venvRoot)
        if ($venvCreation.ExitCode -ne 0) {
            # Never retain a half-created venv. A later retry starts from a known state.
            Reset-NativeRuntimeVenv -Paths $paths
            Throw-RuntimeCommandFailure -Output $venvCreation.Output `
                -DefaultCode "venv_create_failed" `
                -DefaultMessage "无法创建隔离的 Python 运行环境。" `
                -DefaultRecommendedAction (
                    "请确认所选 Python 包含 venv 模块，并且配置目录可写，然后重试。"
                )
        }
        $script:RuntimeWasCreated = $true
        $installedFingerprint = ""
    }

    if ($installedFingerprint -eq $fingerprint) {
        try {
            $null = Get-NativeRuntimeProbe -RuntimePython $venvPython
        }
        catch {
            # A matching source stamp is not sufficient if the venv was partially removed or
            # corrupted. Force the locked wheel installation to repair it in place.
            $installedFingerprint = ""
        }
    }

    if ($installedFingerprint -ne $fingerprint) {
        $wheelRoot = [System.IO.Directory]::CreateDirectory(
            (Join-Path $runtimeRoot ("wheel-" + [guid]::NewGuid().ToString("N")))
        ).FullName
        try {
            $wheelBuild = Invoke-PythonCaptured -Python $BootstrapPython `
                -PythonArguments @(
                "-m", "pip", "wheel", "--disable-pip-version-check", "--no-deps",
                "--wheel-dir", $wheelRoot, $sourceRoot
            )
            if ($wheelBuild.ExitCode -ne 0) {
                Throw-RuntimeCommandFailure -Output $wheelBuild.Output `
                    -DefaultCode "wheel_build_failed" `
                    -DefaultMessage "无法构建原生 zvec wheel。" `
                    -DefaultRecommendedAction (
                        "请确认 pip 可用且应用的 backend 文件完整，然后重试。"
                    )
            }
            $wheel = Get-ChildItem -LiteralPath $wheelRoot -Filter "*.whl" -File |
                Select-Object -First 1
            if ($null -eq $wheel) {
                Throw-RuntimeError -Code "wheel_build_failed" `
                    -Message "原生 zvec 构建没有生成 wheel。" `
                    -RecommendedAction "请修复或重新安装应用，然后重试。"
            }
            $installPython = [pscustomobject]@{ File = $venvPython; Prefix = @() }
            $dependencyInstall = Invoke-PythonCaptured -Python $installPython `
                -PythonArguments @(
                    "-m", "pip", "install", "--disable-pip-version-check",
                    "--upgrade", "--force-reinstall", "--only-binary=:all:",
                    "--constraint", $requirementsLock, $wheel.FullName
                )
            if ($dependencyInstall.ExitCode -ne 0) {
                Throw-RuntimeCommandFailure -Output $dependencyInstall.Output `
                    -DefaultCode "dependency_install_failed" `
                    -DefaultMessage "无法安装原生 zvec 运行依赖。" `
                    -DefaultRecommendedAction (
                        "请检查网络或软件包源代理，确认磁盘空间充足，然后再次点击" +
                        "【修复运行环境】。"
                    )
            }
            [System.IO.File]::WriteAllText($stampPath, $fingerprint + "`n")
            $script:RuntimeWasUpdated = $true
        }
        finally {
            $resolvedRuntime = [System.IO.Path]::GetFullPath($runtimeRoot)
            $resolvedWheel = [System.IO.Path]::GetFullPath($wheelRoot)
            if (
                $resolvedWheel.StartsWith(
                    $resolvedRuntime + [System.IO.Path]::DirectorySeparatorChar,
                    [System.StringComparison]::OrdinalIgnoreCase
                ) -and
                (Test-Path -LiteralPath $resolvedWheel)
            ) {
                Remove-Item -LiteralPath $resolvedWheel -Recurse -Force
            }
        }
    }
    return $venvPython
    }
    finally {
        $runtimeLock.Dispose()
    }
}

function Assert-SupportedPythonRuntime {
    param([Parameter(Mandatory = $true)]$Python)

    $architectureState = Get-PythonArchitectureState -Python $Python
    if ($architectureState -eq "probe_failed") {
        Throw-RuntimeError -Code "python_probe_failed" `
            -Message "无法检查 Python 解释器。" `
            -RecommendedAction "请选择可用的 x64 Python 3.10 或更高版本。"
    }
    if ($architectureState -eq "windows_arm64") {
        Throw-WindowsArm64PythonUnsupported
    }
}

function Write-SuccessfulRuntimeResult {
    param(
        [Parameter(Mandatory = $true)]$Probe,
        [Parameter(Mandatory = $true)]$Paths,
        [Parameter(Mandatory = $true)][bool]$Changed
    )

    $status = if ($Changed) { "repaired" } else { "ready" }
    $message = if ($Changed) {
        "隔离的 Python 运行环境已准备并验证完成。"
    }
    else {
        "隔离的 Python 运行环境已就绪。"
    }
    Write-RuntimeResult -Success $true -Status $status -Code $status `
        -Message $message -PythonExecutable $Probe.python_executable `
        -PythonVersion $Probe.python_version `
        -PythonArchitecture $Probe.python_architecture `
        -RuntimeDirectory $Paths.RuntimeRoot `
        -Dependencies @($Probe.dependencies) -Changed $Changed
}

function Invoke-RuntimeDoctor {
    $paths = Get-NativeRuntimePaths
    $runtimeLock = Enter-NativeRuntimeLock -Paths $paths
    try {
    if (-not (Test-Path -LiteralPath $paths.Stamp -PathType Leaf)) {
        Throw-RuntimeError -Code "runtime_not_installed" `
            -Message "隔离的 Python 运行环境尚未初始化。" `
            -RecommendedAction "请点击【修复运行环境】安装所需依赖。"
    }
    $expectedFingerprint = Get-SourceFingerprint
    $installedFingerprint = [System.IO.File]::ReadAllText($paths.Stamp).Trim()
    if ($installedFingerprint -ne $expectedFingerprint) {
        Throw-RuntimeError -Code "runtime_outdated" `
            -Message "隔离的 Python 运行环境与当前应用版本不匹配。" `
            -RecommendedAction "请点击【修复运行环境】安装当前版本的后端。"
    }
    $runtimePython = [pscustomobject]@{ File = $paths.Python; Prefix = @() }
    if (-not (Test-PythonCommand -Python $runtimePython)) {
        Throw-RuntimeError -Code "venv_unusable" `
            -Message "隔离的 Python 环境已损坏、无法执行或版本低于 3.10。" `
            -RecommendedAction (
                "请点击【修复运行环境】；程序会使用可用的 x64 Python 安全重建 venv。"
            )
    }
    if (-not (Test-PythonPip -Python $runtimePython)) {
        Throw-RuntimeError -Code "venv_pip_unusable" `
            -Message "隔离环境中的 pip 已损坏，无法安装或更新锁定依赖。" `
            -RecommendedAction (
                "请安装带 pip 的 x64 Python 3.10 或更高版本，然后点击" +
                "【修复运行环境】。"
            )
    }
    Assert-SupportedPythonRuntime -Python $runtimePython
    $probe = Get-NativeRuntimeProbe -RuntimePython $paths.Python
    Write-SuccessfulRuntimeResult -Probe $probe -Paths $paths -Changed $false
    }
    finally {
        $runtimeLock.Dispose()
    }
}

function Get-RuntimeFailureData {
    param([Parameter(Mandatory = $true)]$ErrorRecord)

    $code = [string]$ErrorRecord.Exception.Data["ZvecRuntimeCode"]
    if ([string]::IsNullOrWhiteSpace($code)) {
        $messages = New-Object System.Collections.Generic.List[string]
        $isDiskFull = $false
        $currentException = $ErrorRecord.Exception
        while ($null -ne $currentException) {
            $messages.Add([string]$currentException.Message)
            $nativeCode = $currentException.HResult -band 0xFFFF
            if ($nativeCode -in @(39, 112)) {
                $isDiskFull = $true
            }
            $currentException = $currentException.InnerException
        }
        $failureText = $messages -join "`n"
        if (
            $isDiskFull -or
            $failureText -match (
                '(?i)(no space left on device|there is not enough space|' +
                'not enough space (?:on|available)|disk (?:is )?full|' +
                'winerror\s*112|errno\s*28)'
            )
        ) {
            return [pscustomobject]@{
                Code = "runtime_disk_space_insufficient"
                Action = (
                    "请释放 ZVEC_CONFIG_HOME 所在磁盘空间（建议至少 1 GB），再点击" +
                    "【修复运行环境】。未完成的临时文件会在重试时自动清理。"
                )
            }
        }
        $code = "runtime_bootstrap_failed"
    }
    $action = [string]$ErrorRecord.Exception.Data["ZvecRecommendedAction"]
    if ([string]::IsNullOrWhiteSpace($action)) {
        $action = "请打开安装日志，修复运行环境后重试。"
    }
    return [pscustomobject]@{ Code = $code; Action = $action }
}

try {
    if ($Command -ieq "runtime-doctor") {
        Invoke-RuntimeDoctor
        exit 0
    }

    $bootstrapPython = Resolve-PythonCommand
    $versionExit = Invoke-Python -Python $bootstrapPython -PythonArguments @(
        "-c",
        "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
    )
    if ($versionExit -ne 0) {
        Throw-RuntimeError -Code "python_version_unsupported" `
            -Message "需要 Python 3.10 或更高版本。" `
            -RecommendedAction "请安装 x64 CPython 3.10 或更高版本，然后重试。"
    }
    Assert-SupportedPythonRuntime -Python $bootstrapPython

    if (
        $env:ZVEC_NATIVE_USE_SOURCE -eq "1" -and
        $Command -ine "runtime-bootstrap"
    ) {
        $previousPythonPath = $env:PYTHONPATH
        $env:PYTHONPATH = if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
            $sourceRoot
        }
        else {
            "$sourceRoot$([System.IO.Path]::PathSeparator)$previousPythonPath"
        }
        try {
            $pythonArguments = @("-m", "zvec_launcher", $Command) + $Arguments
            $exitCode = Invoke-Python -Python $bootstrapPython `
                -PythonArguments $pythonArguments
        }
        finally {
            $env:PYTHONPATH = $previousPythonPath
        }
        exit $exitCode
    }

    $runtimePython = Initialize-NativeRuntime -BootstrapPython $bootstrapPython
    if ($Command -ieq "runtime-bootstrap") {
        $paths = Get-NativeRuntimePaths
        $probe = Get-NativeRuntimeProbe -RuntimePython $runtimePython
        Write-SuccessfulRuntimeResult -Probe $probe -Paths $paths `
            -Changed ($script:RuntimeWasCreated -or $script:RuntimeWasUpdated)
        exit 0
    }
    & $runtimePython -m zvec_launcher $Command @Arguments
    exit $LASTEXITCODE
}
catch {
    if ($runtimeCommand) {
        $failure = Get-RuntimeFailureData -ErrorRecord $_
        Write-RuntimeResult -Success $false -Status "failed" `
            -Code $failure.Code -Message $_.Exception.Message `
            -RecommendedAction $failure.Action `
            -RuntimeDirectory (Get-RuntimeDirectoryForFailure)
    }
    Write-Error $_.Exception.Message
    exit 1
}
