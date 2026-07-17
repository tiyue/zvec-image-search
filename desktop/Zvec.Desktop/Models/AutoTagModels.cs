using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Windows.Media.Imaging;

namespace Zvec.Desktop.Models;

public static class AutoTagScopes
{
    public const string LatestIndexRun = "latest_index_run";
    public const string Untagged = "untagged";
    public const string Failed = "failed";
    public const string All = "all";
}

public static class AutoTagModels
{
    public const string Flash = "qwen3-vl-flash";
    public const string Plus = "qwen3-vl-plus";
}

public static class AutoTagThumbnailSizing
{
    public const int LongestEdge = 192;

    public static (int DecodePixelWidth, int DecodePixelHeight) GetDecodeDimensions(
        int pixelWidth,
        int pixelHeight)
    {
        if (pixelWidth <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(pixelWidth));
        }
        if (pixelHeight <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(pixelHeight));
        }
        return pixelHeight > pixelWidth
            ? (0, LongestEdge)
            : (LongestEdge, 0);
    }
}

public sealed class AutoTagEstimateRequest
{
    public required string LibraryId { get; init; }

    public string Scope { get; init; } = AutoTagScopes.Untagged;

    public string Model { get; init; } = AutoTagModels.Flash;

    public int MaxImages { get; init; } = 300;

    public decimal MaxBudgetCny { get; init; } = 5m;

    public bool ExternalProcessingConfirmed { get; init; }

    public IReadOnlyDictionary<string, object?> ToParameters() =>
        AutoTagParameterFactory.Create(
            LibraryId,
            Scope,
            Model,
            MaxImages,
            MaxBudgetCny,
            ExternalProcessingConfirmed
        );
}

public sealed class AutoTagRunRequest
{
    public required string LibraryId { get; init; }

    public string Scope { get; init; } = AutoTagScopes.Untagged;

    public string Model { get; init; } = AutoTagModels.Flash;

    public int MaxImages { get; init; } = 300;

    public decimal MaxBudgetCny { get; init; } = 5m;

    public bool ExternalProcessingConfirmed { get; init; }

    public IReadOnlyDictionary<string, object?> ToParameters() =>
        AutoTagParameterFactory.Create(
            LibraryId,
            Scope,
            Model,
            MaxImages,
            MaxBudgetCny,
            ExternalProcessingConfirmed
        );
}

public sealed class IndexAndAutoTagRequest
{
    public required string LibraryId { get; init; }

    public string? Folder { get; init; }

    public bool Recursive { get; init; } = true;

    public bool VerifyHash { get; init; }

    public IReadOnlyList<string>? Tags { get; init; }

    public string Model { get; init; } = AutoTagModels.Flash;

    public int MaxImages { get; init; } = 300;

    public decimal MaxBudgetCny { get; init; } = 5m;

    public bool ExternalProcessingConfirmed { get; init; }

    public IReadOnlyDictionary<string, object?> ToParameters()
    {
        var parameters = new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["library_id"] = LibraryId,
            ["recursive"] = Recursive,
            ["verify_hash"] = VerifyHash,
            ["tags"] = Tags,
            ["model"] = Model,
            ["max_images"] = MaxImages,
            ["max_budget_cny"] = MaxBudgetCny,
            ["external_processing_confirmed"] = ExternalProcessingConfirmed,
        };
        if (!string.IsNullOrWhiteSpace(Folder))
        {
            parameters["folder"] = Folder.Trim();
        }
        return parameters;
    }
}

public sealed class AutoTagPendingRequest
{
    public required string LibraryId { get; init; }

    public int Offset { get; init; }

    public int Limit { get; init; } = 100;

    public AutoTagReviewFilters Filters { get; init; } = new();

    public IReadOnlyDictionary<string, object?> ToParameters() =>
        new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["library_id"] = LibraryId,
            ["offset"] = Offset,
            ["limit"] = Limit,
            ["filters"] = Filters.ToParameters(),
        };
}

public sealed class AutoTagReviewFilters
{
    public bool LatestIndexOnly { get; init; }

    public string Character { get; init; } = string.Empty;

    public string Work { get; init; } = string.Empty;

    public string Action { get; init; } = string.Empty;

    public string Expression { get; init; } = string.Empty;

    public string ReviewState { get; init; } = string.Empty;

    public bool IsActive =>
        LatestIndexOnly ||
        !string.IsNullOrWhiteSpace(Character) ||
        !string.IsNullOrWhiteSpace(Work) ||
        !string.IsNullOrWhiteSpace(Action) ||
        !string.IsNullOrWhiteSpace(Expression) ||
        (!string.IsNullOrWhiteSpace(ReviewState) &&
         !string.Equals(ReviewState, "all", StringComparison.OrdinalIgnoreCase));

    public IReadOnlyDictionary<string, object?> ToParameters()
    {
        var parameters = new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["latest_index_only"] = LatestIndexOnly,
        };
        AddIfPresent(parameters, "character", Character);
        AddIfPresent(parameters, "work", Work);
        AddIfPresent(parameters, "action", Action);
        AddIfPresent(parameters, "expression", Expression);
        AddIfPresent(parameters, "review_state", ReviewState);
        return parameters;
    }

    private static void AddIfPresent(
        IDictionary<string, object?> parameters,
        string key,
        string value)
    {
        if (!string.IsNullOrWhiteSpace(value))
        {
            parameters[key] = value.Trim();
        }
    }
}

public sealed class AutoTagReviewRequest
{
    public required string LibraryId { get; init; }

    public IReadOnlyList<AutoTagReviewDecision> Decisions { get; init; } = [];

    public IReadOnlyDictionary<string, object?> ToParameters() =>
        new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["library_id"] = LibraryId,
            ["decisions"] = Decisions,
        };
}

public sealed class AutoTagReviewDecision
{
    [JsonPropertyName("proposal_id")]
    public required string ProposalId { get; init; }

    [JsonPropertyName("decision")]
    public required string Decision { get; init; }

    [JsonPropertyName("accepted_tags")]
    public IReadOnlyList<string> AcceptedTags { get; init; } = [];

    [JsonPropertyName("confirmed_identity_tags")]
    public IReadOnlyList<string> ConfirmedIdentityTags { get; init; } = [];
}

public sealed class AutoTagBatchReviewRequest
{
    public required string LibraryId { get; init; }

    public IReadOnlyList<string> ProposalIds { get; init; } = [];

    public IReadOnlyDictionary<string, IReadOnlyList<string>> AcceptedTagsByProposal { get; init; } =
        new Dictionary<string, IReadOnlyList<string>>(StringComparer.Ordinal);

