namespace Zvec.Desktop.Services;

public sealed class ApiCredentialService
{
    private readonly CredentialManagerService _credentialManager;
    private readonly LauncherConfigService _configService;

    public ApiCredentialService(
        CredentialManagerService? credentialManager = null,
        LauncherConfigService? configService = null)
    {
        _credentialManager = credentialManager ?? new CredentialManagerService();
        _configService = configService ?? new LauncherConfigService();
    }

    public bool HasApiKey()
    {
        MigrateLegacyApiKey();
        return _credentialManager.HasApiKey();
    }

    public string? ReadApiKey()
    {
        MigrateLegacyApiKey();
        return _credentialManager.ReadApiKey();
    }

    public string? ReadApiUrl()
    {
        var configured = Environment.GetEnvironmentVariable("DASHSCOPE_API_URL");
        return string.IsNullOrWhiteSpace(configured)
            ? _configService.ReadLegacyApiUrl()
            : configured.Trim();
    }

    public void SaveApiKey(string apiKey)
    {
        _credentialManager.SaveApiKey(apiKey);
        _configService.RemoveLegacyApiKey();
    }

    public void DeleteApiKey()
    {
        _credentialManager.DeleteApiKey();
        _configService.RemoveLegacyApiKey();
    }

    public bool MigrateLegacyApiKey()
    {
        if (_credentialManager.HasApiKey())
        {
            _configService.RemoveLegacyApiKey();
            return false;
        }

        var legacyApiKey = _configService.ReadLegacyApiKey();
        if (string.IsNullOrWhiteSpace(legacyApiKey))
        {
            return false;
        }

        _credentialManager.SaveApiKey(legacyApiKey);
        _configService.RemoveLegacyApiKey();
        return true;
    }
}
