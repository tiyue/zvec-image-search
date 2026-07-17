using Zvec.Desktop.Models;

namespace Zvec.Desktop.Services;

internal static class WorkspaceMigrationConfirmationFormatter
{
    internal static string Build(
        WorkspaceBackupPlan? backupPlan,
        IReadOnlyList<LauncherNamedVolumeInspection>? namedVolumes)
    {
        namedVolumes ??= [];
        var hasNamedVolume = namedVolumes.Count > 0;
        string detail;
        if (backupPlan is null)
        {
            detail = "将备份现有配置和 Workspace，再完成兼容迁移与向量完整性校验。";
        }
        else
        {
            detail = $"备份位置：{backupPlan.Destination}{Environment.NewLine}";
            detail += hasNamedVolume
                ? $"元数据备份估算：{FormatByteCount(backupPlan.EstimatedPayloadBytes)}" +
                    "（不包含 named volume 导出）"
                : $"预计数据量：{FormatByteCount(backupPlan.EstimatedPayloadBytes)}";
            detail += $"{Environment.NewLine}备份模式：" +
                (string.Equals(
                    backupPlan.BackupMode,
                    "full",
                    StringComparison.OrdinalIgnoreCase
                )
                    ? "完整备份（包含大型向量 Collection）"
                    : "元数据备份（配置、metadata、SQLite；迁移过程会在 Workspace 保留旧 Collection）");
        }

        if (!hasNamedVolume)
        {
            return detail;
        }

        detail += Environment.NewLine +
            "named volume 导出体积：未知，且不包含在当前估算中；请确保目标磁盘有足够可用空间。";
        detail += Environment.NewLine + "一次性导出：" +
            string.Join(
                "；",
                namedVolumes.Select(volume =>
                    $"{volume.VolumeName} → {volume.DefaultExportDirectory}"
                )
            );
        return detail;
    }

    private static string FormatByteCount(long bytes)
    {
        string[] units = ["B", "KB", "MB", "GB", "TB"];
        var value = Math.Max(0, bytes);
        var unitIndex = 0;
        var display = (double)value;
        while (display >= 1024 && unitIndex < units.Length - 1)
        {
            display /= 1024;
            unitIndex++;
        }
        return unitIndex == 0
            ? $"{value} {units[unitIndex]}"
            : $"{display:0.##} {units[unitIndex]}";
    }
}
