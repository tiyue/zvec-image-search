using System.Text.Json;
using System.Text.Json.Serialization;
using System.Windows.Media.Imaging;

namespace Zvec.Desktop.Models;

public sealed class SearchManifest
{
    [JsonPropertyName("status")]
    public string Status { get; set; } = "ok";

    [JsonPropertyName("candidate_count")]
    public int CandidateCount { get; set; }

    [JsonPropertyName("filtered_count")]
    public int FilteredCount { get; set; }

    [JsonPropertyName("latency_ms")]
    public double? LatencyMs { get; set; }

    [JsonPropertyName("ranking_mode")]
    public string RankingMode { get; set; } = string.Empty;

    [JsonPropertyName("show_low_confidence")]
    public bool ShowLowConfidence { get; set; }

    [JsonPropertyName("low_confidence_override")]
    public bool LowConfidenceOverride { get; set; }

    [JsonPropertyName("query_type")]
    public string QueryType { get; set; } = string.Empty;

    [JsonPropertyName("output_dir")]
    public string OutputDirectory { get; set; } = string.Empty;

    [JsonPropertyName("result_count")]
    public int ResultCount { get; set; }

    [JsonPropertyName("created_at")]
    public DateTimeOffset? CreatedAt { get; set; }

    [JsonPropertyName("model")]
    public string Model { get; set; } = string.Empty;

    [JsonPropertyName("metric")]
    public string Metric { get; set; } = string.Empty;

    [JsonPropertyName("library_ids")]
    public List<string> LibraryIds { get; set; } = [];

    [JsonPropertyName("library_names")]
    public List<string> LibraryNames { get; set; } = [];

    [JsonPropertyName("libraries")]
    public List<SearchManifestLibrary> Libraries { get; set; } = [];

    [JsonPropertyName("query")]
    public JsonElement Query { get; set; }

    [JsonPropertyName("results")]
    public List<SearchManifestHit> Results { get; set; } = [];

    [JsonPropertyName("copy_failures")]
    public List<SearchCopyFailure> CopyFailures { get; set; } = [];
}

public sealed class SearchManifestHit
{
    [JsonPropertyName("rank")]
    public int Rank { get; set; }

    [JsonPropertyName("distance")]
    public double Distance { get; set; }

    [JsonPropertyName("library_id")]
    public string LibraryId { get; set; } = string.Empty;

    [JsonPropertyName("library_name")]
    public string LibraryName { get; set; } = string.Empty;

    [JsonPropertyName("root_id")]
    public string RootId { get; set; } = string.Empty;

    [JsonPropertyName("relative_path")]
    public string RelativePath { get; set; } = string.Empty;

    [JsonPropertyName("copied_file")]
    public string CopiedFile { get; set; } = string.Empty;

    [JsonPropertyName("doc_id")]
    public string DocumentId { get; set; } = string.Empty;

    [JsonPropertyName("tags")]
    public List<string> Tags { get; set; } = [];

    [JsonPropertyName("matched_tags")]
    public List<string> MatchedTags { get; set; } = [];

    [JsonPropertyName("fused_score")]
    public double? FusedScore { get; set; }

    [JsonPropertyName("raw_score")]
    public double? RawScore { get; set; }

    [JsonPropertyName("normalized_score")]
    public double? NormalizedScore { get; set; }

    [JsonPropertyName("confidence")]
    public double? Confidence { get; set; }

    [JsonPropertyName("ranking_confidence")]
    public double? RankingConfidence { get; set; }

    [JsonPropertyName("match_state")]
    public string MatchState { get; set; } = string.Empty;

    [JsonPropertyName("rank_source")]
    public string RankSource { get; set; } = string.Empty;

    [JsonPropertyName("image_confidence")]
    public double? ImageConfidence { get; set; }

    [JsonPropertyName("text_confidence")]
    public double? TextConfidence { get; set; }

    [JsonPropertyName("metadata_confidence")]
    public double? MetadataConfidence { get; set; }