    // This is intentionally always true. Identity tags require per-item confirmation and
    // must never pass through the high-throughput batch path.
    public bool ExcludeIdentityTags { get; init; } = true;

    public IReadOnlyDictionary<string, object?> ToParameters() =>
        new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["library_id"] = LibraryId,
            ["proposal_ids"] = ProposalIds,
            ["accepted_tags_by_proposal"] = AcceptedTagsByProposal,
            ["exclude_identity_tags"] = true,
        };
}

public sealed class AutoTagReviewUndoRequest
{
    public required string LibraryId { get; init; }

    public IReadOnlyDictionary<string, object?> ToParameters() =>
        new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["library_id"] = LibraryId,
        };
}

public sealed class TagAliasListRequest
{
    public required string LibraryId { get; init; }

    public IReadOnlyDictionary<string, object?> ToParameters() =>
        new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["library_id"] = LibraryId,
        };
}

public sealed class TagAliasUpsertRequest
{
    public required string LibraryId { get; init; }

    public required string CanonicalName { get; init; }

    public IReadOnlyList<string> Aliases { get; init; } = [];

    public IReadOnlyDictionary<string, object?> ToParameters() =>
        new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["library_id"] = LibraryId,
            ["canonical_name"] = CanonicalName,
            ["aliases"] = Aliases,
        };
}

public sealed class TagAliasDeleteRequest
{
    public required string LibraryId { get; init; }

    public required string CanonicalName { get; init; }

    public IReadOnlyDictionary<string, object?> ToParameters() =>
        new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["library_id"] = LibraryId,
            ["canonical_name"] = CanonicalName,
        };
}

public sealed class FolderTagBackfillRequest
{
    public required string LibraryId { get; init; }

    public bool Recursive { get; init; } = true;

    public bool VerifyHash { get; init; }

    public IReadOnlyDictionary<string, object?> ToParameters() =>
        new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["library_id"] = LibraryId,
            ["recursive"] = Recursive,
            ["verify_hash"] = VerifyHash,
        };
}

public sealed class MetadataBackfillRequest
{
    public required string LibraryId { get; init; }

    public int MaxImages { get; init; } = 300;

    public IReadOnlyDictionary<string, object?> ToParameters() =>
        new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["library_id"] = LibraryId,
            ["max_images"] = MaxImages,
        };
}

public sealed class MetadataBackfillResult
{
    [JsonPropertyName("scanned")]
    public int Scanned { get; init; }

    [JsonPropertyName("selected")]
    public int Selected { get; init; }

    [JsonPropertyName("succeeded")]
    public int Succeeded { get; init; }

    [JsonPropertyName("failed")]
    public int Failed { get; init; }

    [JsonPropertyName("remaining")]
    public int Remaining { get; init; }

    [JsonPropertyName("already_current")]
    public int AlreadyCurrent { get; init; }

    [JsonPropertyName("skipped_empty")]
    public int SkippedEmpty { get; init; }

    [JsonPropertyName("api_requests")]
    public int ApiRequests { get; init; }
}

public sealed class AutoTagEstimateResult
{
    [JsonPropertyName("candidate_count")]
    public int CandidateCount { get; init; }

    [JsonPropertyName("cached_count")]
    public int CachedCount { get; init; }

    [JsonPropertyName("api_request_count")]
    public int ApiRequestCount { get; init; }

    [JsonPropertyName("estimated_cost_cny")]
    public decimal EstimatedCostCny { get; init; }

    [JsonPropertyName("folder_tag_preview")]
    public IReadOnlyList<FolderTagPreview> FolderTagPreview { get; init; } = [];
}

public sealed class AutoTagRunResult
{
    [JsonPropertyName("processed")]
    public int Processed { get; init; }

    [JsonPropertyName("cached")]
    public int Cached { get; init; }

    [JsonPropertyName("succeeded")]
    public int Succeeded { get; init; }

    [JsonPropertyName("failed")]
    public int Failed { get; init; }

    [JsonPropertyName("stopped_reason")]
    public string? StoppedReason { get; init; }

    [JsonPropertyName("actual_cost_cny")]
    public decimal ActualCostCny { get; init; }

    [JsonPropertyName("proposals")]
    public IReadOnlyList<AutoTagProposal> Proposals { get; init; } = [];
}

// The automatic-tagging job can represent thousands of proposals. The desktop only
// needs these aggregate fields before it fetches a bounded pending-review page.
public sealed class AutoTagRunSummaryResult
{
    [JsonPropertyName("processed")]
    public int Processed { get; init; }

    [JsonPropertyName("cached")]
    public int Cached { get; init; }

    [JsonPropertyName("succeeded")]
    public int Succeeded { get; init; }

    [JsonPropertyName("failed")]
    public int Failed { get; init; }

    [JsonPropertyName("stopped_reason")]
    public string? StoppedReason { get; init; }

    [JsonPropertyName("actual_cost_cny")]
    public decimal ActualCostCny { get; init; }
}

public sealed class AutoTagPendingResult
{
    [JsonPropertyName("pending_count")]
    public int PendingCount { get; init; }

    [JsonPropertyName("offset")]
    public int Offset { get; init; }

    [JsonPropertyName("limit")]
    public int Limit { get; init; }

    [JsonPropertyName("proposals")]
    public IReadOnlyList<AutoTagProposal> Proposals { get; init; } = [];

    [JsonPropertyName("has_more")]
    public bool HasMore { get; init; }

    [JsonPropertyName("undo_available")]
    public bool UndoAvailable { get; init; }
}

public sealed class AutoTagReviewResult
{
    [JsonPropertyName("accepted")]
    public int Accepted { get; init; }

    [JsonPropertyName("rejected")]
    public int Rejected { get; init; }

    [JsonPropertyName("updated")]
    public int Updated { get; init; }

    [JsonPropertyName("undo_available")]
    public bool UndoAvailable { get; init; }
}

public sealed class AutoTagBatchReviewResult
{
    [JsonPropertyName("accepted")]
    public int Accepted { get; init; }

    [JsonPropertyName("updated")]
    public int Updated { get; init; }

    [JsonPropertyName("identity_excluded_count")]
    public int IdentityExcludedCount { get; init; }

    [JsonPropertyName("identity_excluded")]
    public int IdentityExcluded { get; init; }

    [JsonPropertyName("identity_exclusions")]
    public IReadOnlyList<AutoTagIdentityExclusion> IdentityExclusions { get; init; } = [];

