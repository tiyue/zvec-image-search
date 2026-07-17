using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace Zvec.Desktop.Services;

public sealed class CredentialManagerService
{
    private const string DefaultTargetName = "Zvec.ImageSearch/DashScopeApiKey";
    private const uint CredentialTypeGeneric = 1;
    private const uint CredentialPersistLocalMachine = 2;
    private const int ErrorNotFound = 1168;
    private readonly string _targetName;

    public CredentialManagerService(string? targetName = null)
    {
        _targetName = string.IsNullOrWhiteSpace(targetName)
            ? DefaultTargetName
            : targetName.Trim();
    }

    public bool HasApiKey()
        => HasSecret();

    public bool HasSecret()
    {
        if (!OperatingSystem.IsWindows())
        {
            return false;
        }

        if (CredRead(_targetName, CredentialTypeGeneric, 0, out var credentialPointer))
        {
            CredFree(credentialPointer);
            return true;
        }

        var error = Marshal.GetLastWin32Error();
        if (error == ErrorNotFound)
        {
            return false;
        }
        throw new Win32Exception(error, "无法读取 Windows 凭据管理器。");
    }

    public string? ReadApiKey()
        => ReadSecret();

    public string? ReadSecret()
    {
        if (!OperatingSystem.IsWindows())
        {
            return null;
        }
        if (!CredRead(_targetName, CredentialTypeGeneric, 0, out var credentialPointer))
        {
            var error = Marshal.GetLastWin32Error();
            if (error == ErrorNotFound)
            {
                return null;
            }
            throw new Win32Exception(error, "无法读取 Windows 凭据管理器。");
        }

        byte[]? bytes = null;
        try
        {
            var credential = Marshal.PtrToStructure<NativeCredential>(credentialPointer);
            if (credential.CredentialBlobSize == 0 || credential.CredentialBlob == IntPtr.Zero)
            {
                return null;
            }
            bytes = new byte[credential.CredentialBlobSize];
            Marshal.Copy(credential.CredentialBlob, bytes, 0, bytes.Length);
            return Encoding.UTF8.GetString(bytes);
        }
        finally
        {
            if (bytes is not null)
            {
                Array.Clear(bytes);
            }
            CredFree(credentialPointer);
        }
    }

    public void SaveApiKey(string apiKey)
        => SaveSecret(apiKey, nameof(apiKey));

    public void SaveSecret(string secret)
        => SaveSecret(secret, nameof(secret));

    private void SaveSecret(string secret, string parameterName)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(secret, parameterName);
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "Windows Credential Manager is required to save this secret."
            );
        }
        if (secret.Contains('\r') || secret.Contains('\n'))
        {
            throw new ArgumentException("安全凭据不能包含换行符。", parameterName);
        }

        var bytes = Encoding.UTF8.GetBytes(secret);
        if (bytes.Length > 2560)
        {
            Array.Clear(bytes);
            throw new ArgumentException(
                "安全凭据超过 Windows 凭据大小限制。",
                parameterName
            );
        }

        var blob = Marshal.AllocCoTaskMem(bytes.Length);
        try
        {
            Marshal.Copy(bytes, 0, blob, bytes.Length);
            var credential = new NativeCredential
            {
                Type = CredentialTypeGeneric,
                TargetName = _targetName,
                CredentialBlobSize = checked((uint)bytes.Length),
                CredentialBlob = blob,
                Persist = CredentialPersistLocalMachine,
                UserName = Environment.UserName,
            };
            if (!CredWrite(ref credential, 0))
            {
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "无法保存安全凭据到 Windows 凭据管理器。"
                );
            }
        }
        finally
        {
            Array.Clear(bytes);
            ZeroMemory(blob, bytes.Length);
            Marshal.FreeCoTaskMem(blob);
        }
    }

    public void DeleteApiKey()
        => DeleteSecret();

    public void DeleteSecret()
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }
        if (CredDelete(_targetName, CredentialTypeGeneric, 0))
        {
            return;
        }
        var error = Marshal.GetLastWin32Error();
        if (error != ErrorNotFound)
        {
            throw new Win32Exception(error, "无法删除 Windows 凭据管理器中的安全凭据。");
        }
    }

    private static void ZeroMemory(IntPtr pointer, int length)
    {
        for (var index = 0; index < length; index++)
        {
            Marshal.WriteByte(pointer, index, 0);
        }
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NativeCredential
    {
        public uint Flags;
        public uint Type;
        public string TargetName;
        public string? Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public uint CredentialBlobSize;
        public IntPtr CredentialBlob;
        public uint Persist;
        public uint AttributeCount;
        public IntPtr Attributes;
        public string? TargetAlias;
        public string UserName;
    }

    [DllImport("advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CredWrite([In] ref NativeCredential userCredential, uint flags);

    [DllImport("advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CredRead(
        string target,
        uint type,
        uint reservedFlag,
        out IntPtr credentialPointer
    );

    [DllImport("advapi32.dll", EntryPoint = "CredDeleteW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CredDelete(string target, uint type, uint flags);

    [DllImport("advapi32.dll")]
    private static extern void CredFree(IntPtr credentialPointer);
}
