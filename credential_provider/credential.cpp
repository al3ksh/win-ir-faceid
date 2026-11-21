#include "credential.h"
#include "provider.h"
#include "common.h"

#define SECURITY_WIN32
#include <ntsecapi.h>
#include <security.h>
#include <shlwapi.h>
#include <strsafe.h>

#include <cstring>

namespace
{

constexpr wchar_t kStatusScanning[] = L"Scanning infrared face...";
constexpr wchar_t kStatusRecognized[] = L"Recognized!";
constexpr wchar_t kStatusFailed[] = L"Face not recognized — use Fingerprint or PIN";
constexpr wchar_t kStatusNoPassword[] = L"Face recognized, but no saved credential — use Fingerprint or PIN";
constexpr wchar_t kStatusSignInFailed[] = L"Sign-in failed — use Fingerprint or PIN";

std::wstring GetLocalComputerNetbiosName()
{
    wchar_t buf[MAX_COMPUTERNAME_LENGTH + 1]{};
    DWORD len = ARRAYSIZE(buf);
    if (GetComputerNameW(buf, &len) && len > 0)
    {
        return std::wstring(buf, len);
    }
    return L".";
}

bool LookupAuthPackageId(ULONG& outPackageId)
{
    outPackageId = 0;
    HANDLE hLsa = nullptr;
    const NTSTATUS connectStatus = LsaConnectUntrusted(&hLsa);
    if (connectStatus < 0 || hLsa == nullptr)
    {
        return false;
    }

    auto tryPackage = [hLsa, &outPackageId](const char* name) -> bool {
        LSA_STRING pkg{};
        pkg.Buffer = const_cast<PCHAR>(name);
        pkg.Length = static_cast<USHORT>(std::strlen(name));
        pkg.MaximumLength = pkg.Length + 1;
        return LsaLookupAuthenticationPackage(hLsa, &pkg, &outPackageId) >= 0;
    };

    bool found = tryPackage(NEGOSSP_NAME_A);
    if (!found)
    {
        found = tryPackage(MSV1_0_PACKAGE_NAME);
    }

    LsaDeregisterLogonProcess(hLsa);
    return found;
}

HRESULT PackKerbInteractiveUnlockLogon(
    CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus,
    const std::wstring& domain,
    const std::wstring& username,
    const std::wstring& password,
    BYTE** outBuffer,
    ULONG* outSize)
{
    *outBuffer = nullptr;
    *outSize = 0;

    const size_t domainBytes = (domain.size() + 1) * sizeof(wchar_t);
    const size_t userBytes = (username.size() + 1) * sizeof(wchar_t);
    const size_t passBytes = (password.size() + 1) * sizeof(wchar_t);
    const size_t totalBytes = sizeof(KERB_INTERACTIVE_UNLOCK_LOGON) + domainBytes + userBytes + passBytes;

    if (totalBytes > MAXDWORD)
    {
        return E_OUTOFMEMORY;
    }

    auto* pb = static_cast<BYTE*>(CoTaskMemAlloc(totalBytes));
    if (pb == nullptr)
    {
        return E_OUTOFMEMORY;
    }
    ZeroMemory(pb, totalBytes);

    auto* pLogon = reinterpret_cast<KERB_INTERACTIVE_UNLOCK_LOGON*>(pb);
    pLogon->Logon.MessageType = (cpus == CPUS_UNLOCK_WORKSTATION)
        ? KerbWorkstationUnlockLogon
        : KerbInteractiveLogon;
    pLogon->LogonId.LowPart = 0;
    pLogon->LogonId.HighPart = 0;

    BYTE* cursor = pb + sizeof(KERB_INTERACTIVE_UNLOCK_LOGON);

    auto packString = [pb, &cursor](UNICODE_STRING& target, const std::wstring& src, size_t allocBytes) {
        std::memcpy(cursor, src.c_str(), allocBytes);
        target.Length = static_cast<USHORT>(src.size() * sizeof(wchar_t));
        target.MaximumLength = static_cast<USHORT>(allocBytes);
        // LogonUI expects Buffer to be a byte offset relative to the start of rgbSerialization.
        target.Buffer = reinterpret_cast<PWSTR>(static_cast<ULONG_PTR>(cursor - pb));
        cursor += allocBytes;
    };

    packString(pLogon->Logon.LogonDomainName, domain, domainBytes);
    packString(pLogon->Logon.UserName, username, userBytes);
    packString(pLogon->Logon.Password, password, passBytes);

    *outBuffer = pb;
    *outSize = static_cast<ULONG>(totalBytes);
    return S_OK;
}

HBITMAP CreateIRFaceTileBitmap()
{
    constexpr int kSize = 64;
    BITMAPINFO bmi{};
    bmi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bmi.bmiHeader.biWidth = kSize;
    bmi.bmiHeader.biHeight = -kSize; // top-down
    bmi.bmiHeader.biPlanes = 1;
    bmi.bmiHeader.biBitCount = 32;
    bmi.bmiHeader.biCompression = BI_RGB;

    void* bits = nullptr;
    HDC screenDc = GetDC(nullptr);
    HDC memDc = CreateCompatibleDC(screenDc);
    HBITMAP hBmp = CreateDIBSection(screenDc, &bmi, DIB_RGB_COLORS, &bits, nullptr, 0);
    if (screenDc != nullptr)
    {
        ReleaseDC(nullptr, screenDc);
    }

    if (hBmp == nullptr || memDc == nullptr)
    {
        if (memDc != nullptr) DeleteDC(memDc);
        if (hBmp != nullptr) DeleteObject(hBmp);
        return nullptr;
    }

    HGDIOBJ oldBmp = SelectObject(memDc, hBmp);

    // Dark slate background with cyan/infrared scanner reticle
    HBRUSH bgBrush = CreateSolidBrush(RGB(22, 30, 46));
    RECT fullRect{ 0, 0, kSize, kSize };
    FillRect(memDc, &fullRect, bgBrush);
    DeleteObject(bgBrush);

    HPEN ringPen = CreatePen(PS_SOLID, 2, RGB(56, 189, 248));
    HBRUSH hollowBrush = static_cast<HBRUSH>(GetStockObject(NULL_BRUSH));
    HGDIOBJ oldPen = SelectObject(memDc, ringPen);
    HGDIOBJ oldBrush = SelectObject(memDc, hollowBrush);

    // Outer scanner circle
    Ellipse(memDc, 8, 8, kSize - 8, kSize - 8);

    // Eyes / IR sensor dots
    HBRUSH accentBrush = CreateSolidBrush(RGB(244, 63, 94));
    SelectObject(memDc, accentBrush);
    Ellipse(memDc, 21, 23, 27, 29);
    Ellipse(memDc, 37, 23, 43, 29);
    DeleteObject(accentBrush);

    // Smile arc / scan line
    SelectObject(memDc, hollowBrush);
    Arc(memDc, 20, 26, 44, 46, 22, 38, 42, 38);

    SelectObject(memDc, oldBrush);
    SelectObject(memDc, oldPen);
    DeleteObject(ringPen);
    SelectObject(memDc, oldBmp);
    DeleteDC(memDc);
    return hBmp;
}

} // namespace

