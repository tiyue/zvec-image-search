[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Position = 0)]
    [string]$Command = "help",

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Arguments = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:ConfigSchemaVersion = 1
$script:ContainerImageRoot = "/data/roots/main"
$script:ContainerQueryRoot = "/data/query"
$script:ContainerWorkspace = "/data/workspace"
$script:ContainerResults = "/data/results"
$script:DefaultImageName = "zvec-image-search:local"
$script:AppUid = 10001
$script:AppGid = 10001
$script:ScriptRoot = Split-Path -Parent $PSCommandPath
$script:RepoRoot = Split-Path -Parent $script:ScriptRoot

function Write-Usage {
    @"
Zvec Docker launcher

One-time setup:
  zvec init <image-folder> [--workspace <folder>] [--results <folder>]

Daily commands:
  zvec index [tags...] [options]
  zvec sync [options]
  zvec search <text> [--tk N] [--tags <tag...>]
  zvec search-image <image-file> [--tk N] [--tags <tag...>]
  zvec search-mix <image-file> <text> [--tk N] [--tags <tag...>]
  zvec stats
  zvec roots
  zvec results
  zvec clean [days] [--dry-run]
  zvec cache-clear
  zvec doctor
  zvec build [--clean]

Maintenance:
  zvec rebind-root <root-id> [new-image-folder]
  zvec migrate-schema [--dry-run]
  zvec raw <original zvec-image-search arguments>
"@ | Write-Host
}

function Get-ConfigHome {
    if (-not [string]::IsNullOrWhiteSpace($env:ZVEC_DOCKER_CONFIG_HOME)) {
        return [System.IO.Path]::GetFullPath($env:ZVEC_DOCKER_CONFIG_HOME)
    }
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        return Join-Path $env:LOCALAPPDATA "zvec-image-search"
    }
    return Join-Path $HOME ".zvec-image-search"
}

function Get-ConfigPath {
    return Join-Path (Get-ConfigHome) "config.json"
}

function Get-EnvPath {
    return Join-Path (Get-ConfigHome) ".env"
}

function Ensure-Directory {
    param([Parameter(Mandatory = $true)][string]$Path)

    return [System.IO.Directory]::CreateDirectory(
        [System.IO.Path]::GetFullPath($Path)
    ).FullName
}

function Test-DirectoryWritable {
    param([Parameter(Mandatory = $true)][string]$Path)

    $directory = Ensure-Directory $Path
    $probe = Join-Path $directory (
        ".zvec-write-test-$PID-$([guid]::NewGuid().ToString('N')).tmp"
    )
    try {
        [System.IO.File]::WriteAllText($probe, "")
        return $true
    }
    catch {
        return $false
    }
    finally {
        if (Test-Path -LiteralPath $probe) {
            Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
        }
    }
}

function Resolve-Directory {
    param([Parameter(Mandatory = $true)][string]$Path)

    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if (-not $item.PSIsContainer) {
        throw "Directory does not exist: $Path"
    }
    return $item.FullName
}

function Resolve-File {
    param([Parameter(Mandatory = $true)][string]$Path)

    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($item.PSIsContainer) {
        throw "Expected a file, but received a directory: $Path"
    }
    return $item.FullName
}

function Get-TextHash {
    param([Parameter(Mandatory = $true)][string]$Value)

    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        $hash = $sha256.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hash)).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
    }
}

function Write-Utf8File {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Protect-SecretFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ($env:OS -ne "Windows_NT") {
        return
    }
    try {
        $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        $security = New-Object System.Security.AccessControl.FileSecurity
        $security.SetOwner($identity.User)
        $security.SetAccessRuleProtection($true, $false)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $identity.User,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        $security.AddAccessRule($rule)
        [System.IO.File]::SetAccessControl($Path, $security)
    }
    catch {
        Write-Warning "Could not restrict the secret file ACL: $($_.Exception.Message)"
    }
}

