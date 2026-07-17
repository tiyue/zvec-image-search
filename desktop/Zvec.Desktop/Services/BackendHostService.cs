using System.Diagnostics;
using System.Globalization;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Zvec.Desktop.Models;

namespace Zvec.Desktop.Services;

public sealed class BackendHostService : IDisposable, IAsyncDisposable
{
    private readonly LauncherConfigService? _configService;
    private readonly LauncherConfig? _configuredLauncherConfig;
    private readonly ApiCredentialService _credentialService;
    private readonly BackendHostOptions _options;
    private readonly IBackendProcessFactory _processFactory;
    private readonly IRuntimeBootstrapper? _runtimeBootstrapper;
    private readonly BackendInstanceRegistry _instanceRegistry;
    private readonly BackendSessionTokenStore _sessionTokenStore;
    private readonly SemaphoreSlim _lifecycleGate = new(1, 1);

    private BackendApiClient? _apiClient;
    private IBackendProcess? _backendProcess;
    private LauncherConfig? _activeLauncherConfig;
    private string? _instanceId;
    private string? _queryStagingDirectory;
    private string? _runtimeConfigurationDirectory;
    private string? _runtimeConfigurationPath;
    private string? _preparedPythonExecutable;
    private string? _sessionToken;
    private string? _configFingerprint;
    private string? _modelsConfigurationPath;
    private BackendInstanceDescriptor? _instanceDescriptor;
    private BackendConnectionMode _connectionMode;
    private bool _drainOnly;
    private bool _ownsQueryStagingDirectory;
    private bool _disposed;
    private int _hostPort;

    public BackendHostService()
        : this(new LauncherConfigService())
    {
    }

    public BackendHostService(
        LauncherConfigService configService,
        BackendHostOptions? options = null,
        ApiCredentialService? credentialService = null)
        : this(
            configService,
            configuredLauncherConfig: null,
            options,
            credentialService,
            new BackendProcessFactory(),
            runtimeBootstrapper: null
        )
    {
    }

    public BackendHostService(
        LauncherConfig launcherConfig,
        string? environmentPath = null,
        BackendHostOptions? options = null,
        ApiCredentialService? credentialService = null)
        : this(
            configService: null,
            launcherConfig,
            options,
            credentialService,
            new BackendProcessFactory(),
            runtimeBootstrapper: null
        )
    {
        _ = environmentPath; // Source compatibility with the original desktop API.
    }

    internal BackendHostService(
        LauncherConfig launcherConfig,
        BackendHostOptions options,
        IBackendProcessFactory processFactory,
        IRuntimeBootstrapper? runtimeBootstrapper = null)
        : this(
            configService: null,
            launcherConfig,
            options,
            credentialService: null,
            processFactory,
            runtimeBootstrapper
        )
    {
    }

    private BackendHostService(
        LauncherConfigService? configService,
        LauncherConfig? configuredLauncherConfig,
        BackendHostOptions? options,
        ApiCredentialService? credentialService,
        IBackendProcessFactory processFactory,
        IRuntimeBootstrapper? runtimeBootstrapper)
    {
        if (configService is null && configuredLauncherConfig is null)
        {
            throw new ArgumentNullException(nameof(configuredLauncherConfig));
        }
        if (configService is not null && configuredLauncherConfig is not null)
        {
            throw new ArgumentException(
                "Specify either a config service or a configured launcher config, not both."
            );
        }

        _configService = configService;
        _configuredLauncherConfig = configuredLauncherConfig;
        _credentialService = credentialService ?? (configService is null
            ? new ApiCredentialService()
            : new ApiCredentialService(configService: configService));
        _options = ValidateOptions(options ?? new BackendHostOptions());
        _processFactory = processFactory ?? throw new ArgumentNullException(
            nameof(processFactory)
        );
        _runtimeBootstrapper = runtimeBootstrapper;
        var configHome = configService?.ConfigHome ?? ResolveConfigHome();
        _instanceRegistry = new BackendInstanceRegistry(configHome);
        _sessionTokenStore = new BackendSessionTokenStore(configHome);
    }

    public bool IsRunning => _apiClient is not null && (
        _connectionMode == BackendConnectionMode.Attached ||
        _backendProcess is { HasExited: false }
    );

    public BackendConnectionMode ConnectionMode => _connectionMode;

    public bool IsDrainOnly => _drainOnly;

    public bool SupportsLowConfidenceOverride { get; private set; }

    public bool SupportsTagOnlySearch { get; private set; }

    public bool SupportsHybridTagVectorSearch { get; private set; }

    public bool SupportsResultDiversity { get; private set; }

    public bool SupportsIndexAndAutoTag { get; private set; }

    public bool SupportsMetadataEmbeddingSearch { get; private set; }

    public bool SupportsMetadataEmbeddingBackfill { get; private set; }

    public bool SupportsPersistentBackendSession { get; private set; }

    public bool SupportsGracefulShutdown { get; private set; }

    public BackendApiClient ApiClient => _apiClient ?? throw new InvalidOperationException(
        "The backend has not been started. Call StartAsync first."
    );

    public LauncherConfig LauncherConfig => _activeLauncherConfig ??
        _configuredLauncherConfig ??
        throw new InvalidOperationException("The launcher configuration has not been loaded.");

    public string InstanceId => _instanceId ?? throw new InvalidOperationException(
        "The backend has not been started."
    );

    public string QueryStagingDirectory => _queryStagingDirectory ??
        throw new InvalidOperationException("The backend has not been started.");

    public int HostPort => _hostPort;

    public Uri BaseAddress => ApiClient.BaseAddress;

    public async Task<BackendApiClient> StartAsync(
        CancellationToken cancellationToken = default)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        await _lifecycleGate.WaitAsync(cancellationToken);
        try
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            if (IsRunning)
            {
                return _apiClient!;
            }

            var config = _configuredLauncherConfig ??
                await _configService!.LoadAsync(cancellationToken) ??
                throw new InvalidOperationException(
                    $"Zvec has not been initialized. Launcher config was not found at " +
                    $"'{_configService.ConfigPath}'."
                );
            ValidateLauncherConfig(config);
            _activeLauncherConfig = config;
            var entryPoint = ResolveBackendEntryPoint();
            // Invalid models.json must block a new backend before any registry or
            // process state is created. Missing configuration is materialized atomically.
            var modelConfigurationService = new ModelConfigurationService();
            _modelsConfigurationPath = modelConfigurationService.ConfigurationPath;
            var modelConfiguration = await modelConfigurationService.LoadStrictAsync(
                createIfMissing: true,
                cancellationToken
            );
            _configFingerprint = await BackendConfigurationFingerprint.ComputeAsync(
                config,
                entryPoint,
                modelConfiguration,
                cancellationToken
            );