    [JsonPropertyName("image_rank")]
    public int? ImageRank { get; set; }

    [JsonPropertyName("text_rank")]
    public int? TextRank { get; set; }

    [JsonPropertyName("metadata_rank")]
    public int? MetadataRank { get; set; }

    [JsonPropertyName("rank_agreement")]
    public double? RankAgreement { get; set; }
}

public sealed class SearchManifestLibrary
{
    [JsonPropertyName("id")]
    public string Id { get; set; } = string.Empty;

    [JsonPropertyName("name")]
    public string Name { get; set; } = string.Empty;
}

public sealed class SearchCopyFailure
{
    [JsonPropertyName("path")]
    public string Path { get; set; } = string.Empty;

    [JsonPropertyName("error")]
    public string Error { get; set; } = string.Empty;
}

public sealed class SearchResultItem
{
    public required int Rank { get; init; }

    public required string Name { get; init; }

    public required string RelativePath { get; init; }

    public required string CopiedPath { get; init; }

    public string? OriginalPath { get; init; }

    public required string DocumentId { get; init; }

    public required string LibraryId { get; init; }

    public required string LibraryName { get; init; }

    public required string RootId { get; init; }

    public required double Distance { get; init; }

    public double? FusedScore { get; init; }

    public double? RawScore { get; init; }

    public double? NormalizedScore { get; init; }

    public double? Confidence { get; init; }

    public double? RankingConfidence { get; init; }

    public required string MatchState { get; init; }

    public required string RankSource { get; init; }

    public double? ImageConfidence { get; init; }

    public double? TextConfidence { get; init; }

    public double? MetadataConfidence { get; init; }

    public int? ImageRank { get; init; }

    public int? TextRank { get; init; }

    public int? MetadataRank { get; init; }

    public double? RankAgreement { get; init; }

    public bool IsLowConfidenceOverride { get; init; }

    public required IReadOnlyList<string> Tags { get; init; }

    public IReadOnlyList<string> MatchedTags { get; init; } = [];

    public BitmapSource? Thumbnail { get; init; }

    public string RankText => $"#{Rank}";

    public string MatchStateText => (MatchState ?? string.Empty).Trim().ToLowerInvariant() switch
    {
        "high" => "高度相关",
        "possible" => "可能相关",
        "weak" => "低置信度",
        _ => "相关性未校准",
    };

    public string RankSourceText => (RankSource ?? string.Empty).Trim().ToLowerInvariant() switch
    {
        "image" => "图片匹配",
        "text" => "文字匹配",
        "metadata" => "描述匹配",
        "fused" => "图文联合",
        "tag" => "标签匹配",
        _ => "来源未知",
    };

    public string ScoreText => IsLowConfidenceOverride
        ? $"不可信候选 · {MatchStateText} · {RankSourceText}"
        : $"{MatchStateText} · {RankSourceText}";

    public string DiagnosticScoreText
    {
        get
        {
            var values = new List<string>();
            if (IsLowConfidenceOverride)
            {
                values.Add("低置信展示已开启");
            }
            if (RawScore.HasValue)
            {
                values.Add($"原始 {RawScore.Value:0.000000}");
            }
            if (NormalizedScore.HasValue)
            {
                values.Add($"归一化 {NormalizedScore.Value:0.000}");
            }
            if (Confidence.HasValue)
            {
                values.Add($"置信度 {Confidence.Value:0.000}");
            }
            if (RankingConfidence.HasValue)
            {
                values.Add($"排序置信度 {RankingConfidence.Value:0.000}");
            }
            if (ImageConfidence.HasValue)
            {
                var rank = ImageRank.HasValue ? $" #{ImageRank.Value}" : string.Empty;
                values.Add($"图片 {ImageConfidence.Value:0.000}{rank}");
            }
            if (TextConfidence.HasValue)
            {
                var rank = TextRank.HasValue ? $" #{TextRank.Value}" : string.Empty;
                values.Add($"文字 {TextConfidence.Value:0.000}{rank}");
            }
            if (MetadataConfidence.HasValue)
            {
                var rank = MetadataRank.HasValue ? $" #{MetadataRank.Value}" : string.Empty;
                values.Add($"描述 {MetadataConfidence.Value:0.000}{rank}");
            }
            if (RankAgreement.HasValue)
            {
                values.Add($"通道一致度 {RankAgreement.Value:0.000}");
            }
            if (values.Count == 0)
            {
                values.Add(FusedScore.HasValue
                    ? $"旧版 RRF {FusedScore.Value:0.000000}"
                    : $"旧版距离 {Distance:0.000000}");
            }
            return string.Join(" · ", values);
        }
    }

