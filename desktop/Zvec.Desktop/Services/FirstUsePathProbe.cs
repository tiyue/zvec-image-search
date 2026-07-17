namespace Zvec.Desktop.Services;

internal static class FirstUsePathProbe
{
    internal static bool IsReadableDirectory(string path)
    {
        try
        {
            // Enumerating at most one child checks directory-list permission without
            // opening or modifying any image file.
            _ = Directory.EnumerateFileSystemEntries(path).FirstOrDefault();
            return true;
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException or
                ArgumentException or NotSupportedException
        )
        {
            return false;
        }
    }

    internal static bool IsWritableDirectoryOrParent(string path)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            return false;
        }

        string fullPath;
        try
        {
            fullPath = Path.GetFullPath(Environment.ExpandEnvironmentVariables(path));
        }
        catch (Exception exception) when (
            exception is ArgumentException or NotSupportedException or
                PathTooLongException
        )
        {
            return false;
        }
        if (File.Exists(fullPath))
        {
            return false;
        }

        var probeDirectory = Directory.Exists(fullPath)
            ? new DirectoryInfo(fullPath)
            : new DirectoryInfo(fullPath).Parent;
        while (probeDirectory is not null && !probeDirectory.Exists)
        {
            // Do not climb past a path component that is actually a file. A target
            // such as C:\file\child can never be created even if C:\ is writable.
            if (File.Exists(probeDirectory.FullName))
            {
                return false;
            }
            probeDirectory = probeDirectory.Parent;
        }
        if (probeDirectory is null)
        {
            return false;
        }

        var probePath = Path.Combine(
            probeDirectory.FullName,
            $".zvec-write-probe-{Guid.NewGuid():N}.tmp"
        );
        try
        {
            // Workspace/results probes are temporary and always deleted. This helper
            // is deliberately never used merely to test image-root read access.
            using var stream = new FileStream(
                probePath,
                FileMode.CreateNew,
                FileAccess.Write,
                FileShare.None,
                bufferSize: 1,
                FileOptions.DeleteOnClose
            );
            stream.WriteByte(0);
            return true;
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException or
                ArgumentException or NotSupportedException
        )
        {
            return false;
        }
        finally
        {
            TryDelete(probePath);
        }
    }

    private static void TryDelete(string path)
    {
        try
        {
            File.Delete(path);
        }
        catch (IOException)
        {
        }
        catch (UnauthorizedAccessException)
        {
        }
    }
}