            await using var launchLock = await _instanceRegistry.AcquireLaunchLockAsync(
                cancellationToken
            );
            var attached = await TryAttachExistingAsync(
                config,
                _configFingerprint,
                launchLock,
                cancellationToken
            );
            if (attached is not null)
            {
                return attached;
            }

            _preparedPythonExecutable = await PrepareNativeRuntimeAsync(
                config,
                cancellationToken
            );
            _instanceId = $"zvec-backend-{Environment.ProcessId}-{Guid.NewGuid():N}";
            _hostPort = ReserveEphemeralLoopbackPort();
            _sessionToken = CreateSessionToken();
            _connectionMode = BackendConnectionMode.Spawned;
            _drainOnly = false;

            PrepareRuntimeDirectories();
            await WriteRuntimeConfigurationAsync(config, cancellationToken);
            _sessionTokenStore.SaveToken(_sessionToken);
            var registeredAt = DateTimeOffset.UtcNow;
            _instanceDescriptor = CreateDescriptor(
                BackendInstanceStates.Starting,
                registeredAt,
                wrapperPid: null,
                generatedUtc: registeredAt
            );
            await _instanceRegistry.WriteUnderLockAsync(
                _instanceDescriptor,
                launchLock,
                cancellationToken
            );

            var launch = ResolveLaunchCommand(config, entryPoint);
            var startInfo = BuildProcessStartInfo(
                launch,
                _sessionToken,
                _configFingerprint
            );

            _backendProcess = _processFactory.Create();
            await _backendProcess.StartAsync(startInfo, cancellationToken);
            var processStart = _backendProcess.StartTimeUtc ?? registeredAt;
            _instanceDescriptor = CreateDescriptor(
                BackendInstanceStates.Starting,
                processStart,
                _backendProcess.ProcessId,
                DateTimeOffset.UtcNow
            );
            await _instanceRegistry.WriteUnderLockAsync(
                _instanceDescriptor,
                launchLock,
                cancellationToken
            );
            _apiClient = CreateApiClient(_sessionToken, submissionDisabledReason: null);

