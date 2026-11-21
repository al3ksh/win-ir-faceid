#include "provider.h"
#include "credential.h"
#include "common.h"

#include <propkey.h>
#include <sddl.h>
#include <shlguid.h>
#include <shlwapi.h>

#include <cwchar>
#include <new>

namespace
{

struct AutoScanWatcherContext
{
    WinIRFaceIDProvider* provider = nullptr;
    WinIRFaceIDCredential* cred = nullptr;
    HANDLE stopEvent = nullptr;
    CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus = CPUS_INVALID;
};

constexpr int kMonitoredKeys[] = {
    // Mouse clicks (no movement)
    VK_LBUTTON, VK_RBUTTON, VK_MBUTTON,
    // Common unlock/dismiss keys (no Win key)
    VK_SPACE, VK_RETURN, VK_ESCAPE, VK_TAB, VK_BACK,
    VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN,
    VK_SHIFT, VK_CONTROL,
    // Digits '0'..'9'
    '0', '1', '2', '3', '4', '5', '6', '7', '8', '9',
    // Letters 'A'..'Z'
    'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M',
    'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z'
};

void DrainMonitoredKeys()
{
    for (int vk : kMonitoredKeys)
    {
        (void)GetAsyncKeyState(vk);
    }
}

bool HasDeliberateKeyOrClick()
{
    if (((GetAsyncKeyState(VK_LWIN) & 0x8000) != 0) ||
        ((GetAsyncKeyState(VK_RWIN) & 0x8000) != 0))
    {
        DrainMonitoredKeys();
        return false;
    }

    for (int vk : kMonitoredKeys)
    {
        if ((GetAsyncKeyState(vk) & 0x8001) != 0)
        {
            return true;
        }
    }
    return false;
}

bool IsOurDesktopActive()
{
    HDESK hInput = OpenInputDesktop(0, FALSE, DESKTOP_READOBJECTS);
    if (hInput == nullptr)
    {
        // Graceful fallback to foreground window check if input desktop cannot be opened.
        return true;
    }

    wchar_t inputName[128]{};
    wchar_t ourName[128]{};
    DWORD needed = 0;
    const bool gotInput = GetUserObjectInformationW(
        hInput, UOI_NAME, inputName, sizeof(inputName), &needed) != FALSE;
    CloseDesktop(hInput);

    HDESK hOur = GetThreadDesktop(GetCurrentThreadId());
    const bool gotOur = (hOur != nullptr) &&
        (GetUserObjectInformationW(hOur, UOI_NAME, ourName, sizeof(ourName), &needed) != FALSE);

    if (!gotInput || !gotOur)
    {
        return true;
    }
    return _wcsicmp(inputName, ourName) == 0;
}

bool IsSignInScreenActive()
{
    if (!IsOurDesktopActive())
    {
        return false;
    }
    HWND fg = GetForegroundWindow();
    if (fg == nullptr)
    {
        return false;
    }
    DWORD fgPid = 0;
    GetWindowThreadProcessId(fg, &fgPid);
    return fgPid == GetCurrentProcessId() && IsWindowVisible(fg) != FALSE;
}

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
    StopAutoScanWatcher();
    ClearCredentials();
    if (_pEvents != nullptr)
    {
        _pEvents->Release();
        _pEvents = nullptr;
    }
    DeleteCriticalSection(&_cs);
    DllRelease();
}

void WinIRFaceIDProvider::StopAutoScanWatcher()
{
    HANDLE stopEvent = nullptr;
    HANDLE thread = nullptr;

    EnterCriticalSection(&_cs);
    stopEvent = _autoScanStopEvent;
    _autoScanStopEvent = nullptr;
    thread = _autoScanThread;
    _autoScanThread = nullptr;
    LeaveCriticalSection(&_cs);

    if (stopEvent != nullptr)
    {
        SetEvent(stopEvent);
    }
    if (thread != nullptr)
    {
        WaitForSingleObject(thread, 2000);
        CloseHandle(thread);
    }
    if (stopEvent != nullptr)
    {
        CloseHandle(stopEvent);
    }
}