WinIRFaceIDCredential::WinIRFaceIDCredential(
    WinIRFaceIDProvider* pProvider,
    CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus,
    std::wstring userSid,
    std::wstring userName)
    : _pProvider(pProvider)
    , _cpus(cpus)
    , _userSid(std::move(userSid))
    , _userName(std::move(userName))
{
    DllAddRef();
    InitializeCriticalSection(&_cs);
    _cancelEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
}

WinIRFaceIDCredential::~WinIRFaceIDCredential()
{
    CancelScanAndWait();

    EnterCriticalSection(&_cs);
    WipeVerifiedCredentialsLocked();
    if (_pCredEvents != nullptr)
    {
        _pCredEvents->Release();
        _pCredEvents = nullptr;
    }
    _pProvider = nullptr;
    LeaveCriticalSection(&_cs);

    if (_cancelEvent != nullptr)
    {
        CloseHandle(_cancelEvent);
        _cancelEvent = nullptr;
    }

    DeleteCriticalSection(&_cs);
    DllRelease();
}

void WinIRFaceIDCredential::WipeVerifiedCredentialsLocked()
{
    SecureWipeString(_verifiedPassword);
    SecureWipeString(_verifiedUsername);
    SecureWipeString(_verifiedDomain);
    _hasVerifiedCred = false;
}