function ConvertTo-PlainText {
    param(
        [Parameter(Mandatory = $true)]
        [System.Security.SecureString]$SecureString
    )

    $pointer = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureString)
    try {
        return [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function Save-ApiEnvironment {
    param([switch]$SkipPrompt)

    $configHome = Ensure-Directory (Get-ConfigHome)
    $envPath = Join-Path $configHome ".env"
    $apiKey = $env:DASHSCOPE_API_KEY

    if ([string]::IsNullOrWhiteSpace($apiKey) -and (Test-Path -LiteralPath $envPath)) {
        return
    }
    if ([string]::IsNullOrWhiteSpace($apiKey)) {
        if ($SkipPrompt) {
            return
        }
        $secureKey = Read-Host "DASHSCOPE_API_KEY" -AsSecureString
        $apiKey = ConvertTo-PlainText $secureKey
    }
    if ([string]::IsNullOrWhiteSpace($apiKey)) {
        throw "DASHSCOPE_API_KEY cannot be empty."
    }
    if ($apiKey.Contains("`r") -or $apiKey.Contains("`n")) {
        throw "DASHSCOPE_API_KEY cannot contain a newline."
    }

    $lines = @("DASHSCOPE_API_KEY=$apiKey")
    if (-not [string]::IsNullOrWhiteSpace($env:DASHSCOPE_API_URL)) {
        $lines += "DASHSCOPE_API_URL=$($env:DASHSCOPE_API_URL)"
    }
    Write-Utf8File -Path $envPath -Content (($lines -join "`n") + "`n")
    Protect-SecretFile -Path $envPath
}

function Save-Config {
    param([Parameter(Mandatory = $true)]$Config)

    $configHome = Ensure-Directory (Get-ConfigHome)
    $configPath = Join-Path $configHome "config.json"
    $temporaryPath = "$configPath.tmp"
    $json = $Config | ConvertTo-Json -Depth 5
    Write-Utf8File -Path $temporaryPath -Content ($json + "`n")
    Move-Item -LiteralPath $temporaryPath -Destination $configPath -Force
}

function Assert-ConfigProperty {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if ($Config.PSObject.Properties.Name -notcontains $Name) {
        throw "Invalid launcher config: missing '$Name'. Run 'zvec init' again."
    }
}

function Read-Config {
    param([switch]$Optional)

    $configPath = Get-ConfigPath
    if (-not (Test-Path -LiteralPath $configPath)) {
        if ($Optional) {
            return $null
        }
        throw "Launcher is not configured. Run 'zvec init <image-folder>' first."
    }
    try {
        $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "Invalid launcher config at ${configPath}: $($_.Exception.Message)"
    }

    foreach ($name in @(
        "schema_version",
        "image_name",
        "image_root",
        "workspace_type",
        "workspace_source",
        "results_directory"
    )) {
        Assert-ConfigProperty -Config $config -Name $name
    }
    if ([int]$config.schema_version -ne $script:ConfigSchemaVersion) {
        throw "Unsupported launcher config version: $($config.schema_version)"
    }
    if ($config.workspace_type -notin @("volume", "bind")) {
        throw "Invalid workspace_type: $($config.workspace_type)"
    }
    return $config
}

function Test-ModernWsl {
    if ($env:OS -ne "Windows_NT") {
        return $true
    }
    if ($null -eq (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        return $false
    }

    $processInfo = New-Object System.Diagnostics.ProcessStartInfo
    $processInfo.FileName = "wsl.exe"
    $processInfo.Arguments = "--version"
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $processInfo.StandardOutputEncoding = [System.Text.Encoding]::Unicode
    $processInfo.StandardErrorEncoding = [System.Text.Encoding]::Unicode
    $process = $null
    try {
        $process = [System.Diagnostics.Process]::Start($processInfo)
        $null = $process.StandardOutput.ReadToEnd()
        $null = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return $process.ExitCode -eq 0
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $process) {
            $process.Dispose()
        }
    }
}

function Get-VirtualizationStatus {
    if ($env:OS -ne "Windows_NT") {
        return "enabled"
    }
    try {
        $computer = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop
        $processor = Get-CimInstance Win32_Processor -ErrorAction Stop |
            Select-Object -First 1
        if (
            $computer.HypervisorPresent -eq $true -or
            $processor.VirtualizationFirmwareEnabled -eq $true
        ) {
            return "enabled"
        }
        if (
            $processor.VMMonitorModeExtensions -eq $true -and
            $processor.SecondLevelAddressTranslationExtensions -eq $true -and
            $processor.VirtualizationFirmwareEnabled -eq $false
        ) {
            return "disabled"
        }
    }
    catch {
        return "unknown"
    }
    return "unknown"
}

function Get-DockerServerVersion {
    $dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
    if ($null -eq $dockerCommand) {
        return $null
    }

    $processInfo = New-Object System.Diagnostics.ProcessStartInfo
    $processInfo.FileName = $dockerCommand.Source
    $processInfo.Arguments = 'version --format "{{.Server.Version}}"'
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $process = $null
    try {
        $process = [System.Diagnostics.Process]::Start($processInfo)
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(5000)) {
            $process.Kill()
            $process.WaitForExit()
            return $null
        }
        $null = $stderrTask.Result
        if ($process.ExitCode -ne 0) {
            return $null
        }
        $version = $stdoutTask.Result.Trim()
        return $(if ([string]::IsNullOrWhiteSpace($version)) { $null } else { $version })
    }
    catch {
        return $null
    }
    finally {
        if ($null -ne $process) {
            $process.Dispose()
        }
    }
}

function Start-DockerDesktop {
    if ($env:OS -ne "Windows_NT") {
        return $false
    }
    $desktopPath = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path -LiteralPath $desktopPath -PathType Leaf)) {
        return $false
    }
    if ($null -eq (Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue)) {
        Start-Process -FilePath $desktopPath -WindowStyle Hidden
    }
    return $true
}

