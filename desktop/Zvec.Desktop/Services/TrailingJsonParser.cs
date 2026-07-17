using System.Text.Json;

namespace Zvec.Desktop.Services;

public static class TrailingJsonParser
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
    };

    public static bool TryDeserialize<T>(string text, out T? value)
    {
        value = default;
        if (string.IsNullOrWhiteSpace(text))
        {
            return false;
        }

        var trimmed = text.TrimEnd();
        for (var index = trimmed.Length - 1; index >= 0; index--)
        {
            if (trimmed[index] is not ('{' or '['))
            {
                continue;
            }

            try
            {
                value = JsonSerializer.Deserialize<T>(trimmed[index..], JsonOptions);
                if (value is not null)
                {
                    return true;
                }
            }
            catch (JsonException)
            {
                // The candidate can be a nested JSON value or a progress line.
            }
        }

        return false;
    }
}