    [JsonIgnore]
    public int EffectiveIdentityExcludedCount => Math.Max(
        Math.Max(IdentityExcluded, IdentityExcludedCount),
        IdentityExclusions.Sum(item => item.Tags.Count)
    );

    [JsonPropertyName("batch_id")]
    public string BatchId { get; init; } = string.Empty;

    [JsonPropertyName("undo_available")]
    public bool UndoAvailable { get; init; }
}

public sealed class AutoTagIdentityExclusion
{
    [JsonPropertyName("proposal_id")]
    public string ProposalId { get; init; } = string.Empty;

    [JsonPropertyName("tags")]
    public IReadOnlyList<string> Tags { get; init; } = [];
}

public sealed class AutoTagReviewUndoResult
{
    [JsonPropertyName("undone")]
    public bool Undone { get; init; }

    [JsonPropertyName("updated")]
    public int Updated { get; init; }

    [JsonPropertyName("batch_id")]
    public string BatchId { get; init; } = string.Empty;

    [JsonPropertyName("undo_available")]
    public bool UndoAvailable { get; init; }
}

public sealed class TagAliasListResult
{
    [JsonPropertyName("aliases")]
    public IReadOnlyList<TagAliasEntry> Aliases { get; init; } = [];

    // Accept the transitional response name so preview builds remain compatible.
    [JsonPropertyName("items")]
    public IReadOnlyList<TagAliasEntry> Items { get; init; } = [];

    [JsonIgnore]
    public IReadOnlyList<TagAliasEntry> EffectiveAliases => Aliases.Count > 0 ? Aliases : Items;
}

public sealed class TagAliasMutationResult
{
    [JsonPropertyName("updated")]
    public bool Updated { get; init; }

    [JsonPropertyName("deleted")]
    public bool Deleted { get; init; }

    [JsonPropertyName("entry")]
    public TagAliasEntry? Entry { get; init; }
}

public sealed class TagAliasEntry
{
    [JsonPropertyName("canonical_name")]
    public string CanonicalName { get; init; } = string.Empty;

    [JsonPropertyName("canonical")]
    public string LegacyCanonicalName { get; init; } = string.Empty;

    [JsonPropertyName("aliases")]
    public IReadOnlyList<string> Aliases { get; init; } = [];

    [JsonIgnore]
    public string EffectiveCanonicalName => string.IsNullOrWhiteSpace(CanonicalName)
        ? LegacyCanonicalName
        : CanonicalName;

    [JsonIgnore]
    public string AliasesText => string.Join(" · ", Aliases);

    [JsonIgnore]
    public string EditAliasesText => string.Join(" ", Aliases);

    [JsonIgnore]
    public string DisplayText => Aliases.Count == 0
        ? EffectiveCanonicalName
        : $"{EffectiveCanonicalName} ← {string.Join(" / ", Aliases)}";
}

public sealed class FolderTagPreview
{
    [JsonPropertyName("relative_path")]
    public string RelativePath { get; init; } = string.Empty;

    [JsonPropertyName("folder_name")]
    public string FolderName { get; init; } = string.Empty;

    [JsonPropertyName("original_folder")]
    public string OriginalFolder { get; init; } = string.Empty;

    [JsonPropertyName("cleaned_tag")]
    public string CleanedTag { get; init; } = string.Empty;

    [JsonPropertyName("folder_tags")]
    public IReadOnlyList<string> FolderTags { get; init; } = [];

    [JsonIgnore]
    public string SourceText => string.IsNullOrWhiteSpace(FolderName)
        ? string.IsNullOrWhiteSpace(OriginalFolder) ? RelativePath : OriginalFolder
        : FolderName;

    [JsonIgnore]
    public string TagText => !string.IsNullOrWhiteSpace(CleanedTag)
        ? CleanedTag
        : FolderTags.Count == 0 ? "不生成标签" : string.Join(" · ", FolderTags);
}

public sealed class AutoTagProposal
{
    private static readonly IReadOnlyDictionary<string, string> FieldLabels =
        new Dictionary<string, string>(StringComparer.Ordinal)
        {
            ["content_domain"] = "内容",
            ["people_count"] = "人数",
            ["shot_type"] = "景别",
            ["pose"] = "姿态",
            ["action"] = "动作",
            ["expression"] = "神态",
            ["gaze"] = "视线",
            ["hair_color"] = "发色",
            ["hair_style"] = "发型",
            ["clothing"] = "服装",
            ["legwear"] = "腿部穿着",
            ["accessory"] = "配饰",
            ["prop"] = "道具",
            ["scene"] = "场景",
            ["camera_angle"] = "视角",
            ["lighting"] = "光线",
            ["photography_style"] = "风格",
        };