void WinIRFaceIDCredential::DetachProvider()
{
    CancelScanAndWait();
    EnterCriticalSection(&_cs);
    _pProvider = nullptr;
    LeaveCriticalSection(&_cs);
}

bool WinIRFaceIDCredential::HasVerifiedCredential()
{
    EnterCriticalSection(&_cs);
    const bool ready = _hasVerifiedCred && !_verifiedPassword.empty() && !_verifiedUsername.empty();
    LeaveCriticalSection(&_cs);
    return ready;
}

IFACEMETHODIMP WinIRFaceIDCredential::QueryInterface(REFIID riid, void** ppv)
{
    static const QITAB qit[] = {
        QITABENT(WinIRFaceIDCredential, ICredentialProviderCredential2),
        QITABENTMULTI(WinIRFaceIDCredential, ICredentialProviderCredential, ICredentialProviderCredential2),
        { 0 }
    };
    return QISearch(this, qit, riid, ppv);
}

IFACEMETHODIMP_(ULONG) WinIRFaceIDCredential::AddRef()
{
    return static_cast<ULONG>(InterlockedIncrement(&_refCount));
}

IFACEMETHODIMP_(ULONG) WinIRFaceIDCredential::Release()
{
    const LONG count = InterlockedDecrement(&_refCount);
    if (count == 0)
    {
        delete this;
    }
    return static_cast<ULONG>(count);
}

IFACEMETHODIMP WinIRFaceIDCredential::Advise(ICredentialProviderCredentialEvents* pcpce)
{
    bool shouldStart = false;

    EnterCriticalSection(&_cs);
    if (_pCredEvents != nullptr)
    {
        _pCredEvents->Release();
        _pCredEvents = nullptr;
    }
    _pCredEvents = pcpce;
    if (_pCredEvents != nullptr)
    {
        _pCredEvents->AddRef();
    }
    shouldStart = _selected && !_scanning && !_hasVerifiedCred;
    LeaveCriticalSection(&_cs);

    if (shouldStart)
    {
        StartScan();
    }
    return S_OK;
}

IFACEMETHODIMP WinIRFaceIDCredential::UnAdvise()
{
    CancelScanAndWait();

    EnterCriticalSection(&_cs);
    if (_pCredEvents != nullptr)
    {
        _pCredEvents->Release();
        _pCredEvents = nullptr;
    }
    _selected = false;
    LeaveCriticalSection(&_cs);

    return S_OK;
}

IFACEMETHODIMP WinIRFaceIDCredential::SetSelected(BOOL* pbAutoLogon)
{
    if (pbAutoLogon == nullptr)
    {
        return E_POINTER;
    }

    EnterCriticalSection(&_cs);
    _selected = true;
    const bool alreadyVerified = _hasVerifiedCred && !_verifiedPassword.empty() && !_verifiedUsername.empty();
    const bool alreadyScanning = _scanning;
    LeaveCriticalSection(&_cs);

    if (alreadyVerified)
    {
        *pbAutoLogon = TRUE;
        return S_OK;
    }

    *pbAutoLogon = FALSE;
    if (!alreadyScanning)
    {
        StartScan();
    }
    return S_OK;
}

IFACEMETHODIMP WinIRFaceIDCredential::SetDeselected()
{
    EnterCriticalSection(&_cs);
    _selected = false;
    LeaveCriticalSection(&_cs);

    CancelScanAndWait();
    return S_OK;
}

IFACEMETHODIMP WinIRFaceIDCredential::GetFieldState(
    DWORD dwFieldID,
    CREDENTIAL_PROVIDER_FIELD_STATE* pcpfs,
    CREDENTIAL_PROVIDER_FIELD_INTERACTIVE_STATE* pcpfis)
{
    if (pcpfs == nullptr || pcpfis == nullptr)
    {
        return E_POINTER;
    }

    EnterCriticalSection(&_cs);
    const bool scanning = _scanning;
    LeaveCriticalSection(&_cs);

    switch (dwFieldID)
    {
    case FID_TILE_IMAGE:
    case FID_LABEL:
        *pcpfs = CPFS_DISPLAY_IN_BOTH;
        *pcpfis = CPFIS_NONE;
        return S_OK;

    case FID_STATUS:
        *pcpfs = CPFS_DISPLAY_IN_SELECTED_TILE;
        *pcpfis = CPFIS_READONLY;
        return S_OK;

    case FID_RETRY_SUBMIT:
        *pcpfs = CPFS_DISPLAY_IN_SELECTED_TILE;
        *pcpfis = scanning ? CPFIS_DISABLED : CPFIS_FOCUSED;
        return S_OK;

    default:
        return E_INVALIDARG;
    }
}

