using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;

[assembly: AssemblyTitle("zvec-image-search command launcher")]
[assembly: AssemblyProduct("zvec-image-search command launcher")]
[assembly: AssemblyVersion("1.0.0.0")]

internal static class ZvecCommand
{
    private const string LauncherPath = "__ZVEC_LAUNCHER_PATH__";

    public static int Main(string[] args)
    {
        try
        {
            string windows = Environment.GetFolderPath(
                Environment.SpecialFolder.Windows
            );
            string powershell = Path.Combine(
                windows,
                "System32",
                "WindowsPowerShell",
                "v1.0",
                "powershell.exe"
            );
            if (!File.Exists(powershell))
            {
                powershell = "powershell.exe";
            }

            var forwarded = new List<string>
            {
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                LauncherPath,
            };
            forwarded.AddRange(args);

            var commandLine = new StringBuilder();
            foreach (string argument in forwarded)
            {
                if (commandLine.Length > 0)
                {
                    commandLine.Append(' ');
                }
                commandLine.Append(QuoteArgument(argument));
            }

            var startInfo = new ProcessStartInfo
            {
                FileName = powershell,
                Arguments = commandLine.ToString(),
                UseShellExecute = false,
                WorkingDirectory = Environment.CurrentDirectory,
            };
            using (Process process = Process.Start(startInfo))
            {
                if (process == null)
                {
                    Console.Error.WriteLine("Could not start PowerShell.");
                    return 1;
                }
                process.WaitForExit();
                return process.ExitCode;
            }
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine("Could not start zvec: " + exception.Message);
            return 1;
        }
    }

    private static string QuoteArgument(string value)
    {
        var result = new StringBuilder();
        result.Append('"');
        int backslashes = 0;

        foreach (char character in value)
        {
            if (character == '\\')
            {
                backslashes++;
                continue;
            }
            if (character == '"')
            {
                result.Append('\\', backslashes * 2 + 1);
                result.Append('"');
                backslashes = 0;
                continue;
            }
            result.Append('\\', backslashes);
            backslashes = 0;
            result.Append(character);
        }

        result.Append('\\', backslashes * 2);
        result.Append('"');
        return result.ToString();
    }
}