function Wait-DockerServerVersion {
    param([int]$TimeoutSeconds = 180)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $version = Get-DockerServerVersion
        if (-not [string]::IsNullOrWhiteSpace($version)) {
            return $version
        }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)
    return $null
}

function Assert-DockerReady {
    param([switch]$StartIfStopped)

    if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker CLI was not found. Install Docker Desktop first."
    }
    $serverVersion = Get-DockerServerVersion
    $modernWsl = Test-ModernWsl
    $virtualizationStatus = Get-VirtualizationStatus
    if (
        [string]::IsNullOrWhiteSpace($serverVersion) -and
        $StartIfStopped -and
        $modernWsl -and
        $virtualizationStatus -ne "disabled" -and
        (Start-DockerDesktop)
    ) {
        Write-Host "Starting Docker Desktop ..."
        $serverVersion = Wait-DockerServerVersion
    }
    if ([string]::IsNullOrWhiteSpace($serverVersion)) {
        $message = "Docker Desktop is not running or the Docker engine is unavailable."
        if (-not $modernWsl) {
            $message += " Legacy Inbox WSL was detected. Open an administrator " +
                "PowerShell and run: wsl --update --web-download"
        }
        elseif ($virtualizationStatus -eq "disabled") {
            $message += " Firmware virtualization is disabled. Enable SVM Mode " +
                "(AMD) or Intel VT-x in BIOS/UEFI, then restart Windows."
        }
        throw $message
    }
    return $serverVersion
}

function Invoke-DockerQuietly {
    param([Parameter(Mandatory = $true)][object[]]$Arguments)

    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & docker @Arguments *> $null
        return $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
}

function Test-DockerImage {
    param([Parameter(Mandatory = $true)][string]$ImageName)

    $exitCode = Invoke-DockerQuietly -Arguments @(
        "inspect", "--type", "image", $ImageName
    )
    return $exitCode -eq 0
}