IFACEMETHODIMP WinIRFaceIDCredential::GetStringValue(DWORD dwFieldID, LPWSTR* ppsz)
{
    if (ppsz == nullptr)
    {
        return E_POINTER;
    }
    *ppsz = nullptr;

    switch (dwFieldID)
    {
    case FID_LABEL:
        return SHStrDupW(L"IR Face ID", ppsz);

    case FID_STATUS:
    {
        EnterCriticalSection(&_cs);
        std::wstring status = _statusText;
        LeaveCriticalSection(&_cs);
        return SHStrDupW(status.c_str(), ppsz);
    }

    case FID_RETRY_SUBMIT:
        return SHStrDupW(L"Retry IR Scan", ppsz);

    default:
        return E_INVALIDARG;
    }
}

IFACEMETHODIMP WinIRFaceIDCredential::GetBitmapValue(DWORD dwFieldID, HBITMAP* phbmp)
{
    if (phbmp == nullptr)
    {
        return E_POINTER;
    }
    *phbmp = nullptr;

    if (dwFieldID != FID_TILE_IMAGE)
    {
        return E_INVALIDARG;
    }

    *phbmp = CreateIRFaceTileBitmap();
    return (*phbmp != nullptr) ? S_OK : E_FAIL;
}

IFACEMETHODIMP WinIRFaceIDCredential::GetCheckboxValue(
    DWORD /*dwFieldID*/,
    BOOL* /*pbChecked*/,
    LPWSTR* /*ppszLabel*/)
{
    return E_NOTIMPL;
}

IFACEMETHODIMP WinIRFaceIDCredential::GetSubmitButtonValue(DWORD dwFieldID, DWORD* pdwAdjacentTo)
{
    if (pdwAdjacentTo == nullptr)
    {
        return E_POINTER;
    }
    if (dwFieldID != FID_RETRY_SUBMIT)
    {
        return E_INVALIDARG;
    }
    *pdwAdjacentTo = FID_STATUS;
    return S_OK;
}

IFACEMETHODIMP WinIRFaceIDCredential::GetComboBoxValueCount(
    DWORD /*dwFieldID*/,
    DWORD* /*pcItems*/,
    DWORD* /*pdwSelectedItem*/)
{
    return E_NOTIMPL;
}

IFACEMETHODIMP WinIRFaceIDCredential::GetComboBoxValueAt(
    DWORD /*dwFieldID*/,
    DWORD /*dwItem*/,
    LPWSTR* /*ppszItem*/)
{
    return E_NOTIMPL;
}

IFACEMETHODIMP WinIRFaceIDCredential::SetStringValue(DWORD /*dwFieldID*/, LPCWSTR /*psz*/)
{
    return E_NOTIMPL;
}

IFACEMETHODIMP WinIRFaceIDCredential::SetCheckboxValue(DWORD /*dwFieldID*/, BOOL /*bChecked*/)
{
    return E_NOTIMPL;
}

IFACEMETHODIMP WinIRFaceIDCredential::SetComboBoxSelectedValue(DWORD /*dwFieldID*/, DWORD /*dwSelectedItem*/)
{
    return E_NOTIMPL;
}

IFACEMETHODIMP WinIRFaceIDCredential::CommandLinkClicked(DWORD /*dwFieldID*/)
{
    return E_NOTIMPL;
}

