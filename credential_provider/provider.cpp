#include "provider.h"
#include "credential.h"
#include "common.h"

#include <propkey.h>
#include <sddl.h>
#include <shlguid.h>
#include <shlwapi.h>

namespace
{

struct FieldDescTemplate
{
    DWORD id;
    CREDENTIAL_PROVIDER_FIELD_TYPE type;
    const wchar_t* label;
    GUID guid;
};

std::wstring ExtractShortUsername(const std::wstring& raw)
{
    const size_t slashPos = raw.find_last_of(L'\\');
    if (slashPos != std::wstring::npos && slashPos + 1 < raw.size())
    {
        return raw.substr(slashPos + 1);
    }
    return raw;
}

std::wstring LookupUsernameFromSidString(const std::wstring& sidStr)
{
    if (sidStr.empty())
    {
        return {};
    }
    PSID pSid = nullptr;
    if (!ConvertStringSidToSidW(sidStr.c_str(), &pSid))
    {
        return {};
    }

    wchar_t name[256]{};
    wchar_t domain[256]{};
    DWORD nameLen = ARRAYSIZE(name);
    DWORD domainLen = ARRAYSIZE(domain);
    SID_NAME_USE snu = SidTypeUnknown;
    std::wstring result;
    if (LookupAccountSidW(nullptr, pSid, name, &nameLen, domain, &domainLen, &snu))
    {
        result = name;
    }
    LocalFree(pSid);
    return result;
}

} // namespace

WinIRFaceIDProvider::WinIRFaceIDProvider()
{
    DllAddRef();
    InitializeCriticalSection(&_cs);
}

WinIRFaceIDProvider::~WinIRFaceIDProvider()
{
    ClearCredentials();
    if (_pEvents != nullptr)
    {
        _pEvents->Release();
        _pEvents = nullptr;
    }
    DeleteCriticalSection(&_cs);
    DllRelease();
}

void WinIRFaceIDProvider::ClearCredentials()
{
    std::vector<WinIRFaceIDCredential*> toRelease;
    EnterCriticalSection(&_cs);
    toRelease.swap(_credentials);
    LeaveCriticalSection(&_cs);

    for (WinIRFaceIDCredential* cred : toRelease)
    {
        if (cred != nullptr)
        {
            cred->DetachProvider();
            cred->Release();
        }
    }
}

IFACEMETHODIMP WinIRFaceIDProvider::QueryInterface(REFIID riid, void** ppv)
{
    static const QITAB qit[] = {
        QITABENT(WinIRFaceIDProvider, ICredentialProvider),
        QITABENT(WinIRFaceIDProvider, ICredentialProviderSetUserArray),
        { 0 }
    };
    return QISearch(this, qit, riid, ppv);
}

IFACEMETHODIMP_(ULONG) WinIRFaceIDProvider::AddRef()
{
    return static_cast<ULONG>(InterlockedIncrement(&_refCount));
}

IFACEMETHODIMP_(ULONG) WinIRFaceIDProvider::Release()
{
    const LONG count = InterlockedDecrement(&_refCount);
    if (count == 0)
    {
        delete this;
    }
    return static_cast<ULONG>(count);
}

IFACEMETHODIMP WinIRFaceIDProvider::SetUsageScenario(
    CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus,
    DWORD /*dwFlags*/)
{
    switch (cpus)
    {
    case CPUS_LOGON:
    case CPUS_UNLOCK_WORKSTATION:
        EnterCriticalSection(&_cs);
        _cpus = cpus;
        LeaveCriticalSection(&_cs);
        return S_OK;

    case CPUS_CHANGE_PASSWORD:
    case CPUS_CREDUI:
    case CPUS_PLAP:
    case CPUS_INVALID:
    default:
        return E_NOTIMPL;
    }
}

IFACEMETHODIMP WinIRFaceIDProvider::SetSerialization(
    const CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION* /*pcpcs*/)
{
    return E_NOTIMPL;
}

IFACEMETHODIMP WinIRFaceIDProvider::Advise(
    ICredentialProviderEvents* pcpe,
    UINT_PTR upAdviseContext)
{
    EnterCriticalSection(&_cs);
    if (_pEvents != nullptr)
    {
        _pEvents->Release();
        _pEvents = nullptr;
    }
    _pEvents = pcpe;
    if (_pEvents != nullptr)
    {
        _pEvents->AddRef();
    }
    _upAdviseContext = upAdviseContext;
    LeaveCriticalSection(&_cs);
    return S_OK;
}

IFACEMETHODIMP WinIRFaceIDProvider::UnAdvise()
{
    EnterCriticalSection(&_cs);
    if (_pEvents != nullptr)
    {
        _pEvents->Release();
        _pEvents = nullptr;
    }
    _upAdviseContext = 0;
    LeaveCriticalSection(&_cs);
    return S_OK;
}

IFACEMETHODIMP WinIRFaceIDProvider::GetFieldDescriptorCount(DWORD* pdwCount)
{
    if (pdwCount == nullptr)
    {
        return E_POINTER;
    }
    *pdwCount = FID_NUM_FIELDS;
    return S_OK;
}

