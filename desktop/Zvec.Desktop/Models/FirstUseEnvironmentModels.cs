namespace Zvec.Desktop.Models;

public enum EnvironmentCheckState
{
    Ready,
    Pending,
    ActionRequired,
    Checking,
}

public sealed record EnvironmentCheckItem(
    EnvironmentCheckState State,
    string Status,
    string Detail
);

public sealed record FirstUseEnvironmentInput
{
    public bool RunnerAvailable { get; init; }

    public bool PythonReady { get; init; }

    public string PythonDetail { get; init; } = string.Empty;

    public bool DependenciesReady { get; init; }

    public string DependenciesDetail { get; init; } = string.Empty;

    public bool ApiKeyConfigured { get; init; }

    public string ImageRoot { get; init; } = string.Empty;

    public bool ImageRootExists { get; init; }

    public bool ImageRootReadable { get; init; }

    public string WorkspaceDirectory { get; init; } = string.Empty;

    public bool WorkspaceExists { get; init; }

    public bool WorkspaceWritable { get; init; }

    public string ResultsDirectory { get; init; } = string.Empty;

    public bool ResultsDirectoryExists { get; init; }

    public bool ResultsDirectoryWritable { get; init; }

    public string? PathLayoutError { get; init; }

    public bool PathChecksPending { get; init; }

    public bool IsChecking { get; init; }
}

public sealed record FirstUseEnvironmentSnapshot(
    EnvironmentCheckItem Python,
    EnvironmentCheckItem Dependencies,
    EnvironmentCheckItem ApiKey,
    EnvironmentCheckItem ImageRoot,
    EnvironmentCheckItem Workspace,
    EnvironmentCheckItem ResultsDirectory,
    string Summary,
    string NextAction,
    bool CanInitialize
);

public static class FirstUseEnvironmentEvaluator
{
    public static FirstUseEnvironmentSnapshot Evaluate(FirstUseEnvironmentInput input)
    {
        ArgumentNullException.ThrowIfNull(input);

        if (input.IsChecking)
        {
            var checking = new EnvironmentCheckItem(
                EnvironmentCheckState.Checking,
                "正在检测",
                "请稍候…"
            );
            return new FirstUseEnvironmentSnapshot(
                checking,
                checking,
                checking,
                checking,
                checking,
                checking,
                "正在检查首次使用所需项目",
                "检测完成后会给出下一步操作。",
                false
            );
        }

        var python = input.PythonReady
            ? Ready("已就绪", input.PythonDetail)
            : Required(
                "需要准备",
                input.RunnerAvailable
                    ? string.IsNullOrWhiteSpace(input.PythonDetail)
                        ? "点击“自动修复运行环境”，无需手工填写 Python 路径。"
                        : input.PythonDetail
                    : "未找到运行环境准备脚本，请重新安装桌面应用。"
            );
        var dependencies = input.DependenciesReady
            ? Ready("已安装", input.DependenciesDetail)
            : Required(
                "需要安装",
                string.IsNullOrWhiteSpace(input.DependenciesDetail)
                    ? "自动修复会安装锁定版本的运行依赖。"
                    : input.DependenciesDetail
            );
        var apiKey = input.ApiKeyConfigured
            ? Ready("已配置", "凭据保存在当前用户的 Windows 凭据管理器中。")
            : Required("需要 API Key", "在下方输入一次，保存图库时会安全存储。");
        var imageRoot = DirectoryItem(
            input.ImageRoot,
            input.ImageRootExists,
            input.ImageRootReadable,
            "请选择图片目录",
            "目录不存在，请重新选择。",
            "目录无法读取，请检查权限或重新选择。",
            "目录可读取"
        );
        var workspace = WritableDirectoryItem(
            input.WorkspaceDirectory,
            input.WorkspaceExists,
            input.WorkspaceWritable,
            "请设置 Workspace",
            "保存图库时将自动创建"
        );
        var results = WritableDirectoryItem(
            input.ResultsDirectory,
            input.ResultsDirectoryExists,
            input.ResultsDirectoryWritable,
            "请设置结果目录",
            "保存图库时将自动创建"
        );
        if (input.PathChecksPending)
        {
            var pathChecking = new EnvironmentCheckItem(
                EnvironmentCheckState.Checking,
                "正在检测",
                "正在后台检查目录权限…"
            );
            imageRoot = pathChecking;
            workspace = pathChecking;
            results = pathChecking;
        }
        if (!string.IsNullOrWhiteSpace(input.PathLayoutError))
        {
            workspace = Required("路径冲突", input.PathLayoutError);
            results = Required("路径冲突", input.PathLayoutError);
        }

        var canInitialize = input.PythonReady &&
            input.DependenciesReady &&
            input.ApiKeyConfigured &&
            input.ImageRootExists && input.ImageRootReadable &&
            !string.IsNullOrWhiteSpace(input.WorkspaceDirectory) &&
            input.WorkspaceWritable &&
            !string.IsNullOrWhiteSpace(input.ResultsDirectory) &&
            input.ResultsDirectoryWritable &&
            string.IsNullOrWhiteSpace(input.PathLayoutError) &&
            !input.PathChecksPending;
        var nextAction = NextAction(input);
        var summary = canInitialize
            ? "首次使用环境已就绪"
            : "完成下面的提示后即可建立图库";

        return new FirstUseEnvironmentSnapshot(
            python,
            dependencies,
            apiKey,
            imageRoot,
            workspace,
            results,
            summary,
            nextAction,
            canInitialize
        );
    }