    public string TagsText => Tags.Count == 0 ? "无标签" : string.Join(" · ", Tags);

    public bool HasMatchedTags => MatchedTags.Count > 0;

    public string MatchedTagsText => MatchedTags.Count == 0
        ? string.Empty
        : "命中标签：" + string.Join(" · ", MatchedTags);

    public string LibraryText => string.IsNullOrWhiteSpace(LibraryName)
        ? LibraryId
        : LibraryName;

    public string FullPath => OriginalPath ?? CopiedPath;
}

public sealed class SearchResultSession
{
    public required SearchManifest Manifest { get; init; }

    public required string Directory { get; init; }

    public required string ManifestPath { get; init; }

    public required IReadOnlyList<SearchResultItem> Items { get; init; }

    public int EffectiveCandidateCount => Math.Max(
        Items.Count,
        Math.Max(Manifest.CandidateCount, Manifest.FilteredCount)
    );

    public int SearchedLibraryCount
    {
        get
        {
            var manifestLibraryCount = Manifest.Libraries
                .Where(library =>
                    !string.IsNullOrWhiteSpace(library.Id) ||
                    !string.IsNullOrWhiteSpace(library.Name))
                .Select(library => string.IsNullOrWhiteSpace(library.Id)
                    ? library.Name
                    : library.Id)
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .Count();
            if (manifestLibraryCount > 0)
            {
                return manifestLibraryCount;
            }

            var libraryIdCount = Manifest.LibraryIds
                .Where(id => !string.IsNullOrWhiteSpace(id))
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .Count();
            if (libraryIdCount > 0)
            {
                return libraryIdCount;
            }

            var libraryNameCount = Manifest.LibraryNames
                .Where(name => !string.IsNullOrWhiteSpace(name))
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .Count();
            if (libraryNameCount > 0)
            {
                return libraryNameCount;
            }

            var itemIds = Items
                .Where(item => !string.IsNullOrWhiteSpace(item.LibraryId))
                .Select(item => item.LibraryId)
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .Count();
            return itemIds > 0
                ? itemIds
                : Items
                    .Where(item => !string.IsNullOrWhiteSpace(item.LibraryName))
                    .Select(item => item.LibraryName)
                    .Distinct(StringComparer.OrdinalIgnoreCase)
                    .Count();
        }
    }

    public string SearchedLibrariesText
    {
        get
        {
            var labels = Manifest.LibraryNames
                .Where(name => !string.IsNullOrWhiteSpace(name))
                .ToList();
            if (labels.Count == 0)
            {
                labels = Manifest.Libraries
                    .Select(library => !string.IsNullOrWhiteSpace(library.Name)
                        ? library.Name
                        : library.Id)
                    .Where(label => !string.IsNullOrWhiteSpace(label))
                    .ToList();
            }
            if (labels.Count == 0)
            {
                labels = Manifest.LibraryIds
                    .Where(id => !string.IsNullOrWhiteSpace(id))
                    .ToList();
            }
            if (labels.Count == 0)
            {
                labels = Items
                    .Select(item => item.LibraryText)
                    .Where(label => !string.IsNullOrWhiteSpace(label))
                    .ToList();
            }

            var distinct = labels
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .ToList();
            return distinct.Count > 0 ? string.Join("、", distinct) : "当前图库";
        }
    }

    public string SearchLatencyText => Manifest.LatencyMs.HasValue
        ? $"耗时 {Manifest.LatencyMs.Value:0} ms。"
        : string.Empty;