function Get-DockerImageUid {
    param([Parameter(Mandatory = $true)][string]$ImageName)

    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & docker run --rm --entrypoint id $ImageName -u 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($exitCode -ne 0) {
        throw "Could not verify the runtime user for '$ImageName'."
    }
    return ([string]($output -join "`n")).Trim()
}

function Build-DockerImage {
    param(
        [Parameter(Mandatory = $true)][string]$ImageName,
        [switch]$NoCache
    )

    $null = Assert-DockerReady -StartIfStopped
    Write-Host "Building $ImageName ..."
    $dockerArguments = @(
        "build",
        "--provenance=false",
        "--file", (Join-Path $script:RepoRoot "Dockerfile"),
        "--tag", $ImageName
    )
    if ($NoCache) {
        $dockerArguments += "--no-cache"
    }
    $dockerArguments += $script:RepoRoot
    & docker @dockerArguments
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    $uid = Get-DockerImageUid -ImageName $ImageName
    if ($uid -ne [string]$script:AppUid) {
        throw "Built image uses UID $uid; expected $script:AppUid."
    }
    $helpExitCode = Invoke-DockerQuietly -Arguments @(
        "run", "--rm", $ImageName, "--help"
    )
    if ($helpExitCode -ne 0) {
        throw "Built image CLI verification failed."
    }
    Write-Host "Verified $ImageName (UID $uid, CLI ready)."
}

function Initialize-WorkspaceVolume {
    param([Parameter(Mandatory = $true)]$Config)

    if ($Config.workspace_type -ne "volume") {
        return
    }
    & docker volume create $Config.workspace_source *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create Docker volume: $($Config.workspace_source)"
    }

    $mount = "type=volume,source=$($Config.workspace_source),target=$script:ContainerWorkspace"
    & docker run --rm --user "0:0" --entrypoint /bin/sh `
        --mount $mount $Config.image_name `
        -c "chown $script:AppUid`:$script:AppGid $script:ContainerWorkspace"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not initialize Docker volume permissions."
    }
}

function New-BindMount {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Target,
        [switch]$ReadOnly
    )

    if ($Source.Contains(",")) {
        throw "Docker bind mount paths cannot contain a comma: $Source"
    }
    $mount = "type=bind,source=$Source,target=$Target"
    if ($ReadOnly) {
        $mount += ",readonly"
    }
    return $mount
}

function Get-WorkspaceMount {
    param([Parameter(Mandatory = $true)]$Config)

    if ($Config.workspace_type -eq "volume") {
        return "type=volume,source=$($Config.workspace_source),target=$script:ContainerWorkspace"
    }
    $workspace = Ensure-Directory ([string]$Config.workspace_source)
    return New-BindMount -Source $workspace -Target $script:ContainerWorkspace
}

function Get-QueryMount {
    param([Parameter(Mandatory = $true)][string]$ImagePath)

    $resolved = Resolve-File $ImagePath
    $file = Get-Item -LiteralPath $resolved
    return [pscustomobject]@{
        HostDirectory = $file.Directory.FullName
        ContainerPath = "$script:ContainerQueryRoot/$($file.Name)"
    }
}

function Test-ContainerMounts {
    param([Parameter(Mandatory = $true)]$Config)

    $imageRoot = Resolve-Directory ([string]$Config.image_root)
    $resultsDirectory = Ensure-Directory ([string]$Config.results_directory)
    $permissionCheck = (
        "test -r $script:ContainerImageRoot && " +
        "test ! -w $script:ContainerImageRoot && " +
        "test -w $script:ContainerWorkspace && " +
        "test -w $script:ContainerResults"
    )
    $dockerArguments = @(
        "run",
        "--rm",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges=true",
        "--mount", (New-BindMount -Source $imageRoot -Target $script:ContainerImageRoot -ReadOnly),
        "--mount", (Get-WorkspaceMount -Config $Config),
        "--mount", (New-BindMount -Source $resultsDirectory -Target $script:ContainerResults),
        "--entrypoint", "/bin/sh",
        $Config.image_name,
        "-c",
        $permissionCheck
    )
    $exitCode = Invoke-DockerQuietly -Arguments $dockerArguments
    return $exitCode -eq 0
}