IFACEMETHODIMP WinIRFaceIDCredential::GetSerialization(
    CREDENTIAL_PROVIDER_GET_SERIALIZATION_RESPONSE* pcpgsr,
    CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION* pcpcs,
    LPWSTR* ppszOptionalStatusText,
    CREDENTIAL_PROVIDER_STATUS_ICON* pcpsiOptionalStatusIcon)
{
    if (pcpgsr == nullptr || pcpcs == nullptr)
    {
        return E_POINTER;
    }

    *pcpgsr = CPGSR_NO_CREDENTIAL_NOT_FINISHED;
    ZeroMemory(pcpcs, sizeof(*pcpcs));
    if (ppszOptionalStatusText != nullptr)
    {
        *ppszOptionalStatusText = nullptr;
    }
    if (pcpsiOptionalStatusIcon != nullptr)
    {
        *pcpsiOptionalStatusIcon = CPSI_NONE;
    }

    std::wstring username;
    std::wstring domain;
    std::wstring password;
    bool hasCred = false;
    bool isScanning = false;

    EnterCriticalSection(&_cs);
    if (_hasVerifiedCred && !_verifiedPassword.empty() && !_verifiedUsername.empty())
    {
        username = _verifiedUsername;
        domain = _verifiedDomain;
        password = _verifiedPassword;
        hasCred = true;
        WipeVerifiedCredentialsLocked();
    }
    else
    {
        isScanning = _scanning;
    }
    LeaveCriticalSection(&_cs);

    if (!hasCred)
    {
        // Clicking the "Retry IR Scan" submit button triggers GetSerialization;
        // restart scanning if idle without dismissing the credential tile.
        if (!isScanning)
        {
            StartScan();
        }
        return S_OK;
    }

    const size_t slashPos = username.find(L'\\');
    if (slashPos != std::wstring::npos)
    {
        domain = username.substr(0, slashPos);
        username = username.substr(slashPos + 1);
    }
    else if (username.find(L'@') != std::wstring::npos && (domain.empty() || domain == L"."))
    {
        domain.clear();
    }
    else if (domain.empty() || domain == L".")
    {
        domain = GetLocalComputerNetbiosName();
    }

    ULONG authPackage = 0;
    if (!LookupAuthPackageId(authPackage))
    {
        SecureWipeString(password);
        SecureWipeString(username);
        SecureWipeString(domain);
        if (ppszOptionalStatusText != nullptr)
        {
            SHStrDupW(kStatusSignInFailed, ppszOptionalStatusText);
        }
        if (pcpsiOptionalStatusIcon != nullptr)
        {
            *pcpsiOptionalStatusIcon = CPSI_ERROR;
        }
        return E_FAIL;
    }

    BYTE* packedBuffer = nullptr;
    ULONG packedSize = 0;
    const HRESULT hr = PackKerbInteractiveUnlockLogon(
        _cpus,
        domain,
        username,
        password,
        &packedBuffer,
        &packedSize);

    SecureWipeString(password);
    SecureWipeString(username);
    SecureWipeString(domain);

    if (FAILED(hr))
    {
        return hr;
    }

    pcpcs->ulAuthenticationPackage = authPackage;
    pcpcs->clsidCredentialProvider = CLSID_WinIRFaceIDProvider;
    pcpcs->cbSerialization = packedSize;
    pcpcs->rgbSerialization = packedBuffer;
    *pcpgsr = CPGSR_RETURN_CREDENTIAL_FINISHED;
    return S_OK;
}

IFACEMETHODIMP WinIRFaceIDCredential::ReportResult(
    NTSTATUS ntsStatus,
    NTSTATUS /*ntsSubstatus*/,
    LPWSTR* ppszOptionalStatusText,
    CREDENTIAL_PROVIDER_STATUS_ICON* pcpsiOptionalStatusIcon)
{
    EnterCriticalSection(&_cs);
    WipeVerifiedCredentialsLocked();
    if (ntsStatus < 0)
    {
        _statusText = kStatusSignInFailed;
    }
    LeaveCriticalSection(&_cs);

    if (ppszOptionalStatusText != nullptr)
    {
        *ppszOptionalStatusText = nullptr;
        if (ntsStatus < 0)
        {
            SHStrDupW(kStatusSignInFailed, ppszOptionalStatusText);
        }
    }
    if (pcpsiOptionalStatusIcon != nullptr)
    {
        *pcpsiOptionalStatusIcon = (ntsStatus < 0) ? CPSI_ERROR : CPSI_NONE;
    }
    return S_OK;
}

IFACEMETHODIMP WinIRFaceIDCredential::GetUserSid(LPWSTR* sid)
{
    if (sid == nullptr)
    {
        return E_POINTER;
    }
    *sid = nullptr;

    if (_userSid.empty())
    {
        return S_FALSE;
    }
    return SHStrDupW(_userSid.c_str(), sid);
}

