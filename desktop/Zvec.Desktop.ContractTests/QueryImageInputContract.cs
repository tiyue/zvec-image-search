using Zvec.Desktop.Services;

internal static class QueryImageInputContract
{
    public static async Task RunAsync()
    {
        var root = Path.Combine(
            Path.GetTempPath(),
            $"zvec-query-input-contract-{Guid.NewGuid():N}"
        );
        var owned = Path.Combine(root, "owned");
        var external = Path.Combine(root, "external");
        Directory.CreateDirectory(owned);
        Directory.CreateDirectory(external);
        try
        {
            var service = new QueryImageInputService(owned);
            Assert(service.IsSupportedImageFile("portrait.JPG"), "extension matching is case-insensitive");
            Assert(service.IsSupportedImageFile("portrait.webp"), "webp is supported");
            Assert(!service.IsSupportedImageFile("notes.txt"), "non-images are rejected");

            var image = Path.Combine(external, "query.png");
            await File.WriteAllBytesAsync(image, [1, 2, 3]);
            var directoryFirst = service.ResolveDrop([external, image]);
            Assert(directoryFirst.ImagePath == image, "a supported file wins over a directory");
            Assert(directoryFirst.DirectoryPath is null, "resolved file does not retain a directory");

            var directoryOnly = service.ResolveDrop([external]);
            Assert(directoryOnly.DirectoryPath == external, "a dropped directory is preserved");
            Assert(directoryOnly.ImagePath is null, "directory drop does not guess an image");

            var unsupported = Path.Combine(external, "notes.txt");
            await File.WriteAllTextAsync(unsupported, "not an image");
            var rejected = service.ResolveDrop([unsupported]);
            Assert(!rejected.HasImage && !rejected.HasDirectory, "unsupported files are rejected");
            Assert(!string.IsNullOrWhiteSpace(rejected.ErrorMessage), "rejection explains the problem");

            var ownedFile = Path.Combine(owned, $"query-{Guid.NewGuid():N}.png");
            await File.WriteAllBytesAsync(ownedFile, [4, 5, 6]);
            Assert(service.TryDeleteOwnedFile(ownedFile), "service deletes its own temporary image");
            Assert(!File.Exists(ownedFile), "owned temporary image is removed");

            var externalOwnedName = Path.Combine(
                external,
                $"query-{Guid.NewGuid():N}.png"
            );
            await File.WriteAllBytesAsync(externalOwnedName, [7, 8, 9]);
            Assert(
                !service.TryDeleteOwnedFile(externalOwnedName),
                "service refuses to delete a user file outside its directory"
            );
            Assert(File.Exists(externalOwnedName), "external user file remains intact");

            var stale = Path.Combine(owned, $"query-{Guid.NewGuid():N}.png");
            var fresh = Path.Combine(owned, $"query-{Guid.NewGuid():N}.png");
            await File.WriteAllBytesAsync(stale, [10]);
            await File.WriteAllBytesAsync(fresh, [11]);
            File.SetLastWriteTimeUtc(stale, DateTime.UtcNow.AddDays(-3));
            var deleted = await service.DeleteStaleOwnedFilesAsync(TimeSpan.FromDays(2));
            Assert(deleted == 1, "stale cleanup deletes only expired owned files");
            Assert(!File.Exists(stale) && File.Exists(fresh), "fresh clipboard image is retained");
        }
        finally
        {
            if (Directory.Exists(root))
            {
                Directory.Delete(root, recursive: true);
            }
        }
    }

    private static void Assert(bool condition, string message)
    {
        if (!condition)
        {
            throw new InvalidOperationException($"Contract failed: {message}");
        }
    }
}