    private static readonly IReadOnlyDictionary<string, string> EntityLabels =
        new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["real_person"] = "真人",
            ["cosplayer"] = "Cosplayer",
            ["character"] = "角色",
            ["work"] = "作品",
        };

    [JsonPropertyName("proposal_id")]
    public string ProposalId { get; init; } = string.Empty;

    [JsonPropertyName("annotation_id")]
    public string AnnotationId { get; init; } = string.Empty;

    [JsonPropertyName("image_id")]
    public string ImageId { get; init; } = string.Empty;

    [JsonPropertyName("relative_path")]
    public string RelativePath { get; init; } = string.Empty;

    [JsonPropertyName("source_path")]
    public string SourcePath { get; init; } = string.Empty;

    [JsonPropertyName("description")]
    public string Description { get; init; } = string.Empty;

    [JsonPropertyName("tags")]
    public IReadOnlyList<string> Tags { get; init; } = [];

    [JsonPropertyName("suggested_tags")]
    public IReadOnlyList<string> SuggestedTags { get; init; } = [];

    [JsonPropertyName("proposed_tags")]
    public IReadOnlyList<string> ProposedTags { get; init; } = [];

    [JsonPropertyName("folder_tags")]
    public IReadOnlyList<string> FolderTags { get; init; } = [];

    [JsonPropertyName("manual_tags")]
    public IReadOnlyList<string> ManualTags { get; init; } = [];

    [JsonPropertyName("status")]
    public string Status { get; init; } = string.Empty;

    [JsonPropertyName("error")]
    public string Error { get; init; } = string.Empty;

    [JsonPropertyName("failure_category")]
    public string FailureCategory { get; init; } = string.Empty;

    [JsonPropertyName("existing_tags")]
    public IReadOnlyList<string> ExistingTags { get; init; } = [];

    [JsonPropertyName("low_risk_tags")]
    public IReadOnlyList<string> LowRiskTags { get; init; } = [];

    [JsonPropertyName("identity_tags")]
    public IReadOnlyList<string> IdentityTags { get; init; } = [];

    [JsonPropertyName("tag_details")]
    public IReadOnlyList<AutoTagTagDetail> TagDetails { get; init; } = [];

    [JsonPropertyName("fields")]
    public IReadOnlyDictionary<string, AutoTagStableField> Fields { get; init; } =
        new Dictionary<string, AutoTagStableField>(StringComparer.Ordinal);

    // Some transitional schema-v2 payloads used the internal vision-client name.
    [JsonPropertyName("stable_fields")]
    public IReadOnlyDictionary<string, AutoTagStableField> LegacyStableFields { get; init; } =
        new Dictionary<string, AutoTagStableField>(StringComparer.Ordinal);

    [JsonPropertyName("entities")]
    public AutoTagEntities Entities { get; init; } = new();

    [JsonPropertyName("review_required")]
    public bool ReviewRequired { get; init; }

    [JsonPropertyName("requires_review")]
    public bool LegacyRequiresReview { get; init; }

    [JsonPropertyName("review_reasons")]
    public IReadOnlyList<string> ReviewReasons { get; init; } = [];

    [JsonPropertyName("model_trace")]
    public IReadOnlyList<string> ModelTrace { get; init; } = [];

    [JsonPropertyName("model_chain")]
    public IReadOnlyList<string> LegacyModelTrace { get; init; } = [];

    [JsonPropertyName("resolved_model")]
    public string ResolvedModel { get; init; } = string.Empty;

    [JsonPropertyName("escalated")]
    public bool Escalated { get; init; }

    [JsonPropertyName("plus_recommended")]
    public bool PlusRecommended { get; init; }

    [JsonIgnore]
    public string StableId => FirstNonEmpty(ProposalId, AnnotationId, ImageId, RelativePath);

    [JsonIgnore]
    public IReadOnlyList<string> RecommendedTags => ProposedTags
        .Where(tag => !string.IsNullOrWhiteSpace(tag))
        .Distinct(StringComparer.Ordinal)
        .ToList();

    [JsonIgnore]
    public IReadOnlyList<string> EffectiveIdentityTags => IdentityTags
        .Concat(TagDetails
            .Where(detail =>
                detail.RequiresIndividualConfirmation ||
                string.Equals(detail.Risk, "conflict", StringComparison.OrdinalIgnoreCase)
            )
            .Select(detail => detail.Tag))
        .Where(tag => !string.IsNullOrWhiteSpace(tag))
        .Distinct(StringComparer.Ordinal)
        .ToList();

    [JsonIgnore]
    public IReadOnlyList<string> BatchSafeLowRiskTags
    {
        get
        {
            var identityTags = EffectiveIdentityTags.ToHashSet(StringComparer.Ordinal);
            var existingTags = ExistingTags.ToHashSet(StringComparer.Ordinal);
            return LowRiskTags
                .Concat(TagDetails
                    .Where(detail => detail.IsLowRisk && !detail.IsIdentityTag)
                    .Select(detail => detail.Tag))
                .Where(tag => !string.IsNullOrWhiteSpace(tag))
                .Where(tag => !identityTags.Contains(tag) && !existingTags.Contains(tag))
                .Distinct(StringComparer.Ordinal)
                .ToList();
        }
    }

    [JsonIgnore]
    public IReadOnlyList<string> ReviewDraftTags
    {
        get
        {
            var safeTags = BatchSafeLowRiskTags;
            if (safeTags.Count > 0)
            {
                return safeTags;
            }
            var identityTags = EffectiveIdentityTags.ToHashSet(StringComparer.Ordinal);
            var existingTags = ExistingTags.ToHashSet(StringComparer.Ordinal);
            return RecommendedTags
                .Where(tag => !identityTags.Contains(tag) && !existingTags.Contains(tag))
                .ToList();
        }
    }

    [JsonIgnore]
    public IReadOnlyList<AutoTagTagDetail> TagDetailsForDisplay
    {
        get
        {
            if (TagDetails.Count > 0)
            {
                return TagDetails
                    .Where(detail => !string.IsNullOrWhiteSpace(detail.Tag))
                    .GroupBy(
                        detail => (detail.Tag, detail.Source),
                        EqualityComparer<(string Tag, string Source)>.Default
                    )
                    .Select(group => group.First())
                    .ToList();
            }

            var identityTags = EffectiveIdentityTags.ToHashSet(StringComparer.Ordinal);
            return ExistingTags
                .Where(tag => !string.IsNullOrWhiteSpace(tag))
                .Select(tag => new AutoTagTagDetail
                {
                    Tag = tag,
                    Source = "manual",
                    AlreadyPresent = true,
                })
                .Concat(FolderTags
                    .Where(tag => !string.IsNullOrWhiteSpace(tag))
                    .Select(tag => new AutoTagTagDetail
                    {
                        Tag = tag,
                        Source = "folder",
                        AlreadyPresent = ExistingTags.Contains(tag, StringComparer.Ordinal),
                    }))
                .Concat(RecommendedTags.Select(tag => new AutoTagTagDetail
                {
                    Tag = tag,
                    Source = identityTags.Contains(tag) ? "entity" : "field",
                    Risk = identityTags.Contains(tag) ? "identity" : string.Empty,
                    Identity = identityTags.Contains(tag),
                    AlreadyPresent = ExistingTags.Contains(tag, StringComparer.Ordinal),
                }))
                .GroupBy(
                    detail => (detail.Tag, detail.Source),
                    EqualityComparer<(string Tag, string Source)>.Default
                )
                .Select(group => group.First())
                .ToList();
        }
    }

    [JsonIgnore]
    public IReadOnlyDictionary<string, AutoTagStableField> EffectiveFields =>
        Fields.Count > 0 ? Fields : LegacyStableFields;

    [JsonIgnore]
    public bool HasStableFields => EffectiveFields.Values.Any(field => field.Values.Count > 0);

    [JsonIgnore]
    public string StableFieldSummary
    {
        get
        {
            var values = EffectiveFields
                .Where(pair => pair.Value.Values.Count > 0)
                .Select(pair => StableFieldText(pair.Key, pair.Value))
                .Where(value => !string.IsNullOrWhiteSpace(value))
                .ToList();
            return values.Count == 0
                ? "旧建议未包含稳定字段"
                : string.Join(" · ", values);
        }
    }

    [JsonIgnore]
    public string StableFieldConfidenceText
    {
        get
        {
            var confidences = EffectiveFields.Values
                .Where(field => field.Values.Count > 0)
                .Select(field => Math.Clamp(field.Confidence, 0d, 1d))
                .ToList();
            return confidences.Count == 0
                ? "最低字段置信度：未记录"
                : $"最低字段置信度：{confidences.Min():P0}";
        }
    }

    [JsonIgnore]
    public string EntitySummary
    {
        get
        {
            var values = new[]
            {
                EntityText("真人", Entities.RealPerson),
                EntityText("Cosplayer", Entities.Cosplayer),
                EntityText("角色", Entities.Character),
                EntityText("作品", Entities.Work),
            }.Where(value => value is not null);
            return string.Join(" · ", values!);
        }
    }

    [JsonIgnore]
    public bool EffectiveReviewRequired => ReviewRequired || LegacyRequiresReview;

    [JsonIgnore]
    public bool HasReviewReasons => ReviewReasons.Count > 0 || EffectiveReviewRequired;

    [JsonIgnore]
    public string ReviewReasonsText
    {
        get
        {
            if (ReviewReasons.Count == 0)
            {
                return EffectiveReviewRequired
                    ? "审核原因：需要人工确认（后端未提供具体原因）"
                    : "审核原因：无额外风险";
            }
            var reasons = ReviewReasons
                .Where(reason => !string.IsNullOrWhiteSpace(reason))
                .Select(LocalizeReviewReason)
                .Distinct(StringComparer.Ordinal)
                .ToList();
            return reasons.Count == 0
                ? "审核原因：需要人工确认"
                : "审核原因：" + string.Join("；", reasons);
        }
    }

    [JsonIgnore]
    public string ModelPolicySummary
    {
        get
        {
            var trace = (ModelTrace.Count > 0 ? ModelTrace : LegacyModelTrace)
                .Where(model => !string.IsNullOrWhiteSpace(model))
                .Select(LocalizeModel)
                .ToList();
            var resolved = LocalizeModel(ResolvedModel);
            if (trace.Count == 0 && !string.IsNullOrWhiteSpace(resolved))
            {
                trace.Add(resolved);
            }
            if (trace.Count == 0)
            {
                return PlusRecommended
                    ? "模型链路：旧建议未记录；建议使用 Plus 复核"
                    : "模型链路：旧建议未记录";
            }

            var summary = "模型链路：" + string.Join(" → ", trace);
            if (!string.IsNullOrWhiteSpace(resolved))
            {
                summary += $"；最终 {resolved}";
            }
            if (WasEscalated(trace))
            {
                return summary + "（冲突或低置信时自动升级）";
            }
            if (trace.All(model => string.Equals(model, "Plus", StringComparison.Ordinal)))
            {
                return summary + "（手动全程 Plus）";
            }
            if (PlusRecommended)
            {
                return trace.All(model => model is "Flash" or "Plus")
                    ? summary + "（建议使用 Plus 复核）"
                    : summary + "（建议使用升级模型复核）";
            }
            return trace.All(model => string.Equals(model, "Flash", StringComparison.Ordinal))
                ? summary + "（Flash 完成，未触发升级）"
                : summary + "（主模型完成，未触发升级）";
        }
    }

    private static string StableFieldText(string fieldName, AutoTagStableField field)
    {
        var label = FieldLabels.TryGetValue(fieldName, out var localized)
            ? localized
            : fieldName;
        var values = field.Labels
            .Where(value => !string.IsNullOrWhiteSpace(value))
            .Distinct(StringComparer.Ordinal)
            .ToList();
        return values.Count == 0
            ? $"{label}：已识别 {field.Values.Count} 项（中文标签缺失）"
            : $"{label}：{string.Join(" / ", values)}";
    }

    private static string? EntityText(
        string label,
        IReadOnlyList<AutoTagEntity> entities)
    {
        var entity = entities
            .OrderBy(value => EntityStatePriority(value.State))
            .ThenByDescending(value => value.Confidence ?? 0d)
            .FirstOrDefault();
        if (entity is null)
        {
            return null;
        }
        var state = LocalizeEntityState(entity.State);
        return string.IsNullOrWhiteSpace(entity.Name)
            ? $"{label}：{state}"
            : $"{label}：{entity.Name}（{state}）";
    }

    private static int EntityStatePriority(string state) => state.ToLowerInvariant() switch
    {
        "confirmed" => 0,
        "suggested" => 1,
        "conflict" => 2,
        "unable_to_confirm" or "unknown" => 3,
        "not_applicable" => 4,
        _ => 5,
    };

    private static string LocalizeEntityState(string state) => state.ToLowerInvariant() switch
    {
        "confirmed" => "确认",
        "suggested" => "待确认",
        "conflict" => "冲突",
        "unable_to_confirm" or "unknown" => "无法确认",
        "not_applicable" => "不适用",
        _ => "状态未知",
    };

    private static string LocalizeReviewReason(string reason)
    {
        var parts = reason.Split(':', StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length >= 3 && string.Equals(parts[0], "field", StringComparison.Ordinal))
        {
            var field = FieldLabels.TryGetValue(parts[1], out var label) ? label : parts[1];
            return parts[2] switch
            {
                "low_confidence" => $"{field}置信度较低",
                "conflict" => $"{field}结果存在冲突",
                "overflow" => $"{field}候选过多，已按规则裁剪",
                "invalid" => $"{field}返回值无效，已忽略",
                _ => $"{field}需要确认",
            };
        }
        if (parts.Length >= 3 && string.Equals(parts[0], "entity", StringComparison.Ordinal))
        {
            var entity = EntityLabels.TryGetValue(parts[1], out var label)
                ? label
                : parts[1];
            return parts[2] switch
            {
                "conflict" => $"{entity}信息存在冲突",
                "suggested" => $"{entity}为模型推断，待确认",
                "unable_to_confirm" => $"{entity}无法确认",
                "low_confidence" => $"{entity}置信度较低",
                "context_mismatch" => $"{entity}依据与当前人工标签或目录不一致",
                "context_changed" => $"{entity}依据已变化，需要复核",
                _ => $"{entity}需要确认",
            };
        }
        if (parts.Length >= 2)
        {
            var entity = EntityLabels.TryGetValue(parts[1], out var label)
                ? label
                : parts[1];
            return parts[0] switch
            {
                "entity_conflict" => $"{entity}信息存在冲突",
                "unconfirmed_entity" => $"{entity}尚未确认",
                "low_confidence_entity" => $"{entity}置信度较低",
                "missing_explicit_identity_evidence" => $"{entity}缺少明确身份依据",
                _ => reason.Replace('_', ' '),
            };
        }
        return reason switch
        {
            "legacy_annotation" => "旧版建议需要人工复核",
            "model_request_failed" => "模型请求失败",
            "plus_budget_exhausted" => "预算不足，已保留 Flash 结果等待审核",
            "plus_request_failed" => "Plus 请求失败，已保留 Flash 结果等待审核",
            "plus_cached_failure" => "Plus 失败记录已复用，已保留 Flash 结果等待审核",
            _ => reason.Replace('_', ' '),
        };
    }

    private static string LocalizeModel(string model)
    {
        if (string.IsNullOrWhiteSpace(model))
        {
            return string.Empty;
        }
        return model.Trim().ToLowerInvariant() switch
        {
            AutoTagModels.Flash => "Flash",
            AutoTagModels.Plus => "Plus",
            _ => model.Trim(),
        };
    }

    private bool WasEscalated(IReadOnlyList<string> localizedTrace) =>
        Escalated ||
        localizedTrace.Distinct(StringComparer.Ordinal).Skip(1).Any();

    private static string FirstNonEmpty(params string[] values) =>
        values.FirstOrDefault(value => !string.IsNullOrWhiteSpace(value)) ?? string.Empty;
}

