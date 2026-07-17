namespace Zvec.Desktop.Models;

public sealed class LibraryListItem
{
    public required string Id { get; init; }

    public required string Name { get; init; }

    public required string ImageRoot { get; init; }

    public required string WorkspaceDirectory { get; init; }

    public required bool Enabled { get; init; }

    public required bool IsDefault { get; init; }

    public string StateText => IsDefault
        ? Enabled ? "默认 · 已启用" : "默认 · 已停用"
        : Enabled ? "已启用" : "已停用";

    public string WorkspaceText => WorkspaceDirectory;
}

public sealed class LibraryChoiceItem
{
    public required string LibraryId { get; init; }

    public required string Name { get; init; }

    public bool IsDefault { get; init; }

    public string DisplayName => IsDefault ? $"{Name}（默认）" : Name;
}

public sealed class LibrarySearchScopeItem
{
    public string? LibraryId { get; init; }

    public required string DisplayName { get; init; }

    public bool IsAllEnabled { get; init; }
}