    public string Summary
    {
        get
        {
            var type = Manifest.QueryType switch
            {
                "text" => "文字搜索",
                "image" => "图片搜索",
                "image_text" => "图文搜索",
                _ => "搜索",
            };
            var created = Manifest.CreatedAt?.ToLocalTime().ToString("yyyy-MM-dd HH:mm:ss")
                ?? "未知时间";
            var scope = SearchedLibraryCount > 0
                ? $"{SearchedLibraryCount} 个图库 · "
                : string.Empty;
            var latency = Manifest.LatencyMs.HasValue
                ? $" · {Manifest.LatencyMs.Value:0} ms"
                : string.Empty;
            var state = IsLowConfidenceOverride && Items.Count > 0
                ? $"{Items.Count} 个低置信候选（不可信）"
                : IsNoReliableMatch
                ? "无可靠结果"
                : HasCalibratedRanking
                    ? $"{Items.Count} 个可信结果"
                    : $"{Items.Count} 个结果";
            return $"{type} · {scope}{state}（候选 {EffectiveCandidateCount}）{latency} · {created}";
        }
    }

    public bool IsNoReliableMatch =>
        Items.Count == 0 && Manifest.Status is "no_reliable_match" or "no_matches";

    public bool IsLowConfidenceOverride =>
        Manifest.LowConfidenceOverride ||
        string.Equals(
            Manifest.Status,
            "low_confidence_override",
            StringComparison.OrdinalIgnoreCase
        );

    public bool CanShowLowConfidence =>
        IsNoReliableMatch && !WasLowConfidenceRequested && EffectiveCandidateCount > 0;

    public bool CanOfferLowConfidence(bool backendSupportsOverride) =>
        backendSupportsOverride && CanShowLowConfidence;

    public bool WasLowConfidenceRequested => Manifest.ShowLowConfidence;

    public string EmptyTitle => Items.Count == 0 && WasLowConfidenceRequested
        ? "仍无可显示候选"
        : IsNoReliableMatch
            ? "无可靠结果"
            : "暂无检索结果";

    private bool HasCalibratedRanking => Manifest.RankingMode.StartsWith(
        "confidence",
        StringComparison.OrdinalIgnoreCase
    );

    public string EmptyCaption
    {
        get
        {
            if (Items.Count == 0 && WasLowConfidenceRequested)
            {
                var overrideTagFilter = HasTagFilter
                    ? "标签筛选已启用。"
                    : "未使用标签筛选。";
                var copyFailures = Manifest.CopyFailures.Count > 0
                    ? $"另有 {Manifest.CopyFailures.Count} 个候选复制失败。"
                    : string.Empty;
                return $"已请求显示低置信候选，但仍没有可返回的文件。图库：{SearchedLibrariesText}。{overrideTagFilter}{SearchLatencyText}{copyFailures}";
            }

            if (!IsNoReliableMatch)
            {
                return "填写左侧条件开始搜索，或调整范围与关键词后重试。";
            }

            var tagFilter = HasTagFilter ? "标签筛选已启用。" : "未使用标签筛选。";
            var candidateSummary = EffectiveCandidateCount > 0
                ? $"已检查 {EffectiveCandidateCount} 个候选，均未通过可靠性筛选。"
                : "没有召回到候选。";
            return $"{candidateSummary}图库：{SearchedLibrariesText}。{tagFilter}{SearchLatencyText}可调整关键词、图片或标签后重试。";
        }
    }

    private bool HasTagFilter
    {
        get
        {
            if (Manifest.Query.ValueKind != JsonValueKind.Object ||
                !Manifest.Query.TryGetProperty("tags", out var tags) ||
                tags.ValueKind != JsonValueKind.Array)
            {
                return false;
            }
            return tags.EnumerateArray().Any(tag =>
                tag.ValueKind == JsonValueKind.String &&
                !string.IsNullOrWhiteSpace(tag.GetString()));
        }
    }
}
