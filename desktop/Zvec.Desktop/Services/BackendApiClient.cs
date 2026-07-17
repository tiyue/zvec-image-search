using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text.Json;
using Zvec.Desktop.Models;

namespace Zvec.Desktop.Services;

public sealed class BackendApiClient : IDisposable
{
    private static readonly HashSet<string> ValidJobStatuses = new(
        [
            "queued",
            "running",
            "pausing",
            "paused",
            "needs_attention",
            "cancelling",
            "succeeded",
            "partial",
            "failed",
            "cancelled",
        ],
        StringComparer.OrdinalIgnoreCase
    );

    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web)
    {
        PropertyNameCaseInsensitive = true,
    };

    private readonly HttpClient _httpClient;
    private readonly Uri _baseAddress;
    private readonly string _sessionToken;
    private readonly bool _ownsHttpClient;
    private readonly string? _submissionDisabledReason;
    private bool _disposed;

    public BackendApiClient(
        Uri baseAddress,
        string sessionToken,
        HttpClient? httpClient = null,
        string? submissionDisabledReason = null)
    {
        ArgumentNullException.ThrowIfNull(baseAddress);
        ArgumentException.ThrowIfNullOrWhiteSpace(sessionToken);
        if (!baseAddress.IsAbsoluteUri)
        {
            throw new ArgumentException("Backend base address must be absolute.", nameof(baseAddress));
        }

        _baseAddress = baseAddress.AbsoluteUri.EndsWith("/", StringComparison.Ordinal)
            ? baseAddress
            : new Uri(baseAddress.AbsoluteUri + '/', UriKind.Absolute);
        _sessionToken = sessionToken;
        _httpClient = httpClient ?? new HttpClient
        {
            Timeout = TimeSpan.FromSeconds(30),
        };
        _ownsHttpClient = httpClient is null;
        _submissionDisabledReason = string.IsNullOrWhiteSpace(submissionDisabledReason)
            ? null
            : submissionDisabledReason.Trim();
    }

    public Uri BaseAddress => _baseAddress;

    public bool CanSubmitJobs => _submissionDisabledReason is null;

    public async Task<BackendHealthResponse> GetHealthAsync(
        CancellationToken cancellationToken = default)
    {
        using var request = CreateRequest(HttpMethod.Get, "health");
        using var response = await _httpClient.SendAsync(
            request,
            HttpCompletionOption.ResponseHeadersRead,
            cancellationToken
        );

        // A started server can deliberately return 503 while service initialization is
        // degraded. Preserve that typed health payload so the host can keep polling.
        if (response.IsSuccessStatusCode || response.StatusCode == HttpStatusCode.ServiceUnavailable)
        {
            return await ReadResponseAsync<BackendHealthResponse>(response, cancellationToken);
        }

        throw await CreateApiExceptionAsync(response, cancellationToken);
    }

    public async Task<BackendVersionResponse> GetVersionAsync(
        CancellationToken cancellationToken = default)
    {
        using var request = CreateRequest(HttpMethod.Get, "version");
        using var response = await _httpClient.SendAsync(
            request,
            HttpCompletionOption.ResponseHeadersRead,
            cancellationToken
        );
        await EnsureSuccessAsync(response, cancellationToken);
        return await ReadResponseAsync<BackendVersionResponse>(response, cancellationToken);
    }

    public async Task ConfigureCredentialsAsync(
        string dashScopeApiKey,
        string? apiUrl = null,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(dashScopeApiKey);
        using var request = CreateRequest(HttpMethod.Put, "v1/session/credentials");
        request.Content = CreateJsonContent(
            new BackendCredentialRequest
            {
                DashScopeApiKey = dashScopeApiKey,
                ApiUrl = string.IsNullOrWhiteSpace(apiUrl) ? null : apiUrl.Trim(),
            }
        );
        using var response = await _httpClient.SendAsync(
            request,
            HttpCompletionOption.ResponseHeadersRead,
            cancellationToken
        );
        await EnsureSuccessAsync(response, cancellationToken);
        var configured = await ReadResponseAsync<BackendCredentialResponse>(
            response,
            cancellationToken
        );
        if (!configured.CredentialsConfigured)
        {
            throw new BackendProtocolException(
                "Backend did not confirm the session credential configuration."
            );
        }
    }

    public Task<BackendJob> SubmitJobAsync(
        string command,
        IReadOnlyDictionary<string, object?>? parameters = null,
        CancellationToken cancellationToken = default) =>
        SubmitJobAsync(new BackendJobRequest(command, parameters), cancellationToken);

    public async Task<BackendJob> SubmitJobAsync(
        BackendJobRequest jobRequest,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(jobRequest);
        ArgumentException.ThrowIfNullOrWhiteSpace(jobRequest.Command);
        if (_submissionDisabledReason is not null)
        {
            throw new BackendProtocolException(_submissionDisabledReason);
        }

        using var request = CreateRequest(HttpMethod.Post, "v1/jobs");
        request.Content = CreateJsonContent(jobRequest);
        using var response = await _httpClient.SendAsync(
            request,
            HttpCompletionOption.ResponseHeadersRead,
            cancellationToken
        );
        await EnsureSuccessAsync(response, cancellationToken);
        return await ReadJobAsync(response, expectedJobId: null, cancellationToken);
    }

    public Task<BackendJob> EstimateAutoTagsAsync(
        AutoTagEstimateRequest request,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(request);
        ValidateAutoTagOptions(
            request.LibraryId,
            request.Scope,
            request.Model,
            request.MaxImages,
            request.MaxBudgetCny
        );
        return SubmitJobAsync("auto_tag_estimate", request.ToParameters(), cancellationToken);
    }

    public Task<BackendJob> StartAutoTagAsync(
        AutoTagRunRequest request,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(request);
        ValidateAutoTagOptions(
            request.LibraryId,
            request.Scope,
            request.Model,
            request.MaxImages,
            request.MaxBudgetCny
        );
        if (!request.ExternalProcessingConfirmed)
        {
            throw new ArgumentException(
                "External processing must be explicitly confirmed before auto-tagging.",
                nameof(request)
            );
        }
        return SubmitJobAsync("auto_tag", request.ToParameters(), cancellationToken);
    }

    public Task<BackendJob> StartIndexAndAutoTagAsync(
        IndexAndAutoTagRequest request,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(request);
        ValidateAutoTagOptions(
            request.LibraryId,
            AutoTagScopes.LatestIndexRun,
            request.Model,
            request.MaxImages,
            request.MaxBudgetCny
        );
        if (!request.ExternalProcessingConfirmed)
        {
            throw new ArgumentException(
                "External processing must be explicitly confirmed before indexing and auto-tagging.",
                nameof(request)
            );
        }
        return SubmitJobAsync(
            "index_and_auto_tag",
            request.ToParameters(),
            cancellationToken
        );
    }

    public Task<BackendJob> GetPendingAutoTagsAsync(
        AutoTagPendingRequest request,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(request);
        ArgumentException.ThrowIfNullOrWhiteSpace(request.LibraryId);
        if (request.Offset < 0)
        {
            throw new ArgumentOutOfRangeException(
                nameof(request),
                "Pending auto-tag offset cannot be negative."
            );
        }
        if (request.Limit is <= 0 or > 500)
        {
            throw new ArgumentOutOfRangeException(
                nameof(request),
                "Pending auto-tag page size must be between 1 and 500."
            );
        }
        return SubmitJobAsync("auto_tag_pending", request.ToParameters(), cancellationToken);
    }

    public Task<BackendJob> SubmitAutoTagReviewAsync(
        AutoTagReviewRequest request,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(request);
        ArgumentException.ThrowIfNullOrWhiteSpace(request.LibraryId);
        if (request.Decisions.Count == 0)
        {
            throw new ArgumentException(
                "At least one review decision is required.",
                nameof(request)
            );
        }
        foreach (var decision in request.Decisions)
        {
            ArgumentException.ThrowIfNullOrWhiteSpace(decision.ProposalId);
            if (decision.Decision is not ("accept" or "reject" or "manual"))
            {
                throw new ArgumentException(
                    $"Unsupported review decision: {decision.Decision}",
                    nameof(request)
                );
            }
            if ((decision.Decision is "reject" or "manual") &&
                decision.ConfirmedIdentityTags.Count > 0)
            {
                throw new ArgumentException(
                    "Rejected or manually labeled proposals cannot confirm identity tags.",
                    nameof(request)
                );
            }
            if (decision.Decision == "manual" && decision.AcceptedTags.Count == 0)
            {
                throw new ArgumentException(
                    "Manual labeling requires at least one accepted tag.",
                    nameof(request)
                );
            }
            var acceptedTags = decision.AcceptedTags.ToHashSet(StringComparer.Ordinal);
            if (decision.ConfirmedIdentityTags.Any(tag => !acceptedTags.Contains(tag)))
            {
                throw new ArgumentException(
                    "Confirmed identity tags must also be present in accepted tags.",
                    nameof(request)
                );
            }
        }
        return SubmitJobAsync("auto_tag_review", request.ToParameters(), cancellationToken);
    }

    public Task<BackendJob> BackfillFolderTagsAsync(
        FolderTagBackfillRequest request,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(request);
        ArgumentException.ThrowIfNullOrWhiteSpace(request.LibraryId);
        return SubmitJobAsync("folder_tag_backfill", request.ToParameters(), cancellationToken);
    }

    public Task<BackendJob> BackfillMetadataEmbeddingsAsync(
        MetadataBackfillRequest request,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(request);
        ArgumentException.ThrowIfNullOrWhiteSpace(request.LibraryId);
        if (request.MaxImages is <= 0 or > 10_000)
        {
            throw new ArgumentOutOfRangeException(
                nameof(request),
                "Metadata backfill size must be between 1 and 10,000."
            );
        }
        return SubmitJobAsync(
            "metadata_backfill",
            request.ToParameters(),
            cancellationToken
        );
    }

    public Task<BackendJob> SubmitAutoTagBatchReviewAsync(
        AutoTagBatchReviewRequest request,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(request);
        ArgumentException.ThrowIfNullOrWhiteSpace(request.LibraryId);
        if (request.ProposalIds.Count == 0)
        {
            throw new ArgumentException("At least one proposal is required.", nameof(request));
        }
        if (!request.ExcludeIdentityTags)
        {
            throw new ArgumentException(
                "Batch review must exclude identity tags.",
                nameof(request)
            );
        }
        foreach (var proposalId in request.ProposalIds)
        {
            ArgumentException.ThrowIfNullOrWhiteSpace(proposalId);
            if (!request.AcceptedTagsByProposal.TryGetValue(proposalId, out var tags) ||
                tags.Count == 0)
            {
                throw new ArgumentException(
                    $"Batch proposal '{proposalId}' has no accepted low-risk tags.",
                    nameof(request)
                );
            }
        }
        var proposalIds = request.ProposalIds.ToHashSet(StringComparer.Ordinal);
        if (request.AcceptedTagsByProposal.Keys.Any(id => !proposalIds.Contains(id)))
        {
            throw new ArgumentException(
                "AcceptedTagsByProposal contains an unknown proposal ID.",
                nameof(request)
            );
        }
        return SubmitJobAsync("auto_tag_review_batch", request.ToParameters(), cancellationToken);
    }

    public Task<BackendJob> UndoAutoTagBatchReviewAsync(
        AutoTagReviewUndoRequest request,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(request);
        ArgumentException.ThrowIfNullOrWhiteSpace(request.LibraryId);
        return SubmitJobAsync("auto_tag_review_undo", request.ToParameters(), cancellationToken);
    }

    public Task<BackendJob> ListTagAliasesAsync(
        TagAliasListRequest request,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(request);
        ArgumentException.ThrowIfNullOrWhiteSpace(request.LibraryId);
        return SubmitJobAsync("tag_alias_list", request.ToParameters(), cancellationToken);
    }

    public Task<BackendJob> UpsertTagAliasAsync(
        TagAliasUpsertRequest request,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(request);
        ArgumentException.ThrowIfNullOrWhiteSpace(request.LibraryId);
        ArgumentException.ThrowIfNullOrWhiteSpace(request.CanonicalName);
        if (request.Aliases.Count == 0)
        {
            throw new ArgumentException("At least one alias is required.", nameof(request));
        }
        return SubmitJobAsync("tag_alias_upsert", request.ToParameters(), cancellationToken);
    }

    public Task<BackendJob> DeleteTagAliasAsync(
        TagAliasDeleteRequest request,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(request);
        ArgumentException.ThrowIfNullOrWhiteSpace(request.LibraryId);
        ArgumentException.ThrowIfNullOrWhiteSpace(request.CanonicalName);
        return SubmitJobAsync("tag_alias_delete", request.ToParameters(), cancellationToken);
    }

    public async Task<BackendJob> GetJobAsync(
        string jobId,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(jobId);
        var path = $"v1/jobs/{Uri.EscapeDataString(jobId)}";
        using var request = CreateRequest(HttpMethod.Get, path);
        using var response = await _httpClient.SendAsync(
            request,
            HttpCompletionOption.ResponseHeadersRead,
            cancellationToken
        );
        await EnsureSuccessAsync(response, cancellationToken);
        return await ReadJobAsync(response, jobId, cancellationToken);
    }

    public async Task<BackendJobListResponse> GetJobsAsync(
        bool? active = null,
        int limit = 100,
        CancellationToken cancellationToken = default)
    {
        if (limit is < 1 or > 500)
        {
            throw new ArgumentOutOfRangeException(
                nameof(limit),
                "Job list limit must be between 1 and 500."
            );
        }
        var query = active.HasValue
            ? $"?active={active.Value.ToString().ToLowerInvariant()}&limit={limit}"
            : $"?limit={limit}";
        using var request = CreateRequest(HttpMethod.Get, $"v1/jobs{query}");
        using var response = await _httpClient.SendAsync(
            request,
            HttpCompletionOption.ResponseHeadersRead,
            cancellationToken
        );
        await EnsureSuccessAsync(response, cancellationToken);
        var jobs = await ReadResponseAsync<BackendJobListResponse>(response, cancellationToken);
        foreach (var job in jobs.Jobs)
        {
            ValidateJob(job, expectedJobId: null);
        }
        return jobs;
    }

    public async Task ShutdownIfIdleAsync(
        CancellationToken cancellationToken = default)
    {
        using var request = CreateRequest(HttpMethod.Post, "v1/control/shutdown");
        // Force the shutdown request off the reusable HTTP/1.1 connection. Otherwise
        // a request handler that already owns a keep-alive socket can outlive the
        // listening socket briefly and make the stopped service appear reachable.
        request.Headers.ConnectionClose = true;
        request.Content = CreateJsonContent(new { if_idle = true });
        using var response = await _httpClient.SendAsync(
            request,
            HttpCompletionOption.ResponseHeadersRead,
            cancellationToken
        );
        await EnsureSuccessAsync(response, cancellationToken);
    }

    public Task<BackendJob> PollJobAsync(
        string jobId,
        CancellationToken cancellationToken = default) =>
        GetJobAsync(jobId, cancellationToken);

    public async Task<BackendJob> PollJobAsync(
        string jobId,
        TimeSpan pollInterval,
        Action<BackendJob>? progress = null,
        CancellationToken cancellationToken = default)
    {
        if (pollInterval <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(
                nameof(pollInterval),
                "Poll interval must be positive."
            );
        }

        var transientFailureCount = 0;
        while (true)
        {
            BackendJob job;
            try
            {
                job = await GetJobAsync(jobId, cancellationToken);
                transientFailureCount = 0;
            }
            catch (Exception exception) when (
                exception is HttpRequestException or TaskCanceledException &&
                !cancellationToken.IsCancellationRequested &&
                transientFailureCount < 3
            )
            {
                transientFailureCount++;
                await Task.Delay(pollInterval, cancellationToken);
                continue;
            }
            progress?.Invoke(job);
            if (job.IsTerminal || job.IsPaused || job.NeedsAttention)
            {
                return job;
            }

            await Task.Delay(pollInterval, cancellationToken);
        }
    }

    public async Task<BackendJob> CancelJobAsync(
        string jobId,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(jobId);
        var path = $"v1/jobs/{Uri.EscapeDataString(jobId)}";
        using var request = CreateRequest(HttpMethod.Delete, path);
        using var response = await _httpClient.SendAsync(
            request,
            HttpCompletionOption.ResponseHeadersRead,
            cancellationToken
        );
        await EnsureSuccessAsync(response, cancellationToken);
        return await ReadJobAsync(response, jobId, cancellationToken);
    }

    private static async Task<BackendJob> ReadJobAsync(
        HttpResponseMessage response,
        string? expectedJobId,
        CancellationToken cancellationToken)
    {
        var envelope = await ReadResponseAsync<BackendJobEnvelope>(
            response,
            cancellationToken
        );
        var job = envelope.Job ?? throw new BackendProtocolException(
            "Backend response did not contain a job object."
        );
        ValidateJob(job, expectedJobId);
        return job;
    }

    private static void ValidateJob(BackendJob job, string? expectedJobId)
    {
        if (string.IsNullOrWhiteSpace(job.Id))
        {
            throw new BackendProtocolException("Backend job response did not contain an ID.");
        }
        if (
            expectedJobId is not null &&
            !string.Equals(job.Id, expectedJobId, StringComparison.Ordinal)
        )
        {
            throw new BackendProtocolException(
                $"Backend returned job '{job.Id}' while '{expectedJobId}' was requested."
            );
        }
        if (string.IsNullOrWhiteSpace(job.Command))
        {
            throw new BackendProtocolException(
                $"Backend job '{job.Id}' did not contain a command."
            );
        }
        if (!ValidJobStatuses.Contains(job.Status))
        {
            throw new BackendProtocolException(
                $"Backend job '{job.Id}' returned an unknown status '{job.Status}'."
            );
        }
    }

    private HttpRequestMessage CreateRequest(HttpMethod method, string relativePath)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        var request = new HttpRequestMessage(method, new Uri(_baseAddress, relativePath));
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _sessionToken);
        request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
        return request;
    }

    private static ByteArrayContent CreateJsonContent<T>(T value)
    {
        var bytes = JsonSerializer.SerializeToUtf8Bytes(value, JsonOptions);
        var content = new ByteArrayContent(bytes);
        content.Headers.ContentType = new MediaTypeHeaderValue("application/json")
        {
            CharSet = "utf-8",
        };
        content.Headers.ContentLength = bytes.Length;
        return content;
    }

    private static void ValidateAutoTagOptions(
        string libraryId,
        string scope,
        string model,
        int maxImages,
        decimal maxBudgetCny)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(libraryId);
        ArgumentException.ThrowIfNullOrWhiteSpace(scope);
        ArgumentException.ThrowIfNullOrWhiteSpace(model);
        if (maxImages <= 0)
        {
            throw new ArgumentOutOfRangeException(
                nameof(maxImages),
                "Maximum image count must be positive."
            );
        }
        if (maxBudgetCny <= 0)
        {
            throw new ArgumentOutOfRangeException(
                nameof(maxBudgetCny),
                "Maximum budget must be positive."
            );
        }
    }

    private static async Task EnsureSuccessAsync(
        HttpResponseMessage response,
        CancellationToken cancellationToken)
    {
        if (!response.IsSuccessStatusCode)
        {
            throw await CreateApiExceptionAsync(response, cancellationToken);
        }
    }

    private static async Task<T> ReadResponseAsync<T>(
        HttpResponseMessage response,
        CancellationToken cancellationToken)
    {
        await using var stream = await response.Content.ReadAsStreamAsync(cancellationToken);
        var value = await JsonSerializer.DeserializeAsync<T>(
            stream,
            JsonOptions,
            cancellationToken
        );
        return value ?? throw new InvalidDataException(
            $"Backend returned an empty {typeof(T).Name} response."
        );
    }

    private static async Task<BackendApiException> CreateApiExceptionAsync(
        HttpResponseMessage response,
        CancellationToken cancellationToken)
    {
        var body = await response.Content.ReadAsStringAsync(cancellationToken);
        var reason = string.IsNullOrWhiteSpace(response.ReasonPhrase)
            ? response.StatusCode.ToString()
            : response.ReasonPhrase;
        var message = string.IsNullOrWhiteSpace(body)
            ? $"Backend request failed with HTTP {(int)response.StatusCode} ({reason})."
            : $"Backend request failed with HTTP {(int)response.StatusCode} ({reason}): {body}";
        return new BackendApiException(response.StatusCode, body, message);
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        if (_ownsHttpClient)
        {
            _httpClient.Dispose();
        }
    }
}