            await WaitUntilHealthyAsync(_apiClient, cancellationToken);
            var version = await ValidateBackendVersionAsync(_apiClient, cancellationToken);
            ValidateInstanceIdentity(version, _instanceDescriptor);
            await ConfigureStoredCredentialsAsync(_apiClient, cancellationToken);
            _instanceDescriptor = CreateDescriptor(
                BackendInstanceStates.Ready,
                processStart,
                _backendProcess.ProcessId,
                DateTimeOffset.UtcNow
            );
            await _instanceRegistry.WriteUnderLockAsync(
                _instanceDescriptor,
                launchLock,
                cancellationToken
            );
            return _apiClient;
        }
        catch
        {
            if (_backendProcess is not null || _apiClient is not null ||
                _instanceDescriptor is not null)
            {
                if (_connectionMode == BackendConnectionMode.Attached)
                {
                    DetachCore();
                }
                else
                {
                    // No other desktop can submit work while this host still owns
                    // launch.lock and the descriptor is in its startup path. It is
                    // therefore safe to terminate only this failed, owned launch.
                    await StopCoreAsync(forceOwnedProcessTermination: true);
                }
            }
            else
            {
                CleanupRuntimeDirectories();
                ResetRuntimeState();
            }
            throw;
        }
        finally
        {
            _lifecycleGate.Release();
        }
    }

    private async Task<BackendApiClient?> TryAttachExistingAsync(
        LauncherConfig config,
        string currentConfigFingerprint,
        BackendLaunchLock launchLock,
        CancellationToken cancellationToken)
    {
        var descriptor = await _instanceRegistry.ReadAsync(cancellationToken);
        if (descriptor is null)
        {
            return null;
        }
        var token = _sessionTokenStore.ReadToken();
        if (string.IsNullOrWhiteSpace(token))
        {
            if (!IsBackendProcessConfirmedExited(ownedProcess: null, descriptor))
            {
                throw new BackendProtocolException(
                    "检测到仍在运行或无法确认已退出的常驻后端，但其会话凭据已丢失。" +
                    "为避免启动第二个实例并抢占图库，已保留原实例登记；" +
                    "请先结束原后端进程或重新启动电脑后再试。"
                );
            }
            await _instanceRegistry.DeleteIfMatchesUnderLockAsync(
                descriptor.InstanceId,
                descriptor.ProcessStartUtc,
                launchLock,
                cancellationToken
            );
            return null;
        }

        var configurationMatches = string.Equals(
            descriptor.ConfigFingerprint,
            currentConfigFingerprint,
            StringComparison.Ordinal
        );
        var drainReason = configurationMatches
            ? null
            : "后台正在完成旧图库配置中的任务；当前连接仅用于查看或取消任务，" +
                "任务结束后将按新配置重启后端。";
        var client = new BackendApiClient(
            new Uri($"http://{descriptor.Host}:{descriptor.Port}/", UriKind.Absolute),
            token,
            httpClient: null,
            submissionDisabledReason: drainReason
        );
        try
        {
            BackendHealthResponse health;
            try
            {
                using var healthTimeout = CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken
                );
                healthTimeout.CancelAfter(_options.HealthRequestTimeout);
                health = await client.GetHealthAsync(healthTimeout.Token);
            }
            catch (Exception exception) when (
                exception is HttpRequestException or TaskCanceledException or BackendApiException &&
                !cancellationToken.IsCancellationRequested
            )
            {
                if (!IsRegisteredProcessAlive(descriptor))
                {
                    client.Dispose();
                    await DeleteRegistrationUnderLockAsync(
                        descriptor,
                        token,
                        launchLock,
                        cancellationToken
                    );
                    return null;
                }

                ApplyAttachedDescriptor(descriptor, token, client, !configurationMatches);
                try
                {
                    await WaitUntilHealthyAsync(client, cancellationToken);
                    health = await client.GetHealthAsync(cancellationToken);
                }
                catch (Exception waitException) when (
                    waitException is TimeoutException or HttpRequestException or
                        BackendApiException && !cancellationToken.IsCancellationRequested &&
                    (descriptor.WrapperPid is null || !IsRegisteredProcessAlive(descriptor))
                )
                {
                    DetachCore();
                    await DeleteRegistrationUnderLockAsync(
                        descriptor,
                        token,
                        launchLock,
                        cancellationToken
                    );
                    return null;
                }
            }

            ValidateInstanceIdentity(health, descriptor);
            ApplyAttachedDescriptor(descriptor, token, client, !configurationMatches);
            if (!health.IsReady)
            {
                await WaitUntilHealthyAsync(client, cancellationToken);
                health = await client.GetHealthAsync(cancellationToken);
                ValidateInstanceIdentity(health, descriptor);
            }
            var version = await ValidateBackendVersionAsync(client, cancellationToken);
            ValidateInstanceIdentity(version, descriptor);
            if (!version.Capabilities.PersistentBackendSession)
            {
                throw new BackendProtocolException(
                    "已登记的后端不支持持久会话重连，请先结束旧后端后重试。"
                );
            }

            if (!configurationMatches && !health.HasActiveWork)
            {
                if (!version.Capabilities.GracefulShutdown)
                {
                    throw new BackendProtocolException(
                        "旧配置后端已经空闲，但不支持安全关闭；请重新启动软件。"
                    );
                }
                await client.ShutdownIfIdleAsync(cancellationToken);
                if (!await WaitForEndpointExitAsync(client, cancellationToken))
                {
                    throw new TimeoutException(
                        "旧配置后端未在超时时间内安全退出；实例登记已保留。"
                    );
                }
                client.Dispose();
                await DeleteRegistrationUnderLockAsync(
                    descriptor,
                    token,
                    launchLock,
                    cancellationToken
                );
                ResetRuntimeState();
                _activeLauncherConfig = config;
                _configFingerprint = currentConfigFingerprint;
                return null;
            }

            await ConfigureStoredCredentialsAsync(client, cancellationToken);
            if (!descriptor.IsReady)
            {
                _instanceDescriptor = CopyDescriptor(
                    descriptor,
                    state: BackendInstanceStates.Ready,
                    generatedUtc: DateTimeOffset.UtcNow
                );
                await _instanceRegistry.WriteUnderLockAsync(
                    _instanceDescriptor,
                    launchLock,
                    cancellationToken
                );
            }
            return client;
        }
        catch
        {
            if (!ReferenceEquals(_apiClient, client))
            {
                client.Dispose();
            }
            throw;
        }
    }

    private void ApplyAttachedDescriptor(
        BackendInstanceDescriptor descriptor,
        string token,
        BackendApiClient client,
        bool drainOnly)
    {
        _instanceDescriptor = descriptor;
        _instanceId = descriptor.InstanceId;
        _hostPort = descriptor.Port;
        _queryStagingDirectory = descriptor.QueryRoot;
        _runtimeConfigurationPath = descriptor.RuntimeConfigPath;
        _runtimeConfigurationDirectory = Path.GetDirectoryName(descriptor.RuntimeConfigPath);
        _sessionToken = token;
        _configFingerprint = descriptor.ConfigFingerprint;
        _apiClient = client;
        _backendProcess = null;
        _connectionMode = BackendConnectionMode.Attached;
        _drainOnly = drainOnly;
        _ownsQueryStagingDirectory = IsManagedQueryStagingDirectory(
            descriptor.QueryRoot
        );
    }

    private BackendApiClient CreateApiClient(
        string token,
        string? submissionDisabledReason) => new(
            new Uri($"http://127.0.0.1:{_hostPort}/", UriKind.Absolute),
            token,
            httpClient: null,
            submissionDisabledReason
        );

    private async Task ConfigureStoredCredentialsAsync(
        BackendApiClient client,
        CancellationToken cancellationToken)
    {
        var apiKey = _credentialService.ReadApiKey();
        if (!string.IsNullOrWhiteSpace(apiKey))
        {
            await client.ConfigureCredentialsAsync(
                apiKey,
                _credentialService.ReadApiUrl(),
                cancellationToken
            );
        }
    }

    private BackendInstanceDescriptor CreateDescriptor(
        string state,
        DateTimeOffset processStartUtc,
        int? wrapperPid,
        DateTimeOffset generatedUtc)
    {
        var configFingerprint = _configFingerprint ?? throw new InvalidOperationException(
            "Backend configuration fingerprint has not been computed."
        );
        var runtimeConfigPath = _runtimeConfigurationPath ?? throw new InvalidOperationException(
            "Backend runtime configuration has not been prepared."
        );
        return new BackendInstanceDescriptor
        {
            InstanceId = InstanceId,
            Host = "127.0.0.1",
            Port = _hostPort,
            WrapperPid = wrapperPid,
            ProcessStartUtc = processStartUtc.ToUniversalTime(),
            ConfigFingerprint = configFingerprint,
            QueryRoot = BackendPathIdentity.NormalizeDirectoryPath(QueryStagingDirectory),
            RuntimeConfigPath = BackendPathIdentity.NormalizeFilePath(
                runtimeConfigPath
            ),
            State = state,
            GeneratedUtc = generatedUtc.ToUniversalTime(),
        };
    }

    private static BackendInstanceDescriptor CopyDescriptor(
        BackendInstanceDescriptor source,
        string state,
        DateTimeOffset generatedUtc)
    {
        return new BackendInstanceDescriptor
        {
            InstanceId = source.InstanceId,
            Host = source.Host,
            Port = source.Port,
            WrapperPid = source.WrapperPid,
            ServerPid = source.ServerPid,
            ProcessStartUtc = source.ProcessStartUtc,
            ConfigFingerprint = source.ConfigFingerprint,
            QueryRoot = source.QueryRoot,
            RuntimeConfigPath = source.RuntimeConfigPath,
            State = state,
            GeneratedUtc = generatedUtc.ToUniversalTime(),
        };
    }

    private static void ValidateInstanceIdentity(
        BackendHealthResponse health,
        BackendInstanceDescriptor descriptor)
    {
        if (!string.Equals(health.InstanceId, descriptor.InstanceId, StringComparison.Ordinal) ||
            !string.Equals(
                health.ConfigFingerprint,
                descriptor.ConfigFingerprint,
                StringComparison.Ordinal
            ))
        {
            throw new BackendProtocolException(
                "后端健康响应与已登记实例不一致，已拒绝连接。"
            );
        }
    }

    private static void ValidateInstanceIdentity(
        BackendVersionResponse version,
        BackendInstanceDescriptor descriptor)
    {
        if (!string.Equals(version.InstanceId, descriptor.InstanceId, StringComparison.Ordinal) ||
            !string.Equals(
                version.ConfigFingerprint,
                descriptor.ConfigFingerprint,
                StringComparison.Ordinal
            ))
        {
            throw new BackendProtocolException(
                "后端版本响应与已登记实例不一致，已拒绝连接。"
            );
        }
    }

    private static bool IsRegisteredProcessAlive(BackendInstanceDescriptor descriptor)
    {
        if ((descriptor.WrapperPid ?? descriptor.ServerPid) is not int processId)
        {
            return string.Equals(
                descriptor.State,
                BackendInstanceStates.Starting,
                StringComparison.Ordinal
            ) && DateTimeOffset.UtcNow - descriptor.GeneratedUtc < TimeSpan.FromMinutes(2);
        }
        try
        {
            using var process = Process.GetProcessById(processId);
            var startedUtc = process.StartTime.ToUniversalTime();
            return Math.Abs((startedUtc - descriptor.ProcessStartUtc).TotalSeconds) < 2;
        }
        catch (Exception exception) when (
            exception is ArgumentException or InvalidOperationException or
                System.ComponentModel.Win32Exception or NotSupportedException
        )
        {
            return false;
        }
    }

    private static bool IsManagedQueryStagingDirectory(string path)
    {
        var managedRoot = BackendPathIdentity.NormalizeDirectoryPath(
            Path.Combine(Path.GetTempPath(), "zvec-image-search", "query-staging")
        );
        var candidate = BackendPathIdentity.NormalizeDirectoryPath(path);
        var comparison = OperatingSystem.IsWindows()
            ? StringComparison.OrdinalIgnoreCase
            : StringComparison.Ordinal;
        return candidate.StartsWith(
            managedRoot + Path.DirectorySeparatorChar,
            comparison
        );
    }

    private async Task DeleteRegistrationUnderLockAsync(
        BackendInstanceDescriptor descriptor,
        string token,
        BackendLaunchLock launchLock,
        CancellationToken cancellationToken)
    {
        await _instanceRegistry.DeleteIfMatchesUnderLockAsync(
            descriptor.InstanceId,
            descriptor.ProcessStartUtc,
            launchLock,
            cancellationToken
        );
        _sessionTokenStore.DeleteIfMatches(token);
    }

    private async Task<bool> WaitForEndpointExitAsync(
        BackendApiClient client,
        CancellationToken cancellationToken)
    {
        var deadline = Stopwatch.StartNew();
        while (deadline.Elapsed < _options.ShutdownTimeout)
        {
            cancellationToken.ThrowIfCancellationRequested();
            try
            {
                using var timeout = new CancellationTokenSource(
                    TimeSpan.FromMilliseconds(500)
                );
                using var linked = CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken,
                    timeout.Token
                );
                await client.GetHealthAsync(linked.Token);
            }
            catch (HttpRequestException) when (!cancellationToken.IsCancellationRequested)
            {
                return true;
            }
            catch (TaskCanceledException) when (!cancellationToken.IsCancellationRequested)
            {
                // A slow response is not proof that the process exited.
            }
            catch (BackendApiException) when (!cancellationToken.IsCancellationRequested)
            {
                // The HTTP endpoint still answered; continue waiting for socket close.
            }
            await Task.Delay(100, cancellationToken);
        }
        return false;
    }

    public async Task<BackendStagedQueryFile> StageQueryFileAsync(
        string sourcePath,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(sourcePath);
        ObjectDisposedException.ThrowIf(_disposed, this);
        if (!IsRunning)
        {
            throw new InvalidOperationException("Start the backend before staging a query file.");
        }

        var originalPath = Path.GetFullPath(sourcePath);
        if (!File.Exists(originalPath))
        {
            throw new FileNotFoundException("Query image was not found.", originalPath);
        }

        var extension = Path.GetExtension(originalPath);
        if (extension.Length > 16 || extension.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
        {
            extension = string.Empty;
        }
        var stagedPath = Path.Combine(
            QueryStagingDirectory,
            $"{Guid.NewGuid():N}{extension.ToLowerInvariant()}"
        );

        try
        {
            await using var source = new FileStream(
                originalPath,
                FileMode.Open,
                FileAccess.Read,
                FileShare.Read,
                bufferSize: 64 * 1024,
                FileOptions.Asynchronous | FileOptions.SequentialScan
            );
            await using var destination = new FileStream(
                stagedPath,
                FileMode.CreateNew,
                FileAccess.Write,
                FileShare.None,
                bufferSize: 64 * 1024,
                FileOptions.Asynchronous
            );
            await source.CopyToAsync(destination, cancellationToken);
        }
        catch
        {
            TryDeleteFile(stagedPath);
            throw;
        }

        // The native backend shares the host filesystem, so no container-path rewrite is
        // needed. Keeping a staged copy still confines image-query access to query_root.
        return new BackendStagedQueryFile(originalPath, stagedPath, stagedPath);
    }

    public async Task StopAsync(CancellationToken cancellationToken = default)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        await _lifecycleGate.WaitAsync(cancellationToken);
        try
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            await StopCoreAsync();
        }
        finally
        {
            _lifecycleGate.Release();
        }
    }

    public async Task DetachAsync(CancellationToken cancellationToken = default)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        await _lifecycleGate.WaitAsync(cancellationToken);
        try
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            DetachCore();
        }
        finally
        {
            _lifecycleGate.Release();
        }
    }

    private async Task StopCoreAsync(bool forceOwnedProcessTermination = false)
    {
        var client = _apiClient;
        var process = _backendProcess;
        var descriptor = _instanceDescriptor;
        var token = _sessionToken;
        var gracefulAccepted = false;

        if (client is not null && SupportsGracefulShutdown)
        {
            try
            {
                var health = await client.GetHealthAsync();
                if (health.HasActiveWork)
                {
                    throw new InvalidOperationException(
                        "后台仍有任务运行；请使用 DetachAsync 让任务继续，或先取消任务。"
                    );
                }
                await client.ShutdownIfIdleAsync();
                gracefulAccepted = true;
            }
            catch (BackendApiException exception) when (
                exception.StatusCode == HttpStatusCode.Conflict
            )
            {
                throw new InvalidOperationException(
                    "后台仍有任务运行，安全关闭请求已被拒绝。",
                    exception
                );
            }
            catch (HttpRequestException exception)
            {
                if (!forceOwnedProcessTermination &&
                    !IsBackendProcessConfirmedExited(process, descriptor))
                {
                    throw new InvalidOperationException(
                        "无法确认常驻后端是否空闲；为避免中断仍在运行的任务，" +
                        "已保留后台进程和实例登记。",
                        exception
                    );
                }
                // Only a positively identified exited process permits registry cleanup.
                // A missing PID is uncertainty, not proof that an adopted backend died.
            }
        }
        else if (client is not null && !forceOwnedProcessTermination)
        {
            throw new InvalidOperationException(
                "当前后端不支持安全关闭；为避免中断未知任务，已保留该实例。"
            );
        }

        if (gracefulAccepted)
        {
            var endpointExited = await WaitForEndpointExitAsync(client!, CancellationToken.None);
            if (!endpointExited && process is null)
            {
                throw new TimeoutException(
                    "已请求后端安全关闭，但服务未在超时时间内退出；实例登记已保留。"
                );
            }
        }

        _apiClient = null;
        _backendProcess = null;
        client?.Dispose();
        if (process is not null)
        {
            try
            {
                if (!process.HasExited)
                {
                    await process.StopAsync(_options.ShutdownTimeout);
                }
            }
            finally
            {
                await process.DisposeAsync();
            }
        }

        if (descriptor is not null)
        {
            await using var launchLock = await _instanceRegistry.AcquireLaunchLockAsync();
            await _instanceRegistry.DeleteIfMatchesUnderLockAsync(
                descriptor.InstanceId,
                descriptor.ProcessStartUtc,
                launchLock
            );
            if (!string.IsNullOrWhiteSpace(token))
            {
                _sessionTokenStore.DeleteIfMatches(token);
            }
        }

        CleanupRuntimeDirectories();
        ResetRuntimeState();
    }

    private void DetachCore()
    {
        _apiClient?.Dispose();
        _apiClient = null;
        // Do not dispose the owned process wrapper here. The desktop process is about
        // to exit and the registered backend must remain alive for job recovery.
        _backendProcess = null;
        ResetRuntimeState();
    }

    private static bool IsBackendProcessConfirmedExited(
        IBackendProcess? ownedProcess,
        BackendInstanceDescriptor? descriptor)
    {
        if (ownedProcess is not null)
        {
            return ownedProcess.HasExited;
        }
        if (descriptor is null ||
            descriptor.WrapperPid is null && descriptor.ServerPid is null)
        {
            return false;
        }
        return !IsRegisteredProcessAlive(descriptor);
    }

    private void PrepareRuntimeDirectories()
    {
        _ownsQueryStagingDirectory = string.IsNullOrWhiteSpace(
            _options.QueryStagingDirectory
        );
        _queryStagingDirectory = Path.GetFullPath(
            _options.QueryStagingDirectory ?? Path.Combine(
                Path.GetTempPath(),
                "zvec-image-search",
                "query-staging",
                _instanceId!
            )
        );
        if (_ownsQueryStagingDirectory)
        {
            RecreateOwnedDirectory(_queryStagingDirectory);
        }
        else
        {
            Directory.CreateDirectory(_queryStagingDirectory);
        }

        _runtimeConfigurationDirectory = Path.Combine(
            Path.GetTempPath(),
            "zvec-image-search",
            "runtime-config",
            _instanceId!
        );
        RecreateOwnedDirectory(_runtimeConfigurationDirectory);
        _runtimeConfigurationPath = Path.Combine(
            _runtimeConfigurationDirectory,
            "libraries.json"
        );
    }

    private async Task WriteRuntimeConfigurationAsync(
        LauncherConfig config,
        CancellationToken cancellationToken)
    {
        var resultsDirectory = GetOrCreateDirectory(
            config.ResultsDirectory,
            nameof(config.ResultsDirectory)
        );
        var libraries = new List<object>();
        foreach (var library in config.EnabledLibraries)
        {
            libraries.Add(new
            {
                id = library.Id,
                name = library.Name,
                image_root = GetExistingDirectory(
                    library.ImageRoot,
                    $"library {library.Id} image_root"
                ),
                workspace_directory = GetOrCreateDirectory(
                    library.WorkspaceDirectory,
                    $"library {library.Id} workspace_directory"
                ),
                enabled = true,
            });
        }
        var runtimeConfig = new
        {
            schema_version = LauncherConfig.CurrentSchemaVersion,
            default_library_id = config.DefaultLibraryId,
            results_directory = resultsDirectory,
            libraries,
        };
        await using var stream = new FileStream(
            _runtimeConfigurationPath!,
            FileMode.CreateNew,
            FileAccess.Write,
            FileShare.None,
            bufferSize: 16 * 1024,
            FileOptions.Asynchronous | FileOptions.WriteThrough
        );
        await JsonSerializer.SerializeAsync(
            stream,
            runtimeConfig,
            new JsonSerializerOptions { WriteIndented = true },
            cancellationToken
        );
        await stream.FlushAsync(cancellationToken);
    }

    private BackendLaunchCommand ResolveLaunchCommand(
        LauncherConfig config,
        string fullEntryPoint)
    {
        var applicationDirectory = Path.GetFullPath(
            _options.ApplicationDirectory ?? AppContext.BaseDirectory
        );
        var python = FirstNonBlank(
            _preparedPythonExecutable,
            _options.PythonExecutable,
            config.PythonExecutable,
            Environment.GetEnvironmentVariable("ZVEC_PYTHON_EXECUTABLE")
        ) ?? FindBundledPython(applicationDirectory) ??
            (OperatingSystem.IsWindows() ? "python.exe" : "python3");
        python = ResolveExecutableValue(python, applicationDirectory);
        ValidatePythonArchitecture(python);

        return new BackendLaunchCommand(
            python,
            fullEntryPoint,
            Path.GetDirectoryName(fullEntryPoint) ?? applicationDirectory
        );
    }

    private string ResolveBackendEntryPoint()
    {
        var applicationDirectory = Path.GetFullPath(
            _options.ApplicationDirectory ?? AppContext.BaseDirectory
        );
        var entryPoint = FirstNonBlank(
            _options.BackendEntryPointPath,
            Environment.GetEnvironmentVariable("ZVEC_BACKEND_ENTRYPOINT")
        ) ?? FindBackendEntryPoint(applicationDirectory);
        if (string.IsNullOrWhiteSpace(entryPoint))
        {
            throw new FileNotFoundException(
                "找不到原生后端入口 image_service.py。请安装包含 backend 目录的完整桌面包，" +
                "或设置 ZVEC_BACKEND_ENTRYPOINT。"
            );
        }
        var fullEntryPoint = Path.GetFullPath(
            Environment.ExpandEnvironmentVariables(entryPoint),
            applicationDirectory
        );
        if (!File.Exists(fullEntryPoint))
        {
            throw new FileNotFoundException("原生后端入口不存在。", fullEntryPoint);
        }
        return fullEntryPoint;
    }

    private async Task<string?> PrepareNativeRuntimeAsync(
        LauncherConfig config,
        CancellationToken cancellationToken)
    {
        if (!_options.AutoPrepareRuntime)
        {
            return null;
        }

        var applicationDirectory = Path.GetFullPath(
            _options.ApplicationDirectory ?? AppContext.BaseDirectory
        );
        var existingIsolatedPython = FindBundledPython(applicationDirectory);
        var bootstrapPython = FirstNonBlank(
            _options.PythonExecutable,
            Environment.GetEnvironmentVariable("ZVEC_PYTHON_EXECUTABLE"),
            Environment.GetEnvironmentVariable("ZVEC_PYTHON")
        );
        if (string.IsNullOrWhiteSpace(bootstrapPython) &&
            string.IsNullOrWhiteSpace(existingIsolatedPython))
        {
            // A configured base interpreter is needed only for the first venv creation.
            // Once the isolated runtime exists, a later system-Python uninstall must not
            // prevent diagnosis, repair, or normal backend startup.
            bootstrapPython = config.PythonExecutable;
        }
        RuntimeBootstrapService? ownedBootstrapper = null;
        var bootstrapper = _runtimeBootstrapper;
        if (bootstrapper is null)
        {
            var repositoryRoot = RepositoryLocator.FindRepositoryRoot(applicationDirectory);
            ownedBootstrapper = new RuntimeBootstrapService(repositoryRoot);
            bootstrapper = ownedBootstrapper;
        }

        try
        {
            var result = await bootstrapper.EnsureReadyAsync(
                bootstrapPython,
                cancellationToken
            );
            if (!result.IsSuccess)
            {
                cancellationToken.ThrowIfCancellationRequested();
                throw new RuntimeBootstrapException(result);
            }
            if (string.IsNullOrWhiteSpace(result.PythonExecutable))
            {
                throw new RuntimeBootstrapException(result with
                {
                    Success = false,
                    Status = "failed",
                    Code = "bootstrap_python_missing",
                    Message = "运行环境准备完成，但没有返回隔离 Python 路径。",
                    RecommendedAction = "重新执行运行环境修复；若仍失败，请重新安装完整桌面包。",
                });
            }
            return result.PythonExecutable;
        }
        finally
        {
            ownedBootstrapper?.Dispose();
        }
    }

    private ProcessStartInfo BuildProcessStartInfo(
        BackendLaunchCommand launch,
        string sessionToken,
        string configFingerprint)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = launch.PythonExecutable,
            WorkingDirectory = launch.WorkingDirectory,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (var argument in new[]
        {
            launch.EntryPointPath,
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            _hostPort.ToString(CultureInfo.InvariantCulture),
            "--query-root",
            QueryStagingDirectory,
            "--libraries-config",
            _runtimeConfigurationPath!,
            "--instance-id",
            InstanceId,
            "--config-fingerprint",
            configFingerprint,
            "--instance-lock-path",
            Path.Combine(_instanceRegistry.BackendDirectory, "backend.lock"),
        })
        {
            startInfo.ArgumentList.Add(argument);
        }
        startInfo.Environment["PYTHONUTF8"] = "1";
        startInfo.Environment["PYTHONIOENCODING"] = "utf-8";
        startInfo.Environment["PYTHONUNBUFFERED"] = "1";
        startInfo.Environment["ZVEC_BACKEND_TOKEN"] = sessionToken;
        startInfo.Environment["ZVEC_MODELS_CONFIG"] =
            _modelsConfigurationPath ?? ModelConfigurationService.DefaultConfigurationPath;
        // Model credentials are injected through the authenticated HTTP session only.
        startInfo.Environment.Remove("DASHSCOPE_API_KEY");
        return startInfo;
    }

    private async Task<BackendVersionResponse> ValidateBackendVersionAsync(
        BackendApiClient client,
        CancellationToken cancellationToken)
    {
        var version = await client.GetVersionAsync(cancellationToken);
        SupportsLowConfidenceOverride = version.Capabilities.LowConfidenceOverride;
        SupportsTagOnlySearch = version.Capabilities.TagOnlySearch;
        SupportsHybridTagVectorSearch = version.Capabilities.HybridTagVectorSearch;
        SupportsResultDiversity = version.Capabilities.ResultDiversity;
        SupportsIndexAndAutoTag = version.Capabilities.IndexAndAutoTag;
        SupportsMetadataEmbeddingSearch =
            version.Capabilities.MetadataEmbeddingSearch;
        SupportsMetadataEmbeddingBackfill =
            version.Capabilities.MetadataEmbeddingBackfill;
        SupportsPersistentBackendSession = version.Capabilities.PersistentBackendSession;
        SupportsGracefulShutdown = version.Capabilities.GracefulShutdown;
        if (version.ProtocolVersion != _options.RequiredProtocolVersion)
        {
            throw new BackendProtocolException(
                $"Backend protocol {version.ProtocolVersion} is incompatible; " +
                $"protocol {_options.RequiredProtocolVersion} is required. " +
                "请更新桌面包中的原生 Python 后端。"
            );
        }
        if (!version.Capabilities.MultiLibrary ||
            !version.Capabilities.FederatedSearch ||
            !version.Capabilities.SessionCredentials)
        {
            throw new BackendProtocolException(
                "当前原生 Python 后端缺少多图库、跨图库检索或会话凭据能力。"
            );
        }
        return version;
    }

    private async Task WaitUntilHealthyAsync(
        BackendApiClient client,
        CancellationToken cancellationToken)
    {
        var stopwatch = Stopwatch.StartNew();
        BackendHealthResponse? lastHealth = null;
        Exception? lastException = null;

        while (stopwatch.Elapsed < _options.StartupTimeout)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (_connectionMode == BackendConnectionMode.Spawned &&
                (_backendProcess is null || _backendProcess.HasExited))
            {
                throw CreateExitedProcessException(lastException);
            }
            try
            {
                using var healthTimeout = CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken
                );
                healthTimeout.CancelAfter(_options.HealthRequestTimeout);
                lastHealth = await client.GetHealthAsync(healthTimeout.Token);
                lastException = null;
                if (lastHealth.IsReady)
                {
                    return;
                }
                if (string.Equals(
                    lastHealth.Error?.Code,
                    "library_initialization_failed",
                    StringComparison.Ordinal
                ))
                {
                    throw new BackendProtocolException(
                        $"后端图库初始化失败：{FormatHealthDescription(lastHealth)}"
                    );
                }
            }
            catch (Exception exception) when (
                exception is HttpRequestException or TaskCanceledException or BackendApiException &&
                !cancellationToken.IsCancellationRequested
            )
            {
                lastException = exception;
                if (_connectionMode == BackendConnectionMode.Spawned &&
                    (_backendProcess is null || _backendProcess.HasExited))
                {
                    throw CreateExitedProcessException(exception);
                }
            }

            var remaining = _options.StartupTimeout - stopwatch.Elapsed;
            if (remaining <= TimeSpan.Zero)
            {
                break;
            }
            await Task.Delay(
                remaining < _options.HealthPollInterval
                    ? remaining
                    : _options.HealthPollInterval,
                cancellationToken
            );
        }

        var healthDescription = lastHealth is null
            ? lastException?.Message ?? "no health response"
            : FormatHealthDescription(lastHealth);
        throw new TimeoutException(
            $"Zvec 原生后端在 {_options.StartupTimeout.TotalSeconds:N0} 秒内未就绪 " +
            $"({healthDescription}).{FormatProcessOutput()}"
        );
    }

    internal static string FormatHealthDescription(BackendHealthResponse health)
    {
        ArgumentNullException.ThrowIfNull(health);
        var description =
            $"status={health.Status}, service_ready={health.ServiceReady}, " +
            $"worker_alive={health.WorkerAlive}";
        if (health.Error is null)
        {
            return description;
        }

        var errorParts = new List<string>();
        if (!string.IsNullOrWhiteSpace(health.Error.Code))
        {
            errorParts.Add($"error.code={NormalizeDiagnosticText(health.Error.Code)}");
        }
        if (!string.IsNullOrWhiteSpace(health.Error.Message))
        {
            errorParts.Add($"error.message={NormalizeDiagnosticText(health.Error.Message)}");
        }
        if (health.Error.Details is JsonElement details &&
            details.ValueKind is not JsonValueKind.Null and not JsonValueKind.Undefined)
        {
            errorParts.Add($"error.details={FormatHealthErrorDetails(details)}");
        }
        return errorParts.Count == 0
            ? description
            : $"{description}, {string.Join(", ", errorParts)}";
    }

    private static string FormatHealthErrorDetails(JsonElement details)
    {
        if (details.ValueKind != JsonValueKind.Object)
        {
            return JsonSerializer.Serialize(details);
        }

        var libraryErrors = details.EnumerateObject()
            .Select(FormatLibraryStartupError)
            .ToArray();
        return libraryErrors.Length == 0
            ? "{}"
            : string.Join("; ", libraryErrors);
    }

    private static string FormatLibraryStartupError(JsonProperty libraryError)
    {
        if (libraryError.Value.ValueKind != JsonValueKind.Object)
        {
            return $"library[{libraryError.Name}]={JsonSerializer.Serialize(libraryError.Value)}";
        }

        var parts = new List<string>();
        if (libraryError.Value.TryGetProperty("code", out var code) &&
            code.ValueKind == JsonValueKind.String)
        {
            parts.Add($"code={NormalizeDiagnosticText(code.GetString() ?? string.Empty)}");
        }
        if (libraryError.Value.TryGetProperty("message", out var message) &&
            message.ValueKind == JsonValueKind.String)
        {
            parts.Add($"message={NormalizeDiagnosticText(message.GetString() ?? string.Empty)}");
        }
        if (libraryError.Value.TryGetProperty("details", out var nestedDetails) &&
            nestedDetails.ValueKind is not JsonValueKind.Null and not JsonValueKind.Undefined)
        {
            parts.Add($"details={JsonSerializer.Serialize(nestedDetails)}");
        }
        if (parts.Count == 0)
        {
            parts.Add(JsonSerializer.Serialize(libraryError.Value));
        }
        return $"library[{libraryError.Name}]({string.Join(", ", parts)})";
    }

    private static string NormalizeDiagnosticText(string value) =>
        string.Join(" ", value.Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries))
            .Trim();

    private InvalidOperationException CreateExitedProcessException(Exception? inner)
    {
        var exitCode = _backendProcess?.ExitCode;
        var message = "Zvec 原生 Python 后端在完成启动前退出" +
            (exitCode.HasValue ? $"（退出码 {exitCode.Value}）" : string.Empty) +
            $"。{FormatProcessOutput()}";
        return new InvalidOperationException(message, inner);
    }

    private string FormatProcessOutput()
    {
        var output = _backendProcess?.RecentOutput;
        return string.IsNullOrWhiteSpace(output)
            ? string.Empty
            : $"{Environment.NewLine}后端输出：{Environment.NewLine}{output}";
    }

    private static BackendHostOptions ValidateOptions(BackendHostOptions options)
    {
        if (options.StartupTimeout <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(options.StartupTimeout));
        }
        if (options.HealthPollInterval <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(options.HealthPollInterval));
        }
        if (options.HealthRequestTimeout <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(options.HealthRequestTimeout));
        }
        if (options.ShutdownTimeout <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(options.ShutdownTimeout));
        }
        if (options.RequiredProtocolVersion < 1)
        {
            throw new ArgumentOutOfRangeException(nameof(options.RequiredProtocolVersion));
        }
        return options;
    }

    private static void ValidateLauncherConfig(LauncherConfig config)
    {
        if (config.SchemaVersion != LauncherConfig.CurrentSchemaVersion)
        {
            throw new InvalidDataException(
                $"Unsupported launcher configuration schema: {config.SchemaVersion}."
            );
        }
        ArgumentException.ThrowIfNullOrWhiteSpace(config.ResultsDirectory);
        if (config.Libraries.Count == 0 || config.DefaultLibrary is null)
        {
            throw new InvalidDataException("At least one configured library is required.");
        }
        if (config.EnabledLibraries.Count == 0 || config.DefaultLibrary.Enabled != true)
        {
            throw new InvalidDataException(
                "At least one enabled library is required and the default library must be enabled."
            );
        }
        foreach (var library in config.Libraries)
        {
            ArgumentException.ThrowIfNullOrWhiteSpace(library.Id);
            ArgumentException.ThrowIfNullOrWhiteSpace(library.Name);
            ArgumentException.ThrowIfNullOrWhiteSpace(library.ImageRoot);
            ArgumentException.ThrowIfNullOrWhiteSpace(library.WorkspaceDirectory);
            if (library.HasLegacyNamedVolume)
            {
                throw new InvalidDataException(
                    $"Library '{library.Id}' still references a Docker named volume. " +
                    "Export it to a host workspace_directory before starting the native backend."
                );
            }
        }
    }

    private static string? FindBundledPython(string applicationDirectory)
    {
        var configHome = ResolveConfigHome();
        var candidates = OperatingSystem.IsWindows()
            ? new[]
            {
                Path.Combine(configHome, "runtime", "venv", "Scripts", "python.exe"),
                Path.Combine(applicationDirectory, "runtime", "python", "python.exe"),
                Path.Combine(applicationDirectory, "python", "python.exe"),
                Path.Combine(applicationDirectory, ".venv", "Scripts", "python.exe"),
                Path.Combine(applicationDirectory, "venv", "Scripts", "python.exe"),
            }
            : new[]
            {
                Path.Combine(configHome, "runtime", "venv", "bin", "python"),
                Path.Combine(applicationDirectory, "runtime", "python", "bin", "python3"),
                Path.Combine(applicationDirectory, "python", "bin", "python3"),
                Path.Combine(applicationDirectory, ".venv", "bin", "python"),
                Path.Combine(applicationDirectory, "venv", "bin", "python"),
            };
        return candidates.FirstOrDefault(File.Exists);
    }

    private static string ResolveConfigHome()
    {
        var configured = FirstNonBlank(
            Environment.GetEnvironmentVariable("ZVEC_CONFIG_HOME"),
            Environment.GetEnvironmentVariable("ZVEC_DOCKER_CONFIG_HOME")
        );
        if (!string.IsNullOrWhiteSpace(configured))
        {
            return Path.GetFullPath(
                Environment.ExpandEnvironmentVariables(configured)
            );
        }
        return Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "zvec-image-search"
        );
    }

    private static void ValidatePythonArchitecture(string pythonExecutable)
    {
        if (!OperatingSystem.IsWindows() ||
            RuntimeInformation.OSArchitecture != Architecture.Arm64 ||
            !Path.IsPathRooted(pythonExecutable) ||
            !File.Exists(pythonExecutable))
        {
            return;
        }

        const ushort arm64Machine = 0xaa64;
        try
        {
            using var stream = File.OpenRead(pythonExecutable);
            using var reader = new BinaryReader(stream);
            if (stream.Length < 64 || reader.ReadUInt16() != 0x5a4d)
            {
                return;
            }
            stream.Position = 0x3c;
            var peOffset = reader.ReadInt32();
            if (peOffset < 0 || peOffset + 6 > stream.Length)
            {
                return;
            }
            stream.Position = peOffset;
            if (reader.ReadUInt32() != 0x00004550)
            {
                return;
            }
            if (reader.ReadUInt16() == arm64Machine)
            {
                throw new InvalidOperationException(
                    "Windows ARM64 上的 zvec 0.5.1 暂无 ARM64 Python wheel。" +
                    "请安装 x64 CPython，并在“Python 解释器”中选择其 python.exe；" +
                    "Windows ARM64 会通过 x64 仿真运行后端。"
                );
            }
        }
        catch (IOException)
        {
            // The interpreter will produce the authoritative startup error if its PE
            // header cannot be inspected because another process temporarily holds it.
        }
        catch (UnauthorizedAccessException)
        {
            // Same fallback as above; do not reject an otherwise launchable interpreter.
        }
    }

    private static string? FindBackendEntryPoint(string applicationDirectory)
    {
        foreach (var candidate in new[]
        {
            Path.Combine(applicationDirectory, "backend", "image_service.py"),
            Path.Combine(applicationDirectory, "image_service.py"),
        })
        {
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }

        foreach (var start in new[] { applicationDirectory, Environment.CurrentDirectory })
        {
            var directory = new DirectoryInfo(start);
            while (directory is not null)
            {
                var candidate = Path.Combine(directory.FullName, "image_service.py");
                if (File.Exists(candidate))
                {
                    return candidate;
                }
                directory = directory.Parent;
            }
        }
        return null;
    }

    private static string ResolveExecutableValue(string value, string baseDirectory)
    {
        var expanded = Environment.ExpandEnvironmentVariables(value.Trim());
        if (!Path.IsPathRooted(expanded) &&
            (expanded.Contains(Path.DirectorySeparatorChar) ||
                expanded.Contains(Path.AltDirectorySeparatorChar)))
        {
            expanded = Path.GetFullPath(expanded, baseDirectory);
        }
        if (Path.IsPathRooted(expanded) && !File.Exists(expanded))
        {
            throw new FileNotFoundException("配置的 Python 解释器不存在。", expanded);
        }
        return expanded;
    }

    private static string? FirstNonBlank(params string?[] values) => values.FirstOrDefault(
        value => !string.IsNullOrWhiteSpace(value)
    );

    private static string GetExistingDirectory(string path, string parameterName)
    {
        var fullPath = Path.GetFullPath(Environment.ExpandEnvironmentVariables(path));
        if (!Directory.Exists(fullPath))
        {
            throw new DirectoryNotFoundException(
                $"Directory configured by {parameterName} does not exist: {fullPath}"
            );
        }
        return fullPath;
    }

    private static string GetOrCreateDirectory(string path, string parameterName)
    {
        try
        {
            return Directory.CreateDirectory(
                Path.GetFullPath(Environment.ExpandEnvironmentVariables(path))
            ).FullName;
        }
        catch (Exception exception) when (
            exception is ArgumentException or NotSupportedException or IOException or
                UnauthorizedAccessException
        )
        {
            throw new InvalidDataException(
                $"Could not create directory configured by {parameterName}: {path}",
                exception
            );
        }
    }

    private static void RecreateOwnedDirectory(string path)
    {
        var fullPath = Path.GetFullPath(path);
        var ownedRoot = Path.GetFullPath(
            Path.Combine(Path.GetTempPath(), "zvec-image-search")
        ).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        if (!fullPath.StartsWith(ownedRoot, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException(
                $"Refusing to recreate a directory outside the Zvec temporary root: {fullPath}"
            );
        }
        if (Directory.Exists(fullPath))
        {
            Directory.Delete(fullPath, recursive: true);
        }
        Directory.CreateDirectory(fullPath);
    }

    private static int ReserveEphemeralLoopbackPort()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        try
        {
            listener.Start();
            return ((IPEndPoint)listener.LocalEndpoint).Port;
        }
        finally
        {
            listener.Stop();
        }
    }

    private static string CreateSessionToken() => Convert.ToHexString(
        RandomNumberGenerator.GetBytes(32)
    ).ToLowerInvariant();

    private void CleanupRuntimeDirectories()
    {
        if (_ownsQueryStagingDirectory)
        {
            TryDeleteDirectory(_queryStagingDirectory);
        }
        TryDeleteDirectory(_runtimeConfigurationDirectory);
    }

    private void ResetRuntimeState()
    {
        _instanceId = null;
        _queryStagingDirectory = null;
        _runtimeConfigurationDirectory = null;
        _runtimeConfigurationPath = null;
        _preparedPythonExecutable = null;
        _sessionToken = null;
        _configFingerprint = null;
        _instanceDescriptor = null;
        _connectionMode = BackendConnectionMode.None;
        _drainOnly = false;
        _ownsQueryStagingDirectory = false;
        _hostPort = 0;
        _activeLauncherConfig = null;
        SupportsLowConfidenceOverride = false;
        SupportsTagOnlySearch = false;
        SupportsHybridTagVectorSearch = false;
        SupportsResultDiversity = false;
        SupportsIndexAndAutoTag = false;
        SupportsPersistentBackendSession = false;
        SupportsGracefulShutdown = false;
    }

    private static void TryDeleteDirectory(string? path)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            return;
        }
        try
        {
            if (Directory.Exists(path))
            {
                Directory.Delete(path, recursive: true);
            }
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException
        )
        {
            // A scanner may briefly hold a staged file; the OS can reclaim temp data later.
        }
    }

    private static void TryDeleteFile(string path)
    {
        try
        {
            File.Delete(path);
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException
        )
        {
            // Preserve the original staging exception.
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }
        await _lifecycleGate.WaitAsync();
        try
        {
            if (_disposed)
            {
                return;
            }
            if (_connectionMode == BackendConnectionMode.Attached)
            {
                DetachCore();
            }
            else
            {
                await StopCoreAsync();
            }
            _disposed = true;
        }
        finally
        {
            _lifecycleGate.Release();
        }
        GC.SuppressFinalize(this);
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        Task.Run(async () => await DisposeAsync()).GetAwaiter().GetResult();
    }

    private sealed record BackendLaunchCommand(
        string PythonExecutable,
        string EntryPointPath,
        string WorkingDirectory
    );
}
