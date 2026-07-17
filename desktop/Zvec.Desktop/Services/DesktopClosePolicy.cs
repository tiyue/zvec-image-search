namespace Zvec.Desktop.Services;

public enum DesktopCloseAction
{
    HideToTray,
    BeginApplicationExit,
    CompleteApplicationExit,
}

/// <summary>
/// Separates an ordinary window close from an explicit application exit.
/// Keeping this decision free of WPF state makes the behavior easy to test.
/// </summary>
public sealed class DesktopClosePolicy
{
    public bool ExitRequested { get; private set; }

    public void RequestExit() => ExitRequested = true;

    public void CancelExit() => ExitRequested = false;

    public DesktopCloseAction Resolve(bool applicationCloseAllowed)
    {
        if (applicationCloseAllowed)
        {
            return DesktopCloseAction.CompleteApplicationExit;
        }
        return ExitRequested
            ? DesktopCloseAction.BeginApplicationExit
            : DesktopCloseAction.HideToTray;
    }
}