    private static string NextAction(FirstUseEnvironmentInput input)
    {
        if (!input.RunnerAvailable)
        {
            return "运行环境文件缺失，请重新安装桌面应用后再试。";
        }
        if (!input.PythonReady || !input.DependenciesReady)
        {
            return "下一步：点击“自动修复运行环境”。";
        }
        if (!input.ApiKeyConfigured)
        {
            return "下一步：输入 DashScope API Key。";
        }
        if (input.PathChecksPending)
        {
            return "正在后台检测目录可读、可写和路径冲突。";
        }
        if (!input.ImageRootExists)
        {
            return "下一步：选择一个存在的图片目录。";
        }
        if (!input.ImageRootReadable)
        {
            return "下一步：检查图片目录的读取权限，或重新选择目录。";
        }
        if (!string.IsNullOrWhiteSpace(input.PathLayoutError))
        {
            return $"下一步：修正目录设置。{input.PathLayoutError}";
        }
        if (string.IsNullOrWhiteSpace(input.WorkspaceDirectory))
        {
            return "下一步：设置 Workspace 目录。";
        }
        if (!input.WorkspaceWritable)
        {
            return "下一步：选择可写的 Workspace 目录。";
        }
        if (string.IsNullOrWhiteSpace(input.ResultsDirectory))
        {
            return "下一步：设置搜索结果目录。";
        }
        if (!input.ResultsDirectoryWritable)
        {
            return "下一步：选择可写的搜索结果目录。";
        }
        return "可以保存图库配置，然后建立索引。";
    }

    private static EnvironmentCheckItem DirectoryItem(
        string path,
        bool exists,
        bool readable,
        string emptyStatus,
        string missingDetail,
        string unreadableDetail,
        string readyDetail)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            return Required("待选择", emptyStatus);
        }
        return exists
            ? readable
                ? Ready("已就绪", readyDetail)
                : Required("无法读取", unreadableDetail)
            : Required("目录不可用", missingDetail);
    }

    private static EnvironmentCheckItem WritableDirectoryItem(
        string path,
        bool exists,
        bool writable,
        string emptyStatus,
        string createDetail)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            return Required("待设置", emptyStatus);
        }
        if (!writable)
        {
            return Required(
                "无法写入",
                exists ? "目录不可写，请检查权限。" : "父目录不可写，无法自动创建。"
            );
        }
        return exists
            ? Ready("已就绪", "目录已存在且可写")
            : new EnvironmentCheckItem(
                EnvironmentCheckState.Pending,
                "将自动创建",
                createDetail
            );
    }

    private static EnvironmentCheckItem Ready(string status, string detail) => new(
        EnvironmentCheckState.Ready,
        status,
        detail
    );

    private static EnvironmentCheckItem Required(string status, string detail) => new(
        EnvironmentCheckState.ActionRequired,
        status,
        detail
    );
}