function Invoke-ZvecContainer {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)][object[]]$CliArguments,
        [string]$QueryDirectory
    )

    $null = Assert-DockerReady -StartIfStopped
    if (-not (Test-DockerImage -ImageName $Config.image_name)) {
        throw "Docker image '$($Config.image_name)' was not found. Run 'zvec build'."
    }

    $imageRoot = Resolve-Directory ([string]$Config.image_root)
    $resultsDirectory = Ensure-Directory ([string]$Config.results_directory)
    $dockerArguments = @(
        "run",
        "--rm",
        "--init",
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges=true",
        "--mount", (New-BindMount -Source $imageRoot -Target $script:ContainerImageRoot -ReadOnly),
        "--mount", (Get-WorkspaceMount -Config $Config),
        "--mount", (New-BindMount -Source $resultsDirectory -Target $script:ContainerResults)
    )

    if (-not [string]::IsNullOrWhiteSpace($QueryDirectory)) {
        $dockerArguments += @(
            "--mount",
            (New-BindMount -Source $QueryDirectory -Target $script:ContainerQueryRoot -ReadOnly)
        )
    }

    $envPath = Get-EnvPath
    if (Test-Path -LiteralPath $envPath) {
        $dockerArguments += @("--env-file", $envPath)
    }
    if (-not [string]::IsNullOrWhiteSpace($env:DASHSCOPE_API_KEY)) {
        $dockerArguments += @("--env", "DASHSCOPE_API_KEY")
    }
    if (-not [string]::IsNullOrWhiteSpace($env:DASHSCOPE_API_URL)) {
        $dockerArguments += @("--env", "DASHSCOPE_API_URL")
    }

    $dockerArguments += @($Config.image_name)
    $dockerArguments += $CliArguments
    & docker @dockerArguments
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

