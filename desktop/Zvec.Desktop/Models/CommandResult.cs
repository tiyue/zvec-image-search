namespace Zvec.Desktop.Models;

public sealed record CommandResult(
    int ExitCode,
    string StandardOutput,
    string StandardError,
    bool WasCancelled)
{
    public bool IsSuccess => ExitCode == 0 && !WasCancelled;

    public bool IsPartialSuccess => ExitCode == 4 && !WasCancelled;

    public string CombinedOutput
    {
        get
        {
            if (string.IsNullOrWhiteSpace(StandardError))
            {
                return StandardOutput;
            }

            if (string.IsNullOrWhiteSpace(StandardOutput))
            {
                return StandardError;
            }

            return $"{StandardOutput.TrimEnd()}{Environment.NewLine}{StandardError.TrimEnd()}";
        }
    }
}

public sealed class CommandOutputEventArgs(string line, bool isError) : EventArgs
{
    public string Line { get; } = line;

    public bool IsError { get; } = isError;
}