IFACEMETHODIMP WinIRFaceIDProvider::GetFieldDescriptorAt(
    DWORD dwIndex,
    CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR** ppcpfd)
{
    if (ppcpfd == nullptr)
    {
        return E_POINTER;
    }
    *ppcpfd = nullptr;

    const FieldDescTemplate fields[FID_NUM_FIELDS] = {
        { FID_TILE_IMAGE,   CPFT_TILE_IMAGE,    L"IR Face ID",    CPFG_CREDENTIAL_PROVIDER_LOGO },
        { FID_LABEL,        CPFT_LARGE_TEXT,    L"IR Face ID",    CPFG_CREDENTIAL_PROVIDER_LABEL },
        { FID_STATUS,       CPFT_SMALL_TEXT,    L"Status",        GUID_NULL },
        { FID_RETRY_SUBMIT, CPFT_SUBMIT_BUTTON, L"Retry IR Scan", GUID_NULL },
    };

    if (dwIndex >= FID_NUM_FIELDS)
    {
        return E_INVALIDARG;
    }

    auto* desc = static_cast<CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR*>(
        CoTaskMemAlloc(sizeof(CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR)));
    if (desc == nullptr)
    {
        return E_OUTOFMEMORY;
    }

    desc->dwFieldID = fields[dwIndex].id;
    desc->cpft = fields[dwIndex].type;
    desc->guidFieldType = fields[dwIndex].guid;
    desc->pszLabel = nullptr;

    const HRESULT hr = SHStrDupW(fields[dwIndex].label, &desc->pszLabel);
    if (FAILED(hr))
    {
        CoTaskMemFree(desc);
        return hr;
    }

    *ppcpfd = desc;
    return S_OK;
}

IFACEMETHODIMP WinIRFaceIDProvider::SetUserArray(ICredentialProviderUserArray* users)
{
    if (users == nullptr)
    {
        return E_INVALIDARG;
    }

    ClearCredentials();

    DWORD userCount = 0;
    HRESULT hr = users->GetCount(&userCount);
    if (FAILED(hr))
    {
        return hr;
    }

    EnterCriticalSection(&_cs);
    const CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus = _cpus;
    LeaveCriticalSection(&_cs);

    std::vector<WinIRFaceIDCredential*> created;
    created.reserve(userCount);

    for (DWORD i = 0; i < userCount; ++i)
    {
        ICredentialProviderUser* pUser = nullptr;
        if (FAILED(users->GetAt(i, &pUser)) || pUser == nullptr)
        {
            continue;
        }

        std::wstring sidStr;
        LPWSTR pszSid = nullptr;
        if (SUCCEEDED(pUser->GetSid(&pszSid)) && pszSid != nullptr)
        {
            sidStr = pszSid;
            CoTaskMemFree(pszSid);
        }

        std::wstring userName;
        LPWSTR pszName = nullptr;
        if (SUCCEEDED(pUser->GetStringValue(PKEY_Identity_UserName, &pszName)) && pszName != nullptr)
        {
            userName = ExtractShortUsername(pszName);
            CoTaskMemFree(pszName);
        }
        else if (SUCCEEDED(pUser->GetStringValue(PKEY_Identity_QualifiedUserName, &pszName)) && pszName != nullptr)
        {
            userName = ExtractShortUsername(pszName);
            CoTaskMemFree(pszName);
        }
        else
        {
            userName = LookupUsernameFromSidString(sidStr);
        }

        pUser->Release();

        auto* cred = new (std::nothrow) WinIRFaceIDCredential(
            this,
            cpus,
            std::move(sidStr),
            std::move(userName));
        if (cred != nullptr)
        {
            created.push_back(cred);
        }
    }

    EnterCriticalSection(&_cs);
    _credentials.swap(created);
    LeaveCriticalSection(&_cs);

    return S_OK;
}

IFACEMETHODIMP WinIRFaceIDProvider::GetCredentialCount(
    DWORD* pdwCount,
    DWORD* pdwDefault,
    BOOL* pbAutoLogonWithDefault)
{
    if (pdwCount == nullptr || pdwDefault == nullptr || pbAutoLogonWithDefault == nullptr)
    {
        return E_POINTER;
    }

    *pdwDefault = CREDENTIAL_PROVIDER_NO_DEFAULT;
    *pbAutoLogonWithDefault = FALSE;

    std::vector<WinIRFaceIDCredential*> snapshot;
    EnterCriticalSection(&_cs);
    snapshot = _credentials;
    for (WinIRFaceIDCredential* cred : snapshot)
    {
        cred->AddRef();
    }
    LeaveCriticalSection(&_cs);

    *pdwCount = static_cast<DWORD>(snapshot.size());
    for (DWORD i = 0; i < static_cast<DWORD>(snapshot.size()); ++i)
    {
        if (snapshot[i]->HasVerifiedCredential())
        {
            *pdwDefault = i;
            *pbAutoLogonWithDefault = TRUE;
            break;
        }
    }

    for (WinIRFaceIDCredential* cred : snapshot)
    {
        cred->Release();
    }
    return S_OK;
}

IFACEMETHODIMP WinIRFaceIDProvider::GetCredentialAt(
    DWORD dwIndex,
    ICredentialProviderCredential** ppcpc)
{
    if (ppcpc == nullptr)
    {
        return E_POINTER;
    }
    *ppcpc = nullptr;

    WinIRFaceIDCredential* cred = nullptr;
    EnterCriticalSection(&_cs);
    if (dwIndex < _credentials.size())
    {
        cred = _credentials[dwIndex];
        cred->AddRef();
    }
    LeaveCriticalSection(&_cs);

    if (cred == nullptr)
    {
        return E_INVALIDARG;
    }

    const HRESULT hr = cred->QueryInterface(IID_PPV_ARGS(ppcpc));
    cred->Release();
    return hr;
}

void WinIRFaceIDProvider::NotifyCredentialsChanged()
{
    ICredentialProviderEvents* pEvents = nullptr;
    UINT_PTR ctx = 0;

    EnterCriticalSection(&_cs);
    if (_pEvents != nullptr)
    {
        pEvents = _pEvents;
        pEvents->AddRef();
        ctx = _upAdviseContext;
    }
    LeaveCriticalSection(&_cs);

    if (pEvents != nullptr)
    {
        pEvents->CredentialsChanged(ctx);
        pEvents->Release();
    }
}