function Invoke-Init {
    param([string[]]$InitArguments)

    $imageRootArgument = $null
    $workspaceArgument = $null
    $workspaceVolume = $null
    $resultsArgument = $null
    $imageNameArgument = $null
    $noBuild = $false
    $skipKey = $false

    for ($index = 0; $index -lt $InitArguments.Count; $index++) {
        $value = $InitArguments[$index]
        switch ($value) {
            "--workspace" {
                if (++$index -ge $InitArguments.Count) {
                    throw "--workspace requires a directory."
                }
                $workspaceArgument = $InitArguments[$index]
            }
            "--workspace-volume" {
                if (++$index -ge $InitArguments.Count) {
                    throw "--workspace-volume requires a volume name."
                }
                $workspaceVolume = $InitArguments[$index]
            }
            "--results" {
                if (++$index -ge $InitArguments.Count) {
                    throw "--results requires a directory."
                }
                $resultsArgument = $InitArguments[$index]
            }
            "--image-name" {
                if (++$index -ge $InitArguments.Count) {
                    throw "--image-name requires a Docker image name."
                }
                $imageNameArgument = $InitArguments[$index]
            }
            "--no-build" {
                $noBuild = $true
            }
            "--skip-key" {
                $skipKey = $true
            }
            default {
                if ($value.StartsWith("--")) {
                    throw "Unknown init option: $value"
                }
                if ($null -ne $imageRootArgument) {
                    throw "init accepts one image folder."
                }
                $imageRootArgument = $value
            }
        }
    }

    if ([string]::IsNullOrWhiteSpace($imageRootArgument)) {
        throw "Usage: zvec init <image-folder> [--workspace <folder>]"
    }
    if ($null -ne $workspaceArgument -and $null -ne $workspaceVolume) {
        throw "Use either --workspace or --workspace-volume, not both."
    }

    $imageRoot = Resolve-Directory $imageRootArgument
    $rootHash = (Get-TextHash $imageRoot.ToLowerInvariant()).Substring(0, 12)
    $existingConfig = Read-Config -Optional
    $sameRoot = $null -ne $existingConfig -and (
        [string]$existingConfig.image_root -eq $imageRoot
    )

    $imageName = if ($null -ne $imageNameArgument) {
        $imageNameArgument
    }
    elseif ($sameRoot) {
        [string]$existingConfig.image_name
    }
    else {
        $script:DefaultImageName
    }

    if ($null -ne $workspaceArgument) {
        $workspaceType = "bind"
        $workspaceSource = Ensure-Directory $workspaceArgument
    }
    elseif ($null -ne $workspaceVolume) {
        $workspaceType = "volume"
        $workspaceSource = $workspaceVolume
    }
    elseif ($sameRoot) {
        $workspaceType = [string]$existingConfig.workspace_type
        $workspaceSource = [string]$existingConfig.workspace_source
    }
    else {
        $workspaceType = "volume"
        $workspaceSource = "zvec-image-workspace-$rootHash"
    }

    if ($null -ne $resultsArgument) {
        $resultsDirectory = Ensure-Directory $resultsArgument
    }
    elseif ($sameRoot) {
        $resultsDirectory = Ensure-Directory ([string]$existingConfig.results_directory)
    }
    else {
        $resultsDirectory = Ensure-Directory (
            Join-Path (Get-ConfigHome) (Join-Path "results" $rootHash)
        )
    }

    $config = [ordered]@{
        schema_version = $script:ConfigSchemaVersion
        image_name = $imageName
        image_root = $imageRoot
        workspace_type = $workspaceType
        workspace_source = $workspaceSource
        results_directory = $resultsDirectory
    }

    Save-Config -Config $config
    Save-ApiEnvironment -SkipPrompt:$skipKey

    if (-not $noBuild) {
        Build-DockerImage -ImageName $imageName
        Initialize-WorkspaceVolume -Config ([pscustomobject]$config)
    }

    Write-Host "Configured image root: $imageRoot"
    Write-Host "Configured results:    $resultsDirectory"
    Write-Host "Docker image:          $imageName"
    Write-Host "Run 'zvec index' to build the image index."
}

function Write-DoctorResult {
    param(
        [Parameter(Mandatory = $true)][string]$Status,
        [Parameter(Mandatory = $true)][string]$Message
    )
    Write-Host ("[{0}] {1}" -f $Status, $Message)
}

