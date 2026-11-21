#pragma once

#include <windows.h>
#include <credentialprovider.h>
#include <vector>

class WinIRFaceIDCredential;

class WinIRFaceIDProvider final
    : public ICredentialProvider
    , public ICredentialProviderSetUserArray
{
public:
    WinIRFaceIDProvider();

    // IUnknown
    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) override;
    IFACEMETHODIMP_(ULONG) AddRef() override;
    IFACEMETHODIMP_(ULONG) Release() override;

    // ICredentialProvider
    IFACEMETHODIMP SetUsageScenario(
        CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus,
        DWORD dwFlags) override;
    IFACEMETHODIMP SetSerialization(
        const CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION* pcpcs) override;
    IFACEMETHODIMP Advise(
        ICredentialProviderEvents* pcpe,
        UINT_PTR upAdviseContext) override;
    IFACEMETHODIMP UnAdvise() override;
    IFACEMETHODIMP GetFieldDescriptorCount(DWORD* pdwCount) override;
    IFACEMETHODIMP GetFieldDescriptorAt(
        DWORD dwIndex,
        CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR** ppcpfd) override;
    IFACEMETHODIMP GetCredentialCount(
        DWORD* pdwCount,
        DWORD* pdwDefault,
        BOOL* pbAutoLogonWithDefault) override;
    IFACEMETHODIMP GetCredentialAt(
        DWORD dwIndex,
        ICredentialProviderCredential** ppcpc) override;

    // ICredentialProviderSetUserArray
    IFACEMETHODIMP SetUserArray(ICredentialProviderUserArray* users) override;

    // Called by child credentials when face verification produces valid logon data.
    void NotifyCredentialsChanged();

private:
    ~WinIRFaceIDProvider();
    void ClearCredentials();
    void MaybeStartBackgroundAutoScan();

    LONG _refCount = 1;
    CRITICAL_SECTION _cs{};
    CREDENTIAL_PROVIDER_USAGE_SCENARIO _cpus = CPUS_INVALID;
    ICredentialProviderEvents* _pEvents = nullptr;
    UINT_PTR _upAdviseContext = 0;
    bool _autoScanTriggered = false;
    std::vector<WinIRFaceIDCredential*> _credentials;
};