public sealed class AutoTagStableField
{
    [JsonPropertyName("values")]
    public IReadOnlyList<string> Values { get; init; } = [];

    [JsonPropertyName("labels")]
    public IReadOnlyList<string> Labels { get; init; } = [];

    [JsonPropertyName("confidence")]
    public double Confidence { get; init; }
}

public sealed class AutoTagEntities
{
    [JsonPropertyName("real_person")]
    [JsonConverter(typeof(AutoTagEntityListJsonConverter))]
    public IReadOnlyList<AutoTagEntity> RealPerson { get; init; } = [];

    [JsonPropertyName("cosplayer")]
    [JsonConverter(typeof(AutoTagEntityListJsonConverter))]
    public IReadOnlyList<AutoTagEntity> Cosplayer { get; init; } = [];

    [JsonPropertyName("character")]
    [JsonConverter(typeof(AutoTagEntityListJsonConverter))]
    public IReadOnlyList<AutoTagEntity> Character { get; init; } = [];

    [JsonPropertyName("work")]
    [JsonConverter(typeof(AutoTagEntityListJsonConverter))]
    public IReadOnlyList<AutoTagEntity> Work { get; init; } = [];
}

public sealed class AutoTagEntity
{
    [JsonPropertyName("name")]
    public string? Name { get; init; }