function Invoke-Doctor {
    $failures = 0
    $config = $null

    try {
        $config = Read-Config
        Write-DoctorResult -Status "OK" -Message "Launcher config: $(Get-ConfigPath)"
    }
    catch {
        Write-DoctorResult -Status "FAIL" -Message $_.Exception.Message
        $failures++
    }

    if ($null -ne (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-DoctorResult -Status "OK" -Message "Docker CLI found"
    }
    else {
        Write-DoctorResult -Status "FAIL" -Message "Docker CLI not found"
        $failures++
    }

    $dockerReady = $false
    if ($null -ne (Get-Command docker -ErrorAction SilentlyContinue)) {
        try {
            $serverVersion = Assert-DockerReady
            Write-DoctorResult -Status "OK" -Message "Docker engine $serverVersion"
            $dockerReady = $true
        }
        catch {
            Write-DoctorResult -Status "FAIL" -Message $_.Exception.Message
            $failures++
        }
    }

    if ($null -ne $config) {
        if (Test-Path -LiteralPath $config.image_root -PathType Container) {
            Write-DoctorResult -Status "OK" -Message "Image root: $($config.image_root)"
        }
        else {
            Write-DoctorResult -Status "FAIL" -Message "Image root is unavailable"
            $failures++
        }
        if (Test-DirectoryWritable ([string]$config.results_directory)) {
            Write-DoctorResult -Status "OK" -Message "Results directory is writable"
        }
        else {
            Write-DoctorResult -Status "FAIL" -Message "Results directory is not writable"
            $failures++
        }

        if ($dockerReady) {
            if (Test-DockerImage -ImageName $config.image_name) {
                Write-DoctorResult -Status "OK" -Message "Docker image: $($config.image_name)"
                try {
                    $uid = Get-DockerImageUid -ImageName $config.image_name
                    if ($uid -eq [string]$script:AppUid) {
                        Write-DoctorResult -Status "OK" -Message (
                            "Container runtime UID: $uid"
                        )
                    }
                    else {
                        Write-DoctorResult -Status "FAIL" -Message (
                            "Container runtime UID is $uid; expected $script:AppUid"
                        )
                        $failures++
                    }
                }
                catch {
                    Write-DoctorResult -Status "FAIL" -Message $_.Exception.Message
                    $failures++
                }
                if (Test-ContainerMounts -Config $config) {
                    Write-DoctorResult -Status "OK" -Message (
                        "Container mounts have the expected permissions"
                    )
                }
                else {
                    Write-DoctorResult -Status "FAIL" -Message (
                        "Container mount permission check failed"
                    )
                    $failures++
                }
            }
            else {
                Write-DoctorResult -Status "FAIL" -Message "Docker image is missing"
                $failures++
            }
        }
    }

    if (
        -not [string]::IsNullOrWhiteSpace($env:DASHSCOPE_API_KEY) -or
        (Test-Path -LiteralPath (Get-EnvPath))
    ) {
        Write-DoctorResult -Status "OK" -Message "DashScope API key is configured"
    }
    else {
        Write-DoctorResult -Status "WARN" -Message "DashScope API key is not configured"
    }

    if ($failures -gt 0) {
        exit 1
    }
}

try {
    $normalizedCommand = $Command.ToLowerInvariant()
    switch ($normalizedCommand) {
        { $_ -in @("help", "--help", "-h") } {
            Write-Usage
        }
        "init" {
            Invoke-Init -InitArguments $Arguments
        }
        "build" {
            $cleanBuild = $false
            foreach ($argument in $Arguments) {
                if ($argument -in @("--clean", "--no-cache")) {
                    $cleanBuild = $true
                }
                else {
                    throw "Usage: zvec build [--clean]"
                }
            }
            $config = Read-Config -Optional
            $imageName = if ($null -ne $config) {
                [string]$config.image_name
            }
            else {
                $script:DefaultImageName
            }
            Build-DockerImage -ImageName $imageName -NoCache:$cleanBuild
            if ($null -ne $config) {
                Initialize-WorkspaceVolume -Config $config
            }
        }
        "doctor" {
            Invoke-Doctor
        }
        "results" {
            $config = Read-Config
            $resultsDirectory = Ensure-Directory ([string]$config.results_directory)
            Start-Process explorer.exe -ArgumentList $resultsDirectory
        }
        "index" {
            $config = Read-Config
            Invoke-ZvecContainer -Config $config -CliArguments (
                @("index", $script:ContainerImageRoot) + $Arguments
            )
        }
        "sync" {
            $config = Read-Config
            Invoke-ZvecContainer -Config $config -CliArguments (
                @("sync", $script:ContainerImageRoot) + $Arguments
            )
        }
        "search" {
            if ($Arguments.Count -lt 1) {
                throw "Usage: zvec search <text> [--tk N] [--tags <tag...>]"
            }
            $config = Read-Config
            if ($Arguments[0] -eq "--text") {
                $cliArguments = @("search") + $Arguments
            }
            else {
                $cliArguments = @("search", "--text", $Arguments[0])
                if ($Arguments.Count -gt 1) {
                    $cliArguments += $Arguments[1..($Arguments.Count - 1)]
                }
            }
            Invoke-ZvecContainer -Config $config -CliArguments $cliArguments
        }
        "search-image" {
            if ($Arguments.Count -lt 1) {
                throw "Usage: zvec search-image <image-file> [--tk N] [--tags <tag...>]"
            }
            $config = Read-Config
            $query = Get-QueryMount $Arguments[0]
            $cliArguments = @("search", "--image", $query.ContainerPath)
            if ($Arguments.Count -gt 1) {
                $cliArguments += $Arguments[1..($Arguments.Count - 1)]
            }
            Invoke-ZvecContainer -Config $config -CliArguments $cliArguments `
                -QueryDirectory $query.HostDirectory
        }
        "search-mix" {
            if ($Arguments.Count -lt 2) {
                throw "Usage: zvec search-mix <image-file> <text> [--tk N] [--tags <tag...>]"
            }
            $config = Read-Config
            $query = Get-QueryMount $Arguments[0]
            $cliArguments = @(
                "search",
                "--image", $query.ContainerPath,
                "--text", $Arguments[1]
            )
            if ($Arguments.Count -gt 2) {
                $cliArguments += $Arguments[2..($Arguments.Count - 1)]
            }
            Invoke-ZvecContainer -Config $config -CliArguments $cliArguments `
                -QueryDirectory $query.HostDirectory
        }
        "stats" {
            $config = Read-Config
            Invoke-ZvecContainer -Config $config -CliArguments @("stats")
        }
        "roots" {
            $config = Read-Config
            Invoke-ZvecContainer -Config $config -CliArguments @("roots")
        }
        "cache-clear" {
            $config = Read-Config
            Invoke-ZvecContainer -Config $config -CliArguments @("cache-clear")
        }
        "clean" {
            $config = Read-Config
            $days = 7
            $remaining = @()
            if ($Arguments.Count -gt 0) {
                $parsedDays = 0
                if ([int]::TryParse($Arguments[0], [ref]$parsedDays)) {
                    if ($parsedDays -lt 0) {
                        throw "days cannot be negative."
                    }
                    $days = $parsedDays
                    if ($Arguments.Count -gt 1) {
                        $remaining = $Arguments[1..($Arguments.Count - 1)]
                    }
                }
                else {
                    $remaining = $Arguments
                }
            }
            Invoke-ZvecContainer -Config $config -CliArguments (
                @("clean-results", "--days", [string]$days) + $remaining
            )
        }
        "rebind-root" {
            if ($Arguments.Count -lt 1 -or $Arguments.Count -gt 2) {
                throw "Usage: zvec rebind-root <root-id> [new-image-folder]"
            }
            $config = Read-Config
            if ($Arguments.Count -eq 2) {
                $config.image_root = Resolve-Directory $Arguments[1]
            }
            Invoke-ZvecContainer -Config $config -CliArguments @(
                "rebind-root", $Arguments[0], $script:ContainerImageRoot
            )
            if ($Arguments.Count -eq 2) {
                Save-Config -Config $config
            }
        }
        "migrate-path-schema" {
            $config = Read-Config
            Invoke-ZvecContainer -Config $config -CliArguments (
                @("migrate-path-schema") + $Arguments
            )
        }
        "migrate-schema" {
            $config = Read-Config
            Invoke-ZvecContainer -Config $config -CliArguments (
                @("migrate-schema") + $Arguments
            )
        }
        "raw" {
            if ($Arguments.Count -lt 1) {
                throw "Usage: zvec raw <original zvec-image-search arguments>"
            }
            $config = Read-Config
            Invoke-ZvecContainer -Config $config -CliArguments $Arguments
        }
        default {
            throw "Unknown command '$Command'. Run 'zvec help'."
        }
    }
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}

exit 0
