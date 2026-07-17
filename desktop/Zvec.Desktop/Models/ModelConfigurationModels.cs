using System.Text.Json.Serialization;

namespace Zvec.Desktop.Models;

public static class ModelRoles
{
    public const string Embedding = "embedding";
    public const string AutoTagPrimary = "auto_tag_primary";
    public const string AutoTagEscalation = "auto_tag_escalation";

    public static readonly IReadOnlySet<string> All = new HashSet<string>(
        [Embedding, AutoTagPrimary, AutoTagEscalation],
        StringComparer.Ordinal
    );
}

public static class ModelProtocols
{
    public const string MultimodalEmbedding = "dashscope_multimodal_embedding";
    public const string MultimodalConversation = "dashscope_multimodal_conversation";
}

public sealed class ModelConfigurationDocument
{
    [JsonPropertyName("schema_version")]
    [JsonPropertyOrder(0)]
    public int SchemaVersion { get; init; }

    [JsonPropertyName("provider")]
    [JsonPropertyOrder(1)]
    public string? Provider { get; init; }

    [JsonPropertyName("models")]
    [JsonPropertyOrder(2)]
    public List<ModelDefinition>? Models { get; init; }

    [JsonPropertyName("roles")]
    [JsonPropertyOrder(3)]
    public ModelRoleAssignments? Roles { get; init; }
}

public sealed class ModelDefinition
{
    [JsonPropertyName("id")]
    [JsonPropertyOrder(0)]
    public string? Id { get; init; }

    [JsonPropertyName("display_name")]
    [JsonPropertyOrder(1)]
    public string? DisplayName { get; init; }

    [JsonPropertyName("roles")]
    [JsonPropertyOrder(2)]
    public List<string>? Roles { get; init; }

    [JsonPropertyName("protocol")]
    [JsonPropertyOrder(3)]
    public string? Protocol { get; init; }

    [JsonPropertyName("dimension")]
    [JsonPropertyOrder(4)]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? Dimension { get; init; }

    [JsonPropertyName("enabled")]
    [JsonPropertyOrder(5)]
    public bool? Enabled { get; init; }

    [JsonPropertyName("pricing")]
    [JsonPropertyOrder(6)]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public ModelPricing? Pricing { get; init; }
}

public sealed class ModelPricing
{
    [JsonPropertyName("input_yuan_per_million")]
    [JsonPropertyOrder(0)]
    public decimal? InputYuanPerMillion { get; init; }

    [JsonPropertyName("output_yuan_per_million")]
    [JsonPropertyOrder(1)]
    public decimal? OutputYuanPerMillion { get; init; }

    [JsonPropertyName("effective_from")]
    [JsonPropertyOrder(2)]
    public string? EffectiveFrom { get; init; }
}

public sealed class ModelRoleAssignments
{
    [JsonPropertyName("embedding")]
    [JsonPropertyOrder(0)]
    public string? Embedding { get; init; }

    [JsonPropertyName("auto_tag_primary")]
    [JsonPropertyOrder(1)]
    public string? AutoTagPrimary { get; init; }

    [JsonPropertyName("auto_tag_escalation")]
    [JsonPropertyOrder(2)]
    public string? AutoTagEscalation { get; init; }
}

public sealed record ModelConfigurationLoadResult(
    ModelConfigurationDocument Configuration,
    bool IsValid,
    bool IsUsingLastValidConfiguration,
    bool WasCreated,
    string? Error
);

public sealed record ModelChoiceItem(
    string Id,
    string DisplayName,
    string Capability,
    string Protocol)
{
    public string DisplayText => $"{DisplayName}  ·  {Id}";
}