    [JsonPropertyName("state")]
    public string State { get; init; } = "unknown";

    [JsonPropertyName("evidence")]
    public IReadOnlyList<string> Evidence { get; init; } = [];

    [JsonPropertyName("evidence_text")]
    public string? EvidenceText { get; init; }

    [JsonPropertyName("confidence")]
    public double? Confidence { get; init; }
}

public sealed class AutoTagEntityListJsonConverter : JsonConverter<IReadOnlyList<AutoTagEntity>>
{
    public override IReadOnlyList<AutoTagEntity> Read(
        ref Utf8JsonReader reader,
        Type typeToConvert,
        JsonSerializerOptions options)
    {
        if (reader.TokenType == JsonTokenType.Null)
        {
            return [];
        }
        if (reader.TokenType == JsonTokenType.StartArray)
        {
            return JsonSerializer.Deserialize<List<AutoTagEntity>>(ref reader, options) ?? [];
        }
        if (reader.TokenType == JsonTokenType.StartObject)
        {
            var entity = JsonSerializer.Deserialize<AutoTagEntity>(ref reader, options);
            return entity is null ? [] : [entity];
        }
        throw new JsonException("Auto-tag entities must be an object or an array.");
    }

    public override void Write(
        Utf8JsonWriter writer,
        IReadOnlyList<AutoTagEntity> value,
        JsonSerializerOptions options)
    {
        writer.WriteStartArray();
        foreach (var entity in value)
        {
            JsonSerializer.Serialize(writer, entity, options);
        }
        writer.WriteEndArray();
    }
}

public sealed class AutoTagTagDetail
{
    [JsonPropertyName("tag")]
    public string Tag { get; init; } = string.Empty;

    [JsonPropertyName("source")]
    public string Source { get; init; } = string.Empty;

    [JsonPropertyName("sources")]
    public IReadOnlyList<string> Sources { get; init; } = [];

    [JsonPropertyName("source_label")]
    public string SourceLabel { get; init; } = string.Empty;

    [JsonPropertyName("risk")]
    public string Risk { get; init; } = string.Empty;

    [JsonPropertyName("identity")]
    public bool Identity { get; init; }

    [JsonPropertyName("field")]
    public string? Field { get; init; }

    [JsonPropertyName("entity_type")]
    public string? EntityType { get; init; }

    [JsonPropertyName("already_present")]
    public bool AlreadyPresent { get; init; }

    [JsonPropertyName("confirmation")]
    public string Confirmation { get; init; } = string.Empty;

    [JsonPropertyName("requires_individual_confirmation")]
    public bool RequiresIndividualConfirmation { get; init; }

    [JsonPropertyName("confidence")]
    public double? Confidence { get; init; }

    [JsonIgnore]
    public bool IsIdentityTag =>
        Identity ||
        RequiresIndividualConfirmation ||
        string.Equals(Source, "entity", StringComparison.OrdinalIgnoreCase) ||
        string.Equals(Source, "model_entity", StringComparison.OrdinalIgnoreCase) ||
        Sources.Any(source =>
            string.Equals(source, "entity", StringComparison.OrdinalIgnoreCase) ||
            string.Equals(source, "model_entity", StringComparison.OrdinalIgnoreCase)
        ) ||
        string.Equals(Risk, "identity", StringComparison.OrdinalIgnoreCase);

    [JsonIgnore]
    public bool IsLowRisk =>
        string.Equals(Risk, "low", StringComparison.OrdinalIgnoreCase) ||
        string.Equals(Risk, "low_risk", StringComparison.OrdinalIgnoreCase);

    [JsonIgnore]
    public string SourceText
    {
        get
        {
            if (!string.IsNullOrWhiteSpace(SourceLabel))
            {
                return SourceLabel.Trim();
            }
            var localizedSources = Sources
                .Where(value => !string.IsNullOrWhiteSpace(value))
                .Select(LocalizeSource)
                .Distinct(StringComparer.Ordinal)
                .ToList();
            if (localizedSources.Count > 1)
            {
                return string.Join("+", localizedSources);
            }
            var source = !string.IsNullOrWhiteSpace(Source)
                ? Source
                : Sources.FirstOrDefault() ?? string.Empty;
            return LocalizeSource(source);
        }
    }

