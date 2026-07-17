using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using Zvec.Desktop.Models;
using Zvec.Desktop.Services;

internal static class PersistentBackendContract
{
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web)
    {
        WriteIndented = true,
    };

    public static async Task RunAsync()
    {
        var originalConfigHome = Environment.GetEnvironmentVariable("ZVEC_CONFIG_HOME");
        var root = Path.Combine(
            Path.GetTempPath(),
            $"zvec-persistent-backend-contract-{Guid.NewGuid():N}"
        );
        var configHome = Directory.CreateDirectory(Path.Combine(root, "config-home")).FullName;
        var registry = new BackendInstanceRegistry(configHome);
        var tokenStore = new BackendSessionTokenStore(configHome);
        var credentialManager = new CredentialManagerService(
            $"Zvec.ImageSearch/PersistentBackendContract/{Guid.NewGuid():N}"
        );
        var instanceId = $"contract-backend-{Guid.NewGuid():N}";
        var sessionToken = Convert.ToHexString(
            System.Security.Cryptography.RandomNumberGenerator.GetBytes(32)
        ).ToLowerInvariant();

        try
        {
            Environment.SetEnvironmentVariable("ZVEC_CONFIG_HOME", configHome);
            credentialManager.DeleteApiKey();

            var images = Directory.CreateDirectory(Path.Combine(root, "images")).FullName;
            var workspace = Directory.CreateDirectory(Path.Combine(root, "workspace")).FullName;
            var results = Directory.CreateDirectory(Path.Combine(root, "results")).FullName;
            var entryPointDirectory = Directory.CreateDirectory(
                Path.Combine(root, "entrypoint")
            ).FullName;
            var entryPoint = await BackendPayloadContractFixture.CreateAsync(
                entryPointDirectory,
                "persistent-backend"
            );
            var queryRoot = Directory.CreateDirectory(Path.Combine(root, "query")).FullName;
            var runtimeDirectory = Directory.CreateDirectory(
                Path.Combine(root, "runtime")
            ).FullName;
            var runtimeConfigPath = Path.Combine(runtimeDirectory, "libraries.json");
            await File.WriteAllTextAsync(runtimeConfigPath, "{\"schema_version\":3}\n");

            var config = new LauncherConfig
            {
                SchemaVersion = LauncherConfig.CurrentSchemaVersion,
                PythonExecutable = Path.Combine(root, "must-not-spawn-python.exe"),
                ResultsDirectory = results,
                DefaultLibraryId = "contract-library",
                Libraries =
                [
                    new LauncherLibrary
                    {
                        Id = "contract-library",
                        Name = "Persistent backend contract",
                        ImageRoot = images,
                        WorkspaceDirectory = workspace,
                        Enabled = true,
                    },
                ],
            };
            await File.WriteAllTextAsync(
                Path.Combine(configHome, "config.json"),
                JsonSerializer.Serialize(config, JsonOptions)
            );
            var fingerprint = await BackendConfigurationFingerprint.ComputeAsync(
                config,
                entryPoint
            );

            await using var backend = await LoopbackPersistentBackend.StartAsync(
                instanceId,
                fingerprint,
                sessionToken
            );
            var registeredAt = DateTimeOffset.UtcNow;
            using var currentProcess = System.Diagnostics.Process.GetCurrentProcess();
            var processStartUtc = new DateTimeOffset(
                currentProcess.StartTime.ToUniversalTime(),
                TimeSpan.Zero
            );
            var descriptor = new BackendInstanceDescriptor
            {
                InstanceId = instanceId,
                Host = "127.0.0.1",
                Port = backend.Port,
                WrapperPid = currentProcess.Id,
                ProcessStartUtc = processStartUtc,
                ConfigFingerprint = fingerprint,
                QueryRoot = Path.GetFullPath(queryRoot),
                RuntimeConfigPath = Path.GetFullPath(runtimeConfigPath),
                State = BackendInstanceStates.Ready,
                GeneratedUtc = registeredAt,
            };
            await registry.WriteAsync(descriptor);
            tokenStore.SaveToken(sessionToken);
            AssertTokenIsAbsentFromDescriptors(registry.BackendDirectory, sessionToken);
            Require(
                await backend.IsHealthyAsync(),
                "The loopback backend was not healthy before the first attach."
            );
            var hostConfigService = new LauncherConfigService();
            Require(
                Path.GetFullPath(hostConfigService.ConfigHome) == Path.GetFullPath(configHome),
                "The desktop host resolved a different temporary config home."
            );
            var hostVisibleDescriptor = await new BackendInstanceRegistry(
                hostConfigService
            ).ReadAsync();
            Require(
                hostVisibleDescriptor?.InstanceId == instanceId,
                "The desktop host cannot read the prepared instance descriptor."
            );
            Require(
                new BackendSessionTokenStore(hostConfigService.ConfigHome).ReadToken() ==
                    sessionToken,
                "The desktop host cannot read the prepared session token."
            );
            Require(
                await BackendConfigurationFingerprint.ComputeAsync(config, entryPoint) ==
                    fingerprint,
                "The prepared backend configuration fingerprint is not deterministic."
            );

            var credentialService = new ApiCredentialService(
                credentialManager,
                hostConfigService
            );
            var options = new BackendHostOptions
            {
                AutoPrepareRuntime = false,
                PythonExecutable = config.PythonExecutable,
                BackendEntryPointPath = entryPoint,
                ApplicationDirectory = root,
                StartupTimeout = TimeSpan.FromSeconds(3),
                HealthPollInterval = TimeSpan.FromMilliseconds(25),
                HealthRequestTimeout = TimeSpan.FromMilliseconds(250),
                ShutdownTimeout = TimeSpan.FromSeconds(3),
            };

            var changedConfig = new LauncherConfig
            {
                SchemaVersion = LauncherConfig.CurrentSchemaVersion,
                PythonExecutable = config.PythonExecutable,
                ResultsDirectory = results,
                DefaultLibraryId = "contract-library",
                Libraries =
                [
                    new LauncherLibrary
                    {
                        Id = "contract-library",
                        Name = "Changed persistent backend contract",
                        ImageRoot = images,
                        WorkspaceDirectory = workspace,
                        Enabled = true,
                    },
                ],
            };
            backend.SetActivity(runningJobs: 0, queueDepth: 1);
            await using (var drainingHost = new BackendHostService(
                changedConfig,
                options: options,
                credentialService: credentialService
            ))
            {
                await drainingHost.StartAsync();
                Require(
                    drainingHost.ConnectionMode == BackendConnectionMode.Attached &&
                        drainingHost.IsDrainOnly,
                    "A queued old-configuration job must attach in drain-only mode."
                );
                Require(
                    backend.ShutdownRequestCount == 0,
                    "A queued job must not trigger an idle-shutdown request."
                );
                await drainingHost.DetachAsync();
            }
            backend.SetActivity(runningJobs: 0, queueDepth: 0);

            await using (var firstHost = new BackendHostService(
                config,
                options: options,
                credentialService: credentialService
            ))
            {
                var firstClient = await firstHost.StartAsync();
                Require(firstHost.IsRunning, "The first desktop host did not attach.");
                Require(
                    firstHost.ConnectionMode == BackendConnectionMode.Attached,
                    "The first desktop host spawned instead of attaching."
                );
                Require(
                    firstHost.InstanceId == instanceId,
                    "The first desktop host attached to the wrong instance."
                );
                Require(
                    firstHost.QueryStagingDirectory == descriptor.QueryRoot,
                    "The attached host did not reuse the registered query directory."
                );
                Require(
                    firstHost.SupportsPersistentBackendSession &&
                        firstHost.SupportsGracefulShutdown,
                    "The attached backend capabilities were not retained."
                );
                var jobs = await firstClient.GetJobsAsync(active: true, limit: 20);
                Require(jobs.Count == 0 && jobs.Jobs.Count == 0, "Job recovery response is invalid.");

                await firstHost.DetachAsync();
                Require(!firstHost.IsRunning, "Detach left the first host connected.");
            }

            Require(await backend.IsHealthyAsync(), "Detach terminated the persistent endpoint.");
            var retainedDescriptor = await registry.ReadAsync();
            Require(
                retainedDescriptor?.InstanceId == instanceId,
                "Detach removed or replaced the persistent instance descriptor."
            );
            Require(
                tokenStore.ReadToken() == sessionToken,
                "Detach removed or changed the persistent session token."
            );
            AssertTokenIsAbsentFromDescriptors(registry.BackendDirectory, sessionToken);

            await using (var secondHost = new BackendHostService(
                config,
                options: options,
                credentialService: credentialService
            ))
            {
                await secondHost.StartAsync();
                Require(
                    secondHost.ConnectionMode == BackendConnectionMode.Attached,
                    "The second desktop host spawned instead of reconnecting."
                );
                Require(
                    secondHost.InstanceId == instanceId && secondHost.HostPort == backend.Port,
                    "The second desktop host did not reconnect to the original endpoint."
                );
                backend.FailNextHealthRequest();
                await RequireThrowsAsync<InvalidOperationException>(
                    async () => await secondHost.StopAsync()
                );
                Require(
                    secondHost.IsRunning,
                    "A transient health failure disconnected from a live attached backend."
                );
                Require(
                    (await registry.ReadAsync())?.InstanceId == instanceId &&
                        tokenStore.ReadToken() == sessionToken,
                    "A transient health failure removed live backend recovery state."
                );
                Require(
                    backend.ShutdownRequestCount == 0,
                    "A transient health failure sent an unverified shutdown request."
                );
                backend.SetActivity(runningJobs: 1, queueDepth: 0);
                await RequireThrowsAsync<InvalidOperationException>(
                    async () => await secondHost.StopAsync()
                );
                Require(
                    secondHost.IsRunning,
                    "StopAsync disconnected from a backend with an active job."
                );
                Require(
                    (await registry.ReadAsync())?.InstanceId == instanceId,
                    "StopAsync removed the descriptor for an active backend."
                );
                Require(
                    tokenStore.ReadToken() == sessionToken,
                    "StopAsync removed the token for an active backend."
                );
                Require(
                    backend.ShutdownRequestCount == 0,
                    "StopAsync sent a shutdown request while a job was active."
                );
                backend.SetActivity(runningJobs: 0, queueDepth: 0);
                await secondHost.StopAsync();
                Require(!secondHost.IsRunning, "Graceful stop left the second host connected.");
            }

            await backend.WaitForShutdownAsync(TimeSpan.FromSeconds(3));
            Require(backend.ShutdownRequestCount == 1, "Graceful shutdown was not requested once.");
            Require(!await backend.IsHealthyAsync(), "The loopback endpoint survived graceful stop.");
            Require(await registry.ReadAsync() is null, "Graceful stop retained instance.json.");
            Require(!tokenStore.HasToken(), "Graceful stop retained the backend session token.");
            Require(backend.InvalidAuthorizationCount == 0, "A request used the wrong session token.");
            Require(backend.JobListRequestCount > 0, "The persistent job list was not queried.");
        }
        finally
        {
            try
            {
                await registry.DeleteIfMatchesAsync(instanceId);
            }
            catch (Exception exception) when (
                exception is IOException or UnauthorizedAccessException
            )
            {
                // Preserve the original contract failure; the temporary root is also
                // removed below on a best-effort basis.
            }
            tokenStore.DeleteToken();
            credentialManager.DeleteApiKey();
            Environment.SetEnvironmentVariable("ZVEC_CONFIG_HOME", originalConfigHome);
            TryDeleteDirectory(root);
        }
    }

    private static void AssertTokenIsAbsentFromDescriptors(
        string backendDirectory,
        string sessionToken)
    {
        if (!Directory.Exists(backendDirectory))
        {
            return;
        }
        foreach (var path in Directory.EnumerateFiles(
            backendDirectory,
            "*.json",
            SearchOption.AllDirectories
        ))
        {
            var descriptorText = File.ReadAllText(path);
            Require(
                !descriptorText.Contains(sessionToken, StringComparison.Ordinal),
                $"Session authentication material leaked into descriptor '{path}'."
            );
        }
    }

    private static void Require(bool condition, string message)
    {
        if (!condition)
        {
            throw new InvalidOperationException(message);
        }
    }

    private static async Task RequireThrowsAsync<TException>(Func<Task> callback)
        where TException : Exception
    {
        try
        {
            await callback();
        }
        catch (TException)
        {
            return;
        }
        throw new InvalidOperationException(
            $"Expected {typeof(TException).Name} was not thrown."
        );
    }

    private static void TryDeleteDirectory(string path)
    {
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
            // HttpListener and Windows Credential Manager cleanup can finish just after
            // the assertion path unwinds. Temporary test data is safe to reclaim later.
        }
    }

    private sealed class LoopbackPersistentBackend : IAsyncDisposable
    {
        private static readonly byte[] EmptyObject = "{}"u8.ToArray();
        private readonly HttpListener _listener;
        private readonly CancellationTokenSource _lifetime = new();
        private readonly TaskCompletionSource _shutdownRequested = new(
            TaskCreationOptions.RunContinuationsAsynchronously
        );
        private readonly string _instanceId;
        private readonly string _fingerprint;
        private readonly string _sessionToken;
        private readonly Task _serverLoop;
        private int _invalidAuthorizationCount;
        private int _jobListRequestCount;
        private int _shutdownRequestCount;
        private int _shutdownAccepted;
        private int _runningJobs;
        private int _queueDepth;
        private int _failNextHealthRequest;

        private LoopbackPersistentBackend(
            HttpListener listener,
            int port,
            string instanceId,
            string fingerprint,
            string sessionToken)
        {
            _listener = listener;
            Port = port;
            _instanceId = instanceId;
            _fingerprint = fingerprint;
            _sessionToken = sessionToken;
            _serverLoop = RunServerLoopAsync();
        }

        public int Port { get; }

        public int InvalidAuthorizationCount => Volatile.Read(
            ref _invalidAuthorizationCount
        );

        public int JobListRequestCount => Volatile.Read(ref _jobListRequestCount);

        public int ShutdownRequestCount => Volatile.Read(ref _shutdownRequestCount);

        public void SetActivity(int runningJobs, int queueDepth)
        {
            if (runningJobs < 0)
            {
                throw new ArgumentOutOfRangeException(nameof(runningJobs));
            }
            if (queueDepth < 0)
            {
                throw new ArgumentOutOfRangeException(nameof(queueDepth));
            }
            Volatile.Write(ref _runningJobs, runningJobs);
            Volatile.Write(ref _queueDepth, queueDepth);
        }

        public void FailNextHealthRequest() => Volatile.Write(
            ref _failNextHealthRequest,
            1
        );

        public static Task<LoopbackPersistentBackend> StartAsync(
            string instanceId,
            string fingerprint,
            string sessionToken)
        {
            var port = ReserveLoopbackPort();
            var listener = new HttpListener();
            listener.Prefixes.Add($"http://127.0.0.1:{port}/");
            try
            {
                listener.Start();
                return Task.FromResult(
                    new LoopbackPersistentBackend(
                        listener,
                        port,
                        instanceId,
                        fingerprint,
                        sessionToken
                    )
                );
            }
            catch
            {
                listener.Close();
                throw;
            }
        }

        public async Task<bool> IsHealthyAsync()
        {
            try
            {
                using var client = new BackendApiClient(
                    new Uri($"http://127.0.0.1:{Port}/"),
                    _sessionToken
                );
                using var timeout = new CancellationTokenSource(TimeSpan.FromMilliseconds(500));
                var health = await client.GetHealthAsync(timeout.Token);
                return health.IsReady && health.InstanceId == _instanceId;
            }
            catch (Exception exception) when (
                exception is HttpRequestException or TaskCanceledException or BackendApiException
            )
            {
                return false;
            }
        }

        public Task WaitForShutdownAsync(TimeSpan timeout) =>
            _shutdownRequested.Task.WaitAsync(timeout);

        private async Task RunServerLoopAsync()
        {
            while (!_lifetime.IsCancellationRequested && _listener.IsListening)
            {
                HttpListenerContext context;
                try
                {
                    context = await _listener.GetContextAsync().WaitAsync(_lifetime.Token);
                }
                catch (OperationCanceledException) when (_lifetime.IsCancellationRequested)
                {
                    break;
                }
                catch (Exception exception) when (
                    exception is HttpListenerException or ObjectDisposedException
                )
                {
                    break;
                }

                await HandleRequestAsync(context);
            }
        }

        private async Task HandleRequestAsync(HttpListenerContext context)
        {
            var request = context.Request;
            var response = context.Response;
            var shouldStop = false;
            try
            {
                if (!string.Equals(
                    request.Headers["Authorization"],
                    $"Bearer {_sessionToken}",
                    StringComparison.Ordinal
                ))
                {
                    Interlocked.Increment(ref _invalidAuthorizationCount);
                    response.StatusCode = (int)HttpStatusCode.Unauthorized;
                    await WriteJsonAsync(response, new
                    {
                        error = new { code = "unauthorized", message = "Invalid session token." },
                    });
                    return;
                }

                if (Volatile.Read(ref _shutdownAccepted) != 0)
                {
                    // HttpListener/HTTP.sys can retain an already-established connection
                    // after Stop/Abort. Reset the host's first exit probe explicitly, then
                    // tear down the listener. This models the connection-reset behavior of
                    // the real Python process as it exits after accepting shutdown.
                    response.Abort();
                    shouldStop = true;
                    return;
                }

                var path = request.Url?.AbsolutePath ?? string.Empty;
                if (request.HttpMethod == "GET" && path == "/health")
                {
                    if (Interlocked.Exchange(ref _failNextHealthRequest, 0) != 0)
                    {
                        response.Abort();
                        return;
                    }
                    await WriteJsonAsync(response, CreateHealthResponse());
                    return;
                }
                if (request.HttpMethod == "GET" && path == "/version")
                {
                    await WriteJsonAsync(response, CreateVersionResponse());
                    return;
                }
                if (request.HttpMethod == "GET" && path == "/v1/jobs")
                {
                    Interlocked.Increment(ref _jobListRequestCount);
                    await WriteJsonAsync(response, new
                    {
                        instance_id = _instanceId,
                        config_fingerprint = _fingerprint,
                        capabilities = CreateCapabilities(),
                        jobs = Array.Empty<object>(),
                        count = 0,
                        total_count = 0,
                    });
                    return;
                }
                if (request.HttpMethod == "POST" && path == "/v1/control/shutdown")
                {
                    using var body = await JsonDocument.ParseAsync(request.InputStream);
                    var ifIdle = body.RootElement.TryGetProperty("if_idle", out var property) &&
                        property.ValueKind == JsonValueKind.True;
                    if (!ifIdle)
                    {
                        response.StatusCode = (int)HttpStatusCode.BadRequest;
                        await WriteJsonAsync(response, new
                        {
                            error = new
                            {
                                code = "if_idle_required",
                                message = "Safe shutdown requires if_idle=true.",
                            },
                        });
                        return;
                    }
                    Interlocked.Increment(ref _shutdownRequestCount);
                    if (Volatile.Read(ref _runningJobs) > 0 ||
                        Volatile.Read(ref _queueDepth) > 0)
                    {
                        response.StatusCode = (int)HttpStatusCode.Conflict;
                        await WriteJsonAsync(response, new
                        {
                            error = new
                            {
                                code = "backend_busy",
                                message = "The backend still has active work.",
                            },
                        });
                        return;
                    }
                    Volatile.Write(ref _shutdownAccepted, 1);
                    await WriteJsonAsync(response, new
                    {
                        accepted = true,
                        instance_id = _instanceId,
                    });
                    _shutdownRequested.TrySetResult();
                    return;
                }

                response.StatusCode = (int)HttpStatusCode.NotFound;
                await WriteJsonAsync(response, new
                {
                    error = new { code = "not_found", message = "Unknown contract route." },
                });
            }
            catch
            {
                response.Abort();
                throw;
            }
            finally
            {
                if (shouldStop)
                {
                    // Abort only after the accepted JSON response has been closed. This
                    // guarantees that the host's endpoint-exit probe observes a refused
                    // connection rather than an HttpListener request left in the queue.
                    _lifetime.Cancel();
                    _listener.Abort();
                }
            }
        }

        private object CreateHealthResponse() => new
        {
            instance_id = _instanceId,
            config_fingerprint = _fingerprint,
            status = "ok",
            service_ready = true,
            worker_alive = true,
            queue_depth = Volatile.Read(ref _queueDepth),
            running_jobs = Volatile.Read(ref _runningJobs),
            protocol_version = 2,
            credentials_configured = false,
            capabilities = CreateCapabilities(),
        };

        private object CreateVersionResponse() => new
        {
            instance_id = _instanceId,
            config_fingerprint = _fingerprint,
            protocol_version = 2,
            app = "zvec-persistent-contract",
            version = "contract",
            capabilities = CreateCapabilities(),
            credentials_configured = false,
        };

        private static object CreateCapabilities() => new
        {
            persistent_backend_session = true,
            graceful_shutdown = true,
            multi_library = true,
            federated_search = true,
            session_credentials = true,
            low_confidence_override = true,
            concurrent_jobs = true,
            job_list = true,
            partial_jobs = true,
            job_pause_resume = true,
            failure_paging = true,
            index_and_auto_tag = true,
        };

        private static async Task WriteJsonAsync(HttpListenerResponse response, object payload)
        {
            var bytes = payload is null
                ? EmptyObject
                : JsonSerializer.SerializeToUtf8Bytes(payload, JsonOptions);
            response.StatusCode = response.StatusCode == 0
                ? (int)HttpStatusCode.OK
                : response.StatusCode;
            response.ContentType = "application/json; charset=utf-8";
            // Do not leave an HTTP.sys keep-alive connection behind after the listener
            // is aborted. BackendHostService proves exit by opening the next health
            // request, which must observe connection refusal rather than a half-open
            // request that merely times out.
            response.KeepAlive = false;
            response.ContentLength64 = bytes.Length;
            await response.OutputStream.WriteAsync(bytes);
            response.Close();
        }

        private static int ReserveLoopbackPort()
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

        public async ValueTask DisposeAsync()
        {
            _lifetime.Cancel();
            if (_listener.IsListening)
            {
                _listener.Stop();
            }
            _listener.Close();
            try
            {
                await _serverLoop;
            }
            catch (OperationCanceledException)
            {
                // Normal disposal path.
            }
            _lifetime.Dispose();
        }
    }
}