void WinIRFaceIDProvider::ClearCredentials()
{
    StopAutoScanWatcher();

    std::vector<WinIRFaceIDCredential*> toRelease;
    EnterCriticalSection(&_cs);
    toRelease.swap(_credentials);
    _autoScanTriggered = false;
    _unlockArmed = false;
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

bool WinIRFaceIDProvider::ShouldDeferInitialUnlockScan()
{
    EnterCriticalSection(&_cs);
    const CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus = _cpus;
    const bool armed = _unlockArmed;
    const ULONGLONG adviseTickMs = _adviseTickMs;
    LeaveCriticalSection(&_cs);

    if ((cpus != CPUS_LOGON && cpus != CPUS_UNLOCK_WORKSTATION) || armed)
    {
        return false;
    }
    if ((GetTickCount64() - adviseTickMs) < 800ULL)
    {
        return true;
    }

    EnterCriticalSection(&_cs);
    _unlockArmed = true;
    LeaveCriticalSection(&_cs);
    return false;
}

DWORD WINAPI WinIRFaceIDProvider::AutoScanWatcherThreadProc(LPVOID param)
{
    auto* ctx = static_cast<AutoScanWatcherContext*>(param);
    if (ctx != nullptr)
    {
        WinIRFaceIDProvider* provider = ctx->provider;
        WinIRFaceIDCredential* cred = ctx->cred;
        HANDLE stopEvent = ctx->stopEvent;
        const CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus = ctx->cpus;
        delete ctx;

        if (provider != nullptr && cred != nullptr && stopEvent != nullptr)
        {
            provider->RunAutoScanWatcher(cred, stopEvent, cpus);
        }
        if (stopEvent != nullptr)
        {
            CloseHandle(stopEvent);
        }
        if (cred != nullptr)
        {
            cred->Release();
        }
        if (provider != nullptr)
        {
            provider->Release();
        }
    }
    return 0;
}

void WinIRFaceIDProvider::RunAutoScanWatcher(
    WinIRFaceIDCredential* targetCred,
    HANDLE stopEvent,
    CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus)
{
    if (cpus != CPUS_LOGON && cpus != CPUS_UNLOCK_WORKSTATION)
    {
        return;
    }

    // Phase 1 (settle 1200ms): avoid triggering on Win+L or initial lock transition.
    const ULONGLONG lockTickMs = GetTickCount64();
    while ((GetTickCount64() - lockTickMs) < 1200ULL)
    {
        if (WaitForSingleObject(stopEvent, 80) != WAIT_TIMEOUT)
        {
            return;
        }
    }

    // Phase 2 (post-lock baseline at t >= 1200ms): check whether the sign-in screen
    // is obscured behind the lock curtain and drain stale key state.
    bool behindCurtain = !IsSignInScreenActive();
    DrainMonitoredKeys();

    // Phase 3 (poll every 80ms): keep IR LED off behind the lock curtain; exit quietly
    // if already armed, or start scan on curtain dismissal / deliberate key/click.
    while (WaitForSingleObject(stopEvent, 80) == WAIT_TIMEOUT)
    {
        EnterCriticalSection(&_cs);
        const bool alreadyArmed = _unlockArmed;
        LeaveCriticalSection(&_cs);
        if (alreadyArmed)
        {
            return;
        }

        const bool activeNow = IsSignInScreenActive();
        if (!activeNow)
        {
            behindCurtain = true;
            DrainMonitoredKeys();
            continue;
        }

        // Trigger A (curtain just dismissed) or Trigger B (deliberate key/click while active).
        if ((behindCurtain && activeNow) || (activeNow && HasDeliberateKeyOrClick()))
        {
            EnterCriticalSection(&_cs);
            _unlockArmed = true;
            LeaveCriticalSection(&_cs);

            if (!targetCred->HasVerifiedCredential())
            {
                targetCred->StartScan();
            }
            break;
        }
    }
}

void WinIRFaceIDProvider::MaybeStartBackgroundAutoScan()
{
    HANDLE stopEvent = nullptr;
    HANDLE threadStopEvent = nullptr;
    AutoScanWatcherContext* ctx = nullptr;

    EnterCriticalSection(&_cs);
    if (_pEvents != nullptr && !_autoScanTriggered && _credentials.size() == 1 &&
        _autoScanThread == nullptr && _autoScanStopEvent == nullptr)
    {
        WinIRFaceIDCredential* targetCred = _credentials[0];
        const CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus = _cpus;
        if (targetCred != nullptr)
        {
            stopEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
            if (stopEvent != nullptr)
            {
                if (!DuplicateHandle(
                        GetCurrentProcess(),
                        stopEvent,
                        GetCurrentProcess(),
                        &threadStopEvent,
                        0,
                        FALSE,
                        DUPLICATE_SAME_ACCESS))
                {
                    threadStopEvent = nullptr;
                }
            }

            if (stopEvent != nullptr && threadStopEvent != nullptr)
            {
                ctx = new (std::nothrow) AutoScanWatcherContext{};
                if (ctx != nullptr)
                {
                    AddRef();
                    targetCred->AddRef();
                    ctx->provider = this;
                    ctx->cred = targetCred;
                    ctx->stopEvent = threadStopEvent;
                    ctx->cpus = cpus;

                    HANDLE hThread = CreateThread(nullptr, 0, AutoScanWatcherThreadProc, ctx, 0, nullptr);
                    if (hThread != nullptr)
                    {
                        _autoScanStopEvent = stopEvent;
                        _autoScanThread = hThread;
                        _autoScanTriggered = true;
                        stopEvent = nullptr;
                        threadStopEvent = nullptr;
                        ctx = nullptr;
                    }
                }
            }
        }
    }
    LeaveCriticalSection(&_cs);

    if (ctx != nullptr)
    {
        if (ctx->cred != nullptr)
        {
            ctx->cred->Release();
        }
        if (ctx->provider != nullptr)
        {
            ctx->provider->Release();
        }
        delete ctx;
    }
    if (threadStopEvent != nullptr)
    {
        CloseHandle(threadStopEvent);
    }
    if (stopEvent != nullptr)
    {
        CloseHandle(stopEvent);
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
    if (pcpe == nullptr)
    {
        StopAutoScanWatcher();
    }

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
    _adviseTickMs = GetTickCount64();
    _unlockArmed = false;
    LeaveCriticalSection(&_cs);

    if (pcpe != nullptr)
    {
        MaybeStartBackgroundAutoScan();
    }
    return S_OK;
}

IFACEMETHODIMP WinIRFaceIDProvider::UnAdvise()
{
    StopAutoScanWatcher();

    std::vector<WinIRFaceIDCredential*> snapshot;

    EnterCriticalSection(&_cs);
    if (_pEvents != nullptr)
    {
        _pEvents->Release();
        _pEvents = nullptr;
    }
    _upAdviseContext = 0;
    _autoScanTriggered = false;
    _unlockArmed = false;
    snapshot = _credentials;
    for (WinIRFaceIDCredential* cred : snapshot)
    {
        if (cred != nullptr)
        {
            cred->AddRef();
        }
    }
    LeaveCriticalSection(&_cs);

    // Stop any background IR scan immediately when LogonUI tears down (e.g. user signed in via PIN/Fingerprint).
    for (WinIRFaceIDCredential* cred : snapshot)
    {
        if (cred != nullptr)
        {
            cred->CancelScanAndWait();
            cred->Release();
        }
    }
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
        { FID_HERO_IMAGE,   CPFT_TILE_IMAGE,    L"IR Face ID",    GUID_NULL },
        { FID_LABEL,        CPFT_LARGE_TEXT,    L"IR Face ID",    CPFG_CREDENTIAL_PROVIDER_LABEL },
        { FID_STATUS,       CPFT_LARGE_TEXT,    L"Status",        GUID_NULL },
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

        std::wstring rawUserName;
        LPWSTR pszName = nullptr;
        if (SUCCEEDED(pUser->GetStringValue(PKEY_Identity_UserName, &pszName)) && pszName != nullptr)
        {
            rawUserName = pszName;
            CoTaskMemFree(pszName);
        }

        std::wstring qualifiedName;
        LPWSTR pszQualified = nullptr;
        if (SUCCEEDED(pUser->GetStringValue(PKEY_Identity_QualifiedUserName, &pszQualified)) && pszQualified != nullptr)
        {
            qualifiedName = pszQualified;
            CoTaskMemFree(pszQualified);
        }
        if (qualifiedName.empty() && !rawUserName.empty())
        {
            qualifiedName = rawUserName;
        }

        // Prefer SID -> SAM account name (e.g. "User" on Microsoft Accounts where
        // PKEY_Identity_UserName may return the email address).
        std::wstring samName = LookupUsernameFromSidString(sidStr);
        if (samName.empty())
        {
            if (!rawUserName.empty())
            {
                samName = ExtractShortUsername(rawUserName);
            }
            else if (!qualifiedName.empty())
            {
                samName = ExtractShortUsername(qualifiedName);
            }
        }

        pUser->Release();

        auto* cred = new (std::nothrow) WinIRFaceIDCredential(
            this,
            cpus,
            std::move(sidStr),
            std::move(samName),
            std::move(qualifiedName));
        if (cred != nullptr)
        {
            created.push_back(cred);
        }
    }

    EnterCriticalSection(&_cs);
    _credentials.swap(created);
    LeaveCriticalSection(&_cs);

    MaybeStartBackgroundAutoScan();

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