    [JsonIgnore]
    public string StateText => AlreadyPresent
        ? "已有"
        : IsIdentityTag ? "身份待确认" : IsLowRisk ? "低风险新增" : "建议";

    [JsonIgnore]
    public string DisplayText => $"{Tag} · {SourceText} · {StateText}";

    private static string LocalizeSource(string source) => source.Trim().ToLowerInvariant() switch
    {
        "manual" => "人工",
        "folder" => "文件夹",
        "accepted_auto" => "已接受模型",
        "field" or "model_field" => "模型字段",
        "entity" or "model_entity" => "模型身份",
        "alias" => "别名",
        "existing" => "现有",
        _ => string.IsNullOrWhiteSpace(source) ? "来源未记录" : source.Trim(),
    };
}

public sealed class AutoTagReviewItem : INotifyPropertyChanged
{
    private string _decision = "pending";
    private string _editedTagsText = string.Empty;
    private bool _isBatchSelected;
    private bool _isDetailsExpanded;
    private BitmapSource? _thumbnail;
    private int _thumbnailLoadStarted;

    public required string ProposalId { get; init; }

    public required string RelativePath { get; init; }

    public required string SourcePath { get; init; }

    public required string Description { get; init; }

    public required string ProposedTagsText { get; init; }

    public required string FolderTagsText { get; init; }

    public required string ExistingTagsText { get; init; }

    public required string LowRiskTagsText { get; init; }

    public required string DifferenceSummary { get; init; }

    public required string EntitySummary { get; init; }

    public required string StableFieldSummary { get; init; }

    public required string StableFieldConfidenceText { get; init; }

    public required string ModelPolicySummary { get; init; }

    public required string ReviewReasonsText { get; init; }

    public required string FailureText { get; init; }

    public bool IsFailed { get; init; }

    public bool HasFailure => !string.IsNullOrWhiteSpace(FailureText);

    public bool HasStableFields { get; init; }

    public bool HasReviewReasons { get; init; }

    public bool HasTagDetails => TagDetails.Count > 0;

    public bool HasIdentityTags => IdentityChoices.Count > 0;

    public bool CanBatchAccept => !IsFailed && BatchSafeTags.Count > 0;

    public BitmapSource? Thumbnail
    {
        get => _thumbnail;
        set
        {
            if (ReferenceEquals(_thumbnail, value))
            {
                return;
            }
            _thumbnail = value;
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(Thumbnail)));
        }
    }

    public IReadOnlyList<AutoTagTagDetail> TagDetails { get; init; } = [];

    public IReadOnlyList<string> BatchSafeTags { get; init; } = [];

    public IReadOnlySet<string> IdentityTagSet { get; init; } =
        new HashSet<string>(StringComparer.Ordinal);

    public ObservableCollection<AutoTagIdentityChoice> IdentityChoices { get; init; } = [];

    public required string InitialEditedTagsText { get; init; }

    public string EditedTagsText
    {
        get => _editedTagsText;
        set => SetField(ref _editedTagsText, value);
    }

    public string Decision
    {
        get => _decision;
        set => SetField(ref _decision, value);
    }

    public bool IsBatchSelected
    {
        get => _isBatchSelected;
        set => SetField(ref _isBatchSelected, value && CanBatchAccept);
    }

    public bool IsDetailsExpanded
    {
        get => _isDetailsExpanded;
        set
        {
            if (_isDetailsExpanded == value)
            {
                return;
            }
            _isDetailsExpanded = value;
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(IsDetailsExpanded)));
        }
    }

    [JsonIgnore]
    public bool HasUnsavedChanges =>
        !string.Equals(Decision, "pending", StringComparison.Ordinal) ||
        !string.Equals(EditedTagsText, InitialEditedTagsText, StringComparison.Ordinal) ||
        IsBatchSelected ||
        IdentityChoices.Any(choice => choice.IsConfirmed);

    public event PropertyChangedEventHandler? PropertyChanged;

    public static AutoTagReviewItem FromProposal(AutoTagProposal proposal)
    {
        ArgumentNullException.ThrowIfNull(proposal);
        var recommendedTags = proposal.RecommendedTags;
        var draftTags = proposal.ReviewDraftTags;
        var existingTags = proposal.ExistingTags
            .Where(tag => !string.IsNullOrWhiteSpace(tag))
            .Distinct(StringComparer.Ordinal)
            .ToList();
        var allIdentityTags = proposal.EffectiveIdentityTags
            .Where(tag => !string.IsNullOrWhiteSpace(tag))
            .Distinct(StringComparer.Ordinal)
            .ToList();
        var identityTags = allIdentityTags
            .Where(tag => !existingTags.Contains(tag, StringComparer.Ordinal))
            .ToList();
        var lowRiskTags = proposal.BatchSafeLowRiskTags;
        var entitySummary = proposal.EntitySummary;
        var isFailed = string.Equals(
            proposal.Status,
            "failed",
            StringComparison.OrdinalIgnoreCase
        );
        var editedTagsText = string.Join(
            " ",
            isFailed ? proposal.ManualTags : draftTags
        );
        var item = new AutoTagReviewItem
        {
            ProposalId = proposal.StableId,
            RelativePath = string.IsNullOrWhiteSpace(proposal.RelativePath)
                ? proposal.StableId
                : proposal.RelativePath,
            SourcePath = proposal.SourcePath,
            Description = proposal.Description,
            ProposedTagsText = recommendedTags.Count == 0
                ? "没有可自动预填的稳定标签"
                : string.Join(" · ", recommendedTags),
            FolderTagsText = proposal.FolderTags.Count == 0
                ? "无文件夹标签"
                : string.Join(" · ", proposal.FolderTags),
            ExistingTagsText = existingTags.Count == 0
                ? "无"
                : string.Join(" · ", existingTags),
            LowRiskTagsText = lowRiskTags.Count == 0
                ? "无可批量接受的新增标签"
                : string.Join(" · ", lowRiskTags),
            DifferenceSummary = BuildDifferenceSummary(existingTags, lowRiskTags, identityTags),
            EntitySummary = string.IsNullOrWhiteSpace(entitySummary)
                ? "未识别到可确认的身份、角色或作品"
                : entitySummary,
            StableFieldSummary = proposal.StableFieldSummary,
            StableFieldConfidenceText = proposal.StableFieldConfidenceText,
            ModelPolicySummary = proposal.ModelPolicySummary,
            ReviewReasonsText = proposal.ReviewReasonsText,
            FailureText = proposal.Error,
            IsFailed = isFailed,
            HasStableFields = proposal.HasStableFields,
            HasReviewReasons = proposal.HasReviewReasons,
            TagDetails = proposal.TagDetailsForDisplay,
            BatchSafeTags = lowRiskTags,
            IdentityTagSet = allIdentityTags.ToHashSet(StringComparer.Ordinal),
            IdentityChoices = new ObservableCollection<AutoTagIdentityChoice>(
                identityTags.Select(tag => new AutoTagIdentityChoice(tag, FindIdentityType(proposal, tag)))
            ),
            InitialEditedTagsText = editedTagsText,
            EditedTagsText = editedTagsText,
        };
        foreach (var choice in item.IdentityChoices)
        {
            choice.PropertyChanged += (_, args) =>
            {
                if (args.PropertyName == nameof(AutoTagIdentityChoice.IsConfirmed) &&
                    choice.IsConfirmed &&
                    item.Decision == "pending")
                {
                    item.Decision = "accept";
                }
                item.PropertyChanged?.Invoke(
                    item,
                    new PropertyChangedEventArgs(nameof(HasUnsavedChanges))
                );
            };
        }
        return item;
    }

    public IReadOnlyList<string> BuildAcceptedTags(Func<string, IReadOnlyList<string>> tokenParser)
    {
        ArgumentNullException.ThrowIfNull(tokenParser);
        if (IsFailed)
        {
            return tokenParser(EditedTagsText)
                .Where(tag => !string.IsNullOrWhiteSpace(tag))
                .Distinct(StringComparer.Ordinal)
                .ToList();
        }
        var ordinaryTags = tokenParser(EditedTagsText)
            .Where(tag => !IdentityTagSet.Contains(tag));
        return ordinaryTags
            .Concat(BuildConfirmedIdentityTags())
            .Where(tag => !string.IsNullOrWhiteSpace(tag))
            .Distinct(StringComparer.Ordinal)
            .ToList();
    }

    public IReadOnlyList<string> BuildConfirmedIdentityTags() => IdentityChoices
        .Where(choice => choice.IsConfirmed)
        .Select(choice => choice.Tag)
        .Distinct(StringComparer.Ordinal)
        .ToList();

    public IReadOnlyList<string> BuildBatchAcceptedTags() => BatchSafeTags
        .Where(tag => !IdentityTagSet.Contains(tag))
        .Distinct(StringComparer.Ordinal)
        .ToList();

    public bool TryBeginThumbnailLoad() =>
        !string.IsNullOrWhiteSpace(SourcePath) &&
        Interlocked.Exchange(ref _thumbnailLoadStarted, 1) == 0;

    private static string BuildDifferenceSummary(
        IReadOnlyList<string> existingTags,
        IReadOnlyList<string> lowRiskTags,
        IReadOnlyList<string> identityTags)
    {
        var parts = new List<string>();
        parts.Add(existingTags.Count == 0
            ? "现有标签为空"
            : $"保留现有 {existingTags.Count} 项");
        if (lowRiskTags.Count > 0)
        {
            parts.Add($"普通新增 {lowRiskTags.Count} 项");
        }
        if (identityTags.Count > 0)
        {
            parts.Add($"身份冲突待确认 {identityTags.Count} 项");
        }
        if (lowRiskTags.Count == 0 && identityTags.Count == 0)
        {
            parts.Add("没有新增建议");
        }
        return string.Join("；", parts);
    }

    private static string FindIdentityType(AutoTagProposal proposal, string tag)
    {
        var detail = proposal.TagDetails.FirstOrDefault(item =>
            string.Equals(item.Tag, tag, StringComparison.Ordinal)
        );
        return detail?.EntityType?.Trim().ToLowerInvariant() switch
        {
            "real_person" => "真人",
            "cosplayer" => "Cosplayer",
            "character" => "角色",
            "work" => "作品",
            _ => "身份",
        };
    }

    private void SetField(ref string field, string value, [CallerMemberName] string? name = null)
    {
        if (string.Equals(field, value, StringComparison.Ordinal))
        {
            return;
        }
        field = value;
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(HasUnsavedChanges)));
    }

    private void SetField(ref bool field, bool value, [CallerMemberName] string? name = null)
    {
        if (field == value)
        {
            return;
        }
        field = value;
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(HasUnsavedChanges)));
    }
}

