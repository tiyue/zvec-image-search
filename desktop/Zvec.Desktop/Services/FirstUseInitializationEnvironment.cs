namespace Zvec.Desktop.Services;

internal static class FirstUseInitializationEnvironment
{
    internal static IReadOnlyDictionary<string, string?> Create(
        string? pythonExecutable)
    {
        var environment = new Dictionary<string, string?>(
            StringComparer.OrdinalIgnoreCase
        )
        {
            ["DASHSCOPE_API_KEY"] = null,
        };
        if (!string.IsNullOrWhiteSpace(pythonExecutable))
        {
            environment["ZVEC_PYTHON"] = pythonExecutable.Trim();
        }
        return environment;
    }
}