void WinIRFaceIDCredential::StartScan()
{
    HANDLE threadToClose = nullptr;

    EnterCriticalSection(&_cs);
    if (_scanning || _pProvider == nullptr || _cancelEvent == nullptr)
    {
        LeaveCriticalSection(&_cs);
        return;
    }

    // Reap any previously finished scan thread handle before starting a new one.
    if (_hScanThread != nullptr)
    {
        threadToClose = _hScanThread;
        _hScanThread = nullptr;
    }

    WipeVerifiedCredentialsLocked();
    ResetEvent(_cancelEvent);
    _scanning = true;
    _statusText = kStatusScanning;
    AddRef(); // Balanced in ScanThreadProc before exit
    HANDLE hThread = CreateThread(nullptr, 0, ScanThreadProc, this, 0, nullptr);
    if (hThread != nullptr)
    {
        _hScanThread = hThread;
    }
    else
    {
        _scanning = false;
        _statusText = kStatusFailed;
    }
    LeaveCriticalSection(&_cs);

    if (threadToClose != nullptr)
    {
        WaitForSingleObject(threadToClose, 1000);
        CloseHandle(threadToClose);
    }

    if (hThread == nullptr)
    {
        Release();
        UpdateStatusText(kStatusFailed, true);
    }
    else
    {
        UpdateStatusText(kStatusScanning, true);
    }
}

void WinIRFaceIDCredential::CancelScanAndWait()
{
    HANDLE hThread = nullptr;

    EnterCriticalSection(&_cs);
    if (_cancelEvent != nullptr)
    {
        SetEvent(_cancelEvent);
    }
    if (_hScanThread != nullptr)
    {
        hThread = _hScanThread;
        _hScanThread = nullptr;
    }
    LeaveCriticalSection(&_cs);

    if (hThread != nullptr)
    {
        WaitForSingleObject(hThread, 2500);
        CloseHandle(hThread);
    }
}

void WinIRFaceIDCredential::UpdateStatusText(const std::wstring& newText, bool updateButtonState)
{
    ICredentialProviderCredentialEvents* pEvents = nullptr;
    bool scanning = false;

    EnterCriticalSection(&_cs);
    _statusText = newText;
    scanning = _scanning;
    if (_pCredEvents != nullptr)
    {
        pEvents = _pCredEvents;
        pEvents->AddRef();
    }
    LeaveCriticalSection(&_cs);

    if (pEvents != nullptr)
    {
        pEvents->SetFieldString(this, FID_STATUS, newText.c_str());
        if (updateButtonState)
        {
            pEvents->SetFieldInteractiveState(
                this,
                FID_RETRY_SUBMIT,
                scanning ? CPFIS_DISABLED : CPFIS_FOCUSED);
        }
        pEvents->Release();
    }
}

DWORD WINAPI WinIRFaceIDCredential::ScanThreadProc(LPVOID param)
{
    auto* self = static_cast<WinIRFaceIDCredential*>(param);
    if (self != nullptr)
    {
        self->RunScan();
        self->Release();
    }
    return 0;
}

void WinIRFaceIDCredential::RunScan()
{
    pipe_client::VerifyResult result;
    const bool matched = pipe_client::VerifyFace(
        _userName,
        kDefaultVerifyTimeoutMs,
        _cancelEvent,
        result);

    if (result.status == pipe_client::VerifyStatus::Cancelled)
    {
        EnterCriticalSection(&_cs);
        _scanning = false;
        LeaveCriticalSection(&_cs);
        return;
    }

    if (matched && !result.username.empty() && !result.password.empty())
    {
        WinIRFaceIDProvider* provider = nullptr;

        EnterCriticalSection(&_cs);
        _verifiedUsername = std::move(result.username);
        _verifiedDomain = std::move(result.domain);
        _verifiedPassword = std::move(result.password);
        _hasVerifiedCred = true;
        _scanning = false;
        provider = _pProvider;
        LeaveCriticalSection(&_cs);

        result.Clear();
        UpdateStatusText(kStatusRecognized, true);

        if (provider != nullptr)
        {
            provider->NotifyCredentialsChanged();
        }
        return;
    }

    result.Clear();

    EnterCriticalSection(&_cs);
    _scanning = false;
    LeaveCriticalSection(&_cs);

    if (matched)
    {
        UpdateStatusText(kStatusNoPassword, true);
    }
    else
    {
        UpdateStatusText(kStatusFailed, true);
    }
}