public sealed class AutoTagIdentityChoice : INotifyPropertyChanged
{
    private bool _isConfirmed;

    public AutoTagIdentityChoice(string tag, string typeText)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(tag);
        Tag = tag;
        TypeText = string.IsNullOrWhiteSpace(typeText) ? "身份" : typeText;
    }

    public string Tag { get; }

    public string TypeText { get; }

    public string DisplayText => $"{TypeText}：{Tag}";

    public bool IsConfirmed
    {
        get => _isConfirmed;
        set
        {
            if (_isConfirmed == value)
            {
                return;
            }
            _isConfirmed = value;
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(IsConfirmed)));
        }
    }

    public event PropertyChangedEventHandler? PropertyChanged;
}

public static class BackendJobResultExtensions
{
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web)
    {
        PropertyNameCaseInsensitive = true,
    };

    public static T DeserializeResult<T>(this BackendJob job)
    {
        ArgumentNullException.ThrowIfNull(job);
        if (job.Result is not JsonElement result || result.ValueKind is JsonValueKind.Null)
        {
            throw new BackendProtocolException(
                $"Backend job '{job.Id}' did not contain a result payload."
            );
        }

        try
        {
            return result.Deserialize<T>(JsonOptions) ?? throw new BackendProtocolException(
                $"Backend job '{job.Id}' returned an empty {typeof(T).Name} result."
            );
        }
        catch (JsonException exception)
        {
            throw new BackendProtocolException(
                $"Backend job '{job.Id}' returned an invalid {typeof(T).Name} result: " +
                exception.Message
            );
        }
    }

    public static Task<T> DeserializeResultAsync<T>(
        this BackendJob job,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(job);
        return Task.Run(
            () =>
            {
                cancellationToken.ThrowIfCancellationRequested();
                return job.DeserializeResult<T>();
            },
            cancellationToken
        );
    }
}

internal static class AutoTagParameterFactory
{
    public static IReadOnlyDictionary<string, object?> Create(
        string libraryId,
        string scope,
        string model,
        int maxImages,
        decimal maxBudgetCny,
        bool externalProcessingConfirmed) =>
        new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["library_id"] = libraryId,
            ["scope"] = scope,
            ["model"] = model,
            ["max_images"] = maxImages,
            ["max_budget_cny"] = maxBudgetCny,
            ["external_processing_confirmed"] = externalProcessingConfirmed,
        };
}
