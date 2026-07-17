namespace Zvec.Desktop.Services;

public static class BackendSearchRequestOptions
{
    public static void AddSearchMode(
        IDictionary<string, object?> parameters,
        bool semanticEnabled,
        bool tagOnlySearchSupported)
    {
        ArgumentNullException.ThrowIfNull(parameters);
        if (semanticEnabled)
        {
            // Semantic search is the protocol default. Omitting this optional
            // field preserves compatibility with older persistent backends.
            return;
        }
        if (!tagOnlySearchSupported)
        {
            throw new InvalidOperationException(
                "当前常驻后端不支持纯标签搜索，请更新桌面应用后重试。"
            );
        }
        parameters["search_mode"] = "tags";
    }

    public static void AddLowConfidenceOverride(
        IDictionary<string, object?> parameters,
        bool requested,
        bool supported)
    {
        ArgumentNullException.ThrowIfNull(parameters);
        if (!requested)
        {
            return;
        }
        if (!supported)
        {
            throw new InvalidOperationException(
                "当前原生后端不支持显示低置信候选，请更新桌面应用后重试。"
            );
        }
        parameters["show_low_confidence"] = true;
    }

    public static void AddResultDiversity(
        IDictionary<string, object?> parameters,
        bool requested,
        bool supported)
    {
        ArgumentNullException.ThrowIfNull(parameters);
        if (requested)
        {
            // Diversity is the new protocol default. Omitting the optional
            // field preserves a normal request for an older backend.
            return;
        }
        if (!supported)
        {
            throw new InvalidOperationException(
                "当前常驻后端不支持关闭套图多样化，请更新桌面应用后重试。"
            );
        }
        parameters["diversify_results"] = false;
    }
}
