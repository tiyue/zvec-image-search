namespace Zvec.Desktop.Services;

public static class RepositoryLocator
{
    public static string FindRepositoryRoot() => FindRepositoryRoot(startDirectory: null);

    public static string FindRepositoryRoot(string? startDirectory)
    {
        var configured = Environment.GetEnvironmentVariable("ZVEC_REPO_ROOT");
        if (!string.IsNullOrWhiteSpace(configured) && IsRepositoryRoot(configured))
        {
            return Path.GetFullPath(configured);
        }

        foreach (var start in CandidateStarts(startDirectory))
        {
            var directory = new DirectoryInfo(start);
            while (directory is not null)
            {
                if (IsRepositoryRoot(directory.FullName))
                {
                    return directory.FullName;
                }

                directory = directory.Parent;
            }
        }

        if (File.Exists(Path.Combine(AppContext.BaseDirectory, "scripts", "zvec.ps1")))
        {
            return AppContext.BaseDirectory;
        }

        throw new DirectoryNotFoundException(
            "找不到项目根目录。请从仓库内运行桌面程序，或设置 ZVEC_REPO_ROOT。"
        );
    }

    private static IEnumerable<string> CandidateStarts(string? startDirectory)
    {
        if (!string.IsNullOrWhiteSpace(startDirectory))
        {
            yield return Path.GetFullPath(startDirectory);
        }
        yield return AppContext.BaseDirectory;
        yield return Environment.CurrentDirectory;
    }

    private static bool IsRepositoryRoot(string path) =>
        File.Exists(Path.Combine(path, "scripts", "zvec.ps1")) &&
        (
            File.Exists(Path.Combine(path, "image_service.py")) ||
            File.Exists(Path.Combine(path, "backend", "image_service.py")) ||
            File.Exists(Path.Combine(path, "pyproject.toml"))
        );
}
