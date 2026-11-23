#include "credential.h"
#include "provider.h"
#include "common.h"
#include "resource.h"

#define SECURITY_WIN32
#include <ntsecapi.h>
#include <security.h>
#include <shlwapi.h>
#include <strsafe.h>
#include <wincodec.h>

#include <cstring>
#include <vector>

namespace
{

constexpr wchar_t kStatusReadyDeferred[] = L"Scan face";
constexpr wchar_t kStatusScanning[] = L"Looking for you...";
constexpr wchar_t kStatusRecognized[] = L"Welcome back!";
constexpr wchar_t kStatusFailed[] = L"Couldn't recognize you";
constexpr wchar_t kStatusServiceOffline[] = L"Face ID service offline - use Fingerprint or PIN";
constexpr wchar_t kStatusNoPassword[] = L"Face recognized, but no saved credential - use Fingerprint or PIN";
constexpr wchar_t kStatusSignInFailed[] = L"Sign-in failed - use Fingerprint or PIN";

constexpr DWORD kEnterFrameMs = 25;
constexpr DWORD kScanFrameMs = 28;
constexpr DWORD kUnlockFrameMs = 25;
constexpr DWORD kFailFrameMs = 25;
constexpr DWORD kUnlockPreviewWaitMs = 300;

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

template <typename T>
class ComRef
{
public:
    ComRef() = default;
    ~ComRef()
    {
        Reset();
    }

    ComRef(const ComRef&) = delete;
    ComRef& operator=(const ComRef&) = delete;

    T* Get() const { return _ptr; }
    T** GetAddressOf()
    {
        Reset();
        return &_ptr;
    }
    T* operator->() const { return _ptr; }

    void Reset()
    {
        if (_ptr != nullptr)
        {
            _ptr->Release();
            _ptr = nullptr;
        }
    }

private:
    T* _ptr = nullptr;
};

void FillBitmapV5Header(BITMAPV5HEADER& hdr, UINT width, UINT height)
{
    ZeroMemory(&hdr, sizeof(hdr));
    hdr.bV5Size = sizeof(BITMAPV5HEADER);
    hdr.bV5Width = static_cast<LONG>(width);
    hdr.bV5Height = -static_cast<LONG>(height); // top-down
    hdr.bV5Planes = 1;
    hdr.bV5BitCount = 32;
    hdr.bV5Compression = BI_BITFIELDS;
    hdr.bV5SizeImage = width * height * 4U;
    hdr.bV5RedMask = 0x00FF0000;
    hdr.bV5GreenMask = 0x0000FF00;
    hdr.bV5BlueMask = 0x000000FF;
    hdr.bV5AlphaMask = 0xFF000000;
    hdr.bV5CSType = LCS_sRGB;
    hdr.bV5Intent = LCS_GM_IMAGES;
}

HBITMAP CreateTopDown32BppDib(int width, int height, void** outBits)
{
    if (outBits == nullptr || width <= 0 || height <= 0)
    {
        return nullptr;
    }
    *outBits = nullptr;

    BITMAPV5HEADER bV5{};
    FillBitmapV5Header(bV5, static_cast<UINT>(width), static_cast<UINT>(height));

    HDC screenDc = GetDC(nullptr);
    HBITMAP hBmp = CreateDIBSection(
        screenDc,
        reinterpret_cast<const BITMAPINFO*>(&bV5),
        DIB_RGB_COLORS,
        outBits,
        nullptr,
        0);
    if (screenDc != nullptr)
    {
        ReleaseDC(nullptr, screenDc);
    }

    if (hBmp == nullptr || *outBits == nullptr)
    {
        if (hBmp != nullptr)
        {
            DeleteObject(hBmp);
        }
        *outBits = nullptr;
        return nullptr;
    }

    ZeroMemory(*outBits, static_cast<size_t>(width) * static_cast<size_t>(height) * 4U);
    return hBmp;
}

bool LoadEmbeddedPngResource(UINT resId, const BYTE** ppData, DWORD* pSize)
{
    if (ppData == nullptr || pSize == nullptr)
    {
        return false;
    }
    *ppData = nullptr;
    *pSize = 0;

    HINSTANCE hInst = GetModuleInstance();
    if (hInst == nullptr)
    {
        return false;
    }

    HRSRC hRes = FindResourceW(hInst, MAKEINTRESOURCEW(resId), RT_RCDATA);
    if (hRes == nullptr)
    {
        return false;
    }

    const DWORD size = SizeofResource(hInst, hRes);
    HGLOBAL hResData = LoadResource(hInst, hRes);
    if (hResData == nullptr || size == 0)
    {
        return false;
    }

    const void* bytes = LockResource(hResData);
    if (bytes == nullptr)
    {
        return false;
    }

    *ppData = static_cast<const BYTE*>(bytes);
    *pSize = size;
    return true;
}

bool LoadEmbeddedResourceBytes(UINT resourceId, const void*& outBytes, DWORD& outSize)
{
    const BYTE* ptr = nullptr;
    if (!LoadEmbeddedPngResource(resourceId, &ptr, &outSize))
    {
        outBytes = nullptr;
        return false;
    }
    outBytes = ptr;
    return true;
}

bool DecodePngResourceToPbgra(
    UINT resourceId,
    UINT& outWidth,
    UINT& outHeight,
    std::vector<BYTE>& outPixels)
{
    outWidth = 0;
    outHeight = 0;
    outPixels.clear();

    const void* pngBytes = nullptr;
    DWORD pngSize = 0;
    if (!LoadEmbeddedResourceBytes(resourceId, pngBytes, pngSize))
    {
        return false;
    }

    const HRESULT hrCom = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);
    if (FAILED(hrCom) && hrCom != RPC_E_CHANGED_MODE)
    {
        return false;
    }
    const bool shouldUninit = (hrCom == S_OK || hrCom == S_FALSE);

    bool decoded = false;
    {
        ComRef<IWICImagingFactory> factory;
        ComRef<IWICStream> stream;
        ComRef<IWICBitmapDecoder> decoder;
        ComRef<IWICBitmapFrameDecode> frame;
        ComRef<IWICFormatConverter> converter;

        HRESULT hr = CoCreateInstance(
            CLSID_WICImagingFactory,
            nullptr,
            CLSCTX_INPROC_SERVER,
            IID_PPV_ARGS(factory.GetAddressOf()));

        if (SUCCEEDED(hr))
        {
            hr = factory->CreateStream(stream.GetAddressOf());
        }
        if (SUCCEEDED(hr))
        {
            hr = stream->InitializeFromMemory(
                static_cast<WICInProcPointer>(const_cast<void*>(pngBytes)),
                pngSize);
        }
        if (SUCCEEDED(hr))
        {
            hr = factory->CreateDecoderFromStream(
                stream.Get(),
                nullptr,
                WICDecodeMetadataCacheOnLoad,
                decoder.GetAddressOf());
        }
        if (SUCCEEDED(hr))
        {
            hr = decoder->GetFrame(0, frame.GetAddressOf());
        }
        if (SUCCEEDED(hr))
        {
            hr = factory->CreateFormatConverter(converter.GetAddressOf());
        }
        if (SUCCEEDED(hr))
        {
            hr = converter->Initialize(
                frame.Get(),
                GUID_WICPixelFormat32bppPBGRA,
                WICBitmapDitherTypeNone,
                nullptr,
                0.0,
                WICBitmapPaletteTypeCustom);
        }

        UINT width = 0;
        UINT height = 0;
        if (SUCCEEDED(hr))
        {
            hr = converter->GetSize(&width, &height);
        }

        if (SUCCEEDED(hr) && width > 0 && height > 0 && width <= 4096 && height <= 4096)
        {
            const UINT stride = width * 4U;
            const UINT bufferSize = stride * height;
            outPixels.resize(bufferSize);
            hr = converter->CopyPixels(nullptr, stride, bufferSize, outPixels.data());
            if (SUCCEEDED(hr))
            {
                outWidth = width;
                outHeight = height;
                decoded = true;
            }
            else
            {
                outPixels.clear();
            }
        }
    }

    if (shouldUninit)
    {
        CoUninitialize();
    }

    return decoded;
}

void CreateFallbackIRFacePixels(
    UINT& outWidth,
    UINT& outHeight,
    std::vector<BYTE>& outPixels)
{
    constexpr UINT kSize = 64;
    outWidth = kSize;
    outHeight = kSize;
    outPixels.assign(static_cast<size_t>(kSize) * kSize * 4U, 0);

    // Minimalist pure-white (0xFFFFFFFF premultiplied BGRA) face brackets on 100% transparent background
    auto* pixels = reinterpret_cast<uint32_t*>(outPixels.data());
    auto fillRect = [pixels, kSize](UINT x0, UINT y0, UINT x1, UINT y1) {
        for (UINT y = y0; y < y1; ++y)
        {
            for (UINT x = x0; x < x1; ++x)
            {
                if (x < kSize && y < kSize)
                {
                    pixels[y * kSize + x] = 0xFFFFFFFFU;
                }
            }
        }
    };

    // Corner brackets
    fillRect(10, 10, 22, 13); fillRect(10, 10, 13, 22);
    fillRect(42, 10, 54, 13); fillRect(51, 10, 54, 22);
    fillRect(10, 51, 22, 54); fillRect(10, 42, 13, 54);
    fillRect(42, 51, 54, 54); fillRect(51, 42, 54, 54);
    // Eyes and mouth
    fillRect(23, 24, 26, 30);
    fillRect(38, 24, 41, 30);
    fillRect(24, 40, 40, 43);
}

void GetIRFaceTilePbgra(
    UINT resourceId,
    UINT& outWidth,
    UINT& outHeight,
    std::vector<BYTE>& outPixels)
{
    if (!DecodePngResourceToPbgra(resourceId, outWidth, outHeight, outPixels))
    {
        if (resourceId == IDR_IR_FACEID_PNG ||
            !DecodePngResourceToPbgra(IDR_IR_FACEID_PNG, outWidth, outHeight, outPixels))
        {
            CreateFallbackIRFacePixels(outWidth, outHeight, outPixels);
        }
    }
}

HBITMAP CreateIRFaceTileBitmap(UINT resourceId)
{
    UINT width = 0;
    UINT height = 0;
    std::vector<BYTE> pixels;
    GetIRFaceTilePbgra(resourceId, width, height, pixels);
    if (width == 0 || height == 0 || pixels.empty())
    {
        return nullptr;
    }

    void* bits = nullptr;
    HBITMAP hBmp = CreateTopDown32BppDib(static_cast<int>(width), static_cast<int>(height), &bits);
    if (hBmp == nullptr)
    {
        return nullptr;
    }

    std::memcpy(bits, pixels.data(), pixels.size());
    return hBmp;
}

HRESULT CreateIRFaceTileBitmapBuffer(UINT resourceId, DWORD* outSize, BYTE** outBuffer)
{
    if (outSize == nullptr || outBuffer == nullptr)
    {
        return E_POINTER;
    }
    *outSize = 0;
    *outBuffer = nullptr;

    const BYTE* pngBytes = nullptr;
    DWORD pngSize = 0;
    if (!LoadEmbeddedPngResource(resourceId, &pngBytes, &pngSize))
    {
        if (resourceId == IDR_IR_FACEID_PNG ||
            !LoadEmbeddedPngResource(IDR_IR_FACEID_PNG, &pngBytes, &pngSize))
        {
            return E_FAIL;
        }
    }

    auto* buffer = static_cast<BYTE*>(CoTaskMemAlloc(pngSize));
    if (buffer == nullptr)
    {
        return E_OUTOFMEMORY;
    }

    std::memcpy(buffer, pngBytes, pngSize);

    *outSize = pngSize;
    *outBuffer = buffer;
    return S_OK;
}

} // namespace

WinIRFaceIDCredential::WinIRFaceIDCredential(
    WinIRFaceIDProvider* pProvider,
    CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus,
    std::wstring userSid,
    std::wstring userName,
    std::wstring qualifiedName)
    : _pProvider(pProvider)
    , _cpus(cpus)
    , _userSid(std::move(userSid))
    , _userName(std::move(userName))
    , _qualifiedName(std::move(qualifiedName))
{
    DllAddRef();
    InitializeCriticalSection(&_cs);
    _cancelEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    _animStopEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    _animWakeEvent = CreateEventW(nullptr, FALSE, FALSE, nullptr);
}

WinIRFaceIDCredential::~WinIRFaceIDCredential()
{
    CancelScanAndWait();
    StopAnimThreadAndWait();

    ICredentialProviderCredentialEvents* pEventsToRelease = nullptr;
    ICredentialProviderCredentialEvents3* pEvents3ToRelease = nullptr;

    EnterCriticalSection(&_cs);
    WipeVerifiedCredentialsLocked();
    pEvents3ToRelease = _pCredEvents3;
    _pCredEvents3 = nullptr;
    pEventsToRelease = _pCredEvents;
    _pCredEvents = nullptr;
    _pProvider = nullptr;
    LeaveCriticalSection(&_cs);

    if (pEvents3ToRelease != nullptr)
    {
        pEvents3ToRelease->Release();
    }
    if (pEventsToRelease != nullptr)
    {
        pEventsToRelease->Release();
    }

    if (_cancelEvent != nullptr)
    {
        CloseHandle(_cancelEvent);
        _cancelEvent = nullptr;
    }
    if (_animStopEvent != nullptr)
    {
        CloseHandle(_animStopEvent);
        _animStopEvent = nullptr;
    }
    if (_animWakeEvent != nullptr)
    {
        CloseHandle(_animWakeEvent);
        _animWakeEvent = nullptr;
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
    StopAnimThreadAndWait();
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
        QITABENT(WinIRFaceIDCredential, ICredentialProviderCredential3),
        QITABENTMULTI(WinIRFaceIDCredential, ICredentialProviderCredential2, ICredentialProviderCredential3),
        QITABENTMULTI(WinIRFaceIDCredential, ICredentialProviderCredential, ICredentialProviderCredential3),
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
    bool canStart = false;
    bool isSelected = false;
    bool isScanning = false;
    bool hasCredEvents3 = false;
    WinIRFaceIDProvider* provider = nullptr;
    ICredentialProviderCredentialEvents* oldEvents = nullptr;
    ICredentialProviderCredentialEvents3* oldEvents3 = nullptr;
    ICredentialProviderCredentialEvents3* newEvents3 = nullptr;

    if (pcpce != nullptr)
    {
        if (FAILED(pcpce->QueryInterface(
                __uuidof(ICredentialProviderCredentialEvents3),
                reinterpret_cast<void**>(&newEvents3))))
        {
            newEvents3 = nullptr;
        }
    }

    EnterCriticalSection(&_cs);
    oldEvents = _pCredEvents;
    _pCredEvents = pcpce;
    if (_pCredEvents != nullptr)
    {
        _pCredEvents->AddRef();
    }

    oldEvents3 = _pCredEvents3;
    _pCredEvents3 = newEvents3; // Transfer reference from QueryInterface

    canStart = (pcpce != nullptr) && !_scanning && !_hasVerifiedCred;
    isSelected = _selected;
    isScanning = _scanning;
    hasCredEvents3 = (_pCredEvents3 != nullptr);
    if (canStart && _pProvider != nullptr)
    {
        provider = _pProvider;
        provider->AddRef();
    }
    LeaveCriticalSection(&_cs);

    if (oldEvents3 != nullptr)
    {
        oldEvents3->Release();
    }
    if (oldEvents != nullptr)
    {
        oldEvents->Release();
    }

    if (canStart)
    {
        if (isSelected)
        {
            if (provider != nullptr && provider->ShouldDeferInitialUnlockScan())
            {
                SetHeroAnimState(HeroAnimState::Idle);
                UpdateStatusText(kStatusReadyDeferred, true);
            }
            else
            {
                StartScan();
            }
        }
        else if (provider != nullptr && !provider->ShouldDeferInitialUnlockScan())
        {
            StartScan();
        }
    }
    else if (isSelected && hasCredEvents3 && isScanning)
    {
        EnsureAnimThreadStarted();
    }

    if (provider != nullptr)
    {
        provider->Release();
    }
    return S_OK;
}

IFACEMETHODIMP WinIRFaceIDCredential::UnAdvise()
{
    ICredentialProviderCredentialEvents* pEventsToRelease = nullptr;
    ICredentialProviderCredentialEvents3* pEvents3ToRelease = nullptr;

    EnterCriticalSection(&_cs);
    pEventsToRelease = _pCredEvents;
    _pCredEvents = nullptr;
    pEvents3ToRelease = _pCredEvents3;
    _pCredEvents3 = nullptr;
    _selected = false;
    _animState = HeroAnimState::Idle;
    ++_animGeneration;
    _currentHeroResId = IDR_IR_FACEID_PNG;
    LeaveCriticalSection(&_cs);

    CancelScanAndWait();
    StopAnimThreadAndWait();

    if (pEvents3ToRelease != nullptr)
    {
        pEvents3ToRelease->Release();
    }
    if (pEventsToRelease != nullptr)
    {
        pEventsToRelease->Release();
    }

    return S_OK;
}

IFACEMETHODIMP WinIRFaceIDCredential::SetSelected(BOOL* pbAutoLogon)
{
    if (pbAutoLogon == nullptr)
    {
        return E_POINTER;
    }

    WinIRFaceIDProvider* provider = nullptr;
    EnterCriticalSection(&_cs);
    _selected = true;
    const bool alreadyVerified = _hasVerifiedCred && !_verifiedPassword.empty() && !_verifiedUsername.empty();
    const bool alreadyScanning = _scanning;
    if (!alreadyVerified && !alreadyScanning && _pProvider != nullptr)
    {
        provider = _pProvider;
        provider->AddRef();
    }
    LeaveCriticalSection(&_cs);

    if (alreadyVerified)
    {
        *pbAutoLogon = TRUE;
        return S_OK;
    }

    *pbAutoLogon = FALSE;
    if (alreadyScanning)
    {
        EnsureAnimThreadStarted();
    }
    else
    {
        if (provider != nullptr && provider->ShouldDeferInitialUnlockScan())
        {
            SetHeroAnimState(HeroAnimState::Idle);
            UpdateStatusText(kStatusReadyDeferred, true);
        }
        else
        {
            StartScan();
        }
    }

    if (provider != nullptr)
    {
        provider->Release();
    }
    return S_OK;
}

IFACEMETHODIMP WinIRFaceIDCredential::SetDeselected()
{
    EnterCriticalSection(&_cs);
    _selected = false;
    _animState = HeroAnimState::Idle;
    ++_animGeneration;
    _currentHeroResId = IDR_IR_FACEID_PNG;
    LeaveCriticalSection(&_cs);

    StopAnimThreadAndWait();
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
        *pcpfs = CPFS_DISPLAY_IN_BOTH;
        *pcpfis = CPFIS_NONE;
        return S_OK;

    case FID_HERO_IMAGE:
        *pcpfs = CPFS_DISPLAY_IN_SELECTED_TILE;
        *pcpfis = CPFIS_NONE;
        return S_OK;

    case FID_LABEL:
        *pcpfs = CPFS_DISPLAY_IN_DESELECTED_TILE;
        *pcpfis = CPFIS_NONE;
        return S_OK;

    case FID_STATUS:
        *pcpfs = CPFS_DISPLAY_IN_SELECTED_TILE;
        *pcpfis = CPFIS_READONLY;
        return S_OK;

    case FID_RETRY_SUBMIT:
        *pcpfs = scanning ? CPFS_HIDDEN : CPFS_DISPLAY_IN_SELECTED_TILE;
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

    if (dwFieldID != FID_TILE_IMAGE && dwFieldID != FID_HERO_IMAGE)
    {
        return E_INVALIDARG;
    }

    UINT resId = IDR_IR_FACEID_PNG;
    if (dwFieldID == FID_HERO_IMAGE)
    {
        EnterCriticalSection(&_cs);
        if (_currentHeroResId != 0)
        {
            resId = _currentHeroResId;
        }
        LeaveCriticalSection(&_cs);
    }

    *phbmp = CreateIRFaceTileBitmap(resId);
    return (*phbmp != nullptr) ? S_OK : E_FAIL;
}

IFACEMETHODIMP WinIRFaceIDCredential::GetBitmapBufferValue(
    DWORD dwFieldID,
    DWORD* pImageBufferSize,
    BYTE** ppImageBuffer)
{
    if (pImageBufferSize == nullptr || ppImageBuffer == nullptr)
    {
        return E_POINTER;
    }
    *pImageBufferSize = 0;
    *ppImageBuffer = nullptr;

    if (dwFieldID != FID_TILE_IMAGE && dwFieldID != FID_HERO_IMAGE)
    {
        return E_INVALIDARG;
    }

    UINT resId = IDR_IR_FACEID_PNG;
    if (dwFieldID == FID_HERO_IMAGE)
    {
        EnterCriticalSection(&_cs);
        if (_currentHeroResId != 0)
        {
            resId = _currentHeroResId;
        }
        LeaveCriticalSection(&_cs);
    }

    return CreateIRFaceTileBitmapBuffer(resId, pImageBufferSize, ppImageBuffer);
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
    else if (username.find(L'@') != std::wstring::npos)
    {
        if (domain.empty() || domain == L".")
        {
            domain.clear();
        }
    }
    else if (domain.empty() || domain == L".")
    {
        // Preserve Windows qualified identity (e.g. MicrosoftAccount\user@example.com or COMPUTER\User)
        // when the profile store uses the default local domain placeholder ".".
        const size_t qSlash = _qualifiedName.find(L'\\');
        if (qSlash != std::wstring::npos && qSlash > 0 && qSlash + 1 < _qualifiedName.size())
        {
            domain = _qualifiedName.substr(0, qSlash);
            username = _qualifiedName.substr(qSlash + 1);
        }
        else
        {
            domain = GetLocalComputerNetbiosName();
        }
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
        SetHeroAnimState(HeroAnimState::Failed);
        UpdateStatusText(kStatusFailed, true);
    }
    else
    {
        UpdateStatusText(kStatusScanning, true);
        SetHeroAnimState(HeroAnimState::Entering);
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

void WinIRFaceIDCredential::EnsureAnimThreadStarted()
{
    HANDLE threadToClose = nullptr;
    HANDLE hNewThread = nullptr;
    bool shouldRelease = false;

    EnterCriticalSection(&_cs);
    if (_selected && _pCredEvents3 != nullptr && _animStopEvent != nullptr && _animWakeEvent != nullptr)
    {
        if (_animThread != nullptr)
        {
            if (WaitForSingleObject(_animThread, 0) != WAIT_TIMEOUT)
            {
                threadToClose = _animThread;
                _animThread = nullptr;
            }
        }

        if (_animThread == nullptr)
        {
            ResetEvent(_animStopEvent);
            AddRef(); // Balanced in AnimThreadProc before exit
            hNewThread = CreateThread(nullptr, 0, AnimThreadProc, this, 0, nullptr);
            if (hNewThread != nullptr)
            {
                _animThread = hNewThread;
            }
            else
            {
                shouldRelease = true;
            }
        }
    }
    LeaveCriticalSection(&_cs);

    if (threadToClose != nullptr)
    {
        CloseHandle(threadToClose);
    }
    if (shouldRelease)
    {
        Release();
    }
}

void WinIRFaceIDCredential::StopAnimThreadAndWait()
{
    HANDLE hThread = nullptr;

    EnterCriticalSection(&_cs);
    if (_animStopEvent != nullptr)
    {
        SetEvent(_animStopEvent);
    }
    if (_animThread != nullptr)
    {
        hThread = _animThread;
        _animThread = nullptr;
    }
    LeaveCriticalSection(&_cs);

    if (hThread != nullptr)
    {
        WaitForSingleObject(hThread, 2500);
        CloseHandle(hThread);
    }
}

void WinIRFaceIDCredential::SetHeroAnimState(HeroAnimState state)
{
    bool startThread = false;

    EnterCriticalSection(&_cs);
    const bool retrigger =
        (state == HeroAnimState::Entering) ||
        (state == HeroAnimState::Unlocked) ||
        (state == HeroAnimState::Failed);
    if (_animState != state || retrigger)
    {
        _animState = state;
        ++_animGeneration;
    }
    startThread = _selected && (_pCredEvents3 != nullptr);
    HANDLE wakeEvent = _animWakeEvent;
    LeaveCriticalSection(&_cs);

    if (startThread)
    {
        EnsureAnimThreadStarted();
    }
    if (wakeEvent != nullptr)
    {
        SetEvent(wakeEvent);
    }
}

void WinIRFaceIDCredential::PushHeroFrame(UINT resId)
{
    const BYTE* pngData = nullptr;
    DWORD pngSize = 0;
    if (!LoadEmbeddedPngResource(resId, &pngData, &pngSize))
    {
        return;
    }

    ICredentialProviderCredentialEvents3* pEvents3 = nullptr;
    EnterCriticalSection(&_cs);
    _currentHeroResId = resId;
    if (_selected && _pCredEvents3 != nullptr)
    {
        pEvents3 = _pCredEvents3;
        pEvents3->AddRef();
    }
    LeaveCriticalSection(&_cs);

    if (pEvents3 != nullptr)
    {
        pEvents3->SetFieldBitmapBuffer(this, FID_HERO_IMAGE, pngSize, pngData);
        pEvents3->Release();
    }
}

DWORD WINAPI WinIRFaceIDCredential::AnimThreadProc(LPVOID param)
{
    auto* self = static_cast<WinIRFaceIDCredential*>(param);
    if (self != nullptr)
    {
        self->RunAnimation();
        self->Release();
    }
    return 0;
}

void WinIRFaceIDCredential::RunAnimation()
{
    struct AnimSequenceSpec
    {
        UINT baseResId = IDR_IR_FACEID_PNG;
        DWORD frameCount = 1;
        DWORD frameDelayMs = INFINITE;
        bool loop = false;
        bool advanceToScanning = false;
    };

    auto getSpec = [](HeroAnimState state) -> AnimSequenceSpec {
        switch (state)
        {
        case HeroAnimState::Entering:
            return { IDR_ANIM_ENTER_BASE, IDR_ANIM_ENTER_COUNT, kEnterFrameMs, false, true };
        case HeroAnimState::Scanning:
            return { IDR_ANIM_SCAN_BASE, IDR_ANIM_SCAN_COUNT, kScanFrameMs, true, false };
        case HeroAnimState::Unlocked:
            return { IDR_ANIM_UNLOCK_BASE, IDR_ANIM_UNLOCK_COUNT, kUnlockFrameMs, false, false };
        case HeroAnimState::Failed:
            return { IDR_ANIM_FAIL_BASE, IDR_ANIM_FAIL_COUNT, kFailFrameMs, false, false };
        case HeroAnimState::Idle:
        default:
            return { IDR_IR_FACEID_PNG, 1, INFINITE, false, false };
        }
    };

    uint64_t activeGen = 0;
    HeroAnimState activeState = HeroAnimState::Idle;
    DWORD frameIdx = 0;
    bool settled = false;
    bool hasInitialized = false;

    for (;;)
    {
        if (_animStopEvent == nullptr ||
            WaitForSingleObject(_animStopEvent, 0) == WAIT_OBJECT_0)
        {
            break;
        }

        EnterCriticalSection(&_cs);
        const HeroAnimState desiredState = _animState;
        const uint64_t desiredGen = _animGeneration;
        const bool canPush = _selected && (_pCredEvents3 != nullptr);
        LeaveCriticalSection(&_cs);

        if (!canPush)
        {
            HANDLE handles[2] = { _animStopEvent, _animWakeEvent };
            const DWORD waitRes = WaitForMultipleObjects(2, handles, FALSE, INFINITE);
            if (waitRes == WAIT_OBJECT_0 || waitRes == WAIT_FAILED)
            {
                break;
            }
            continue;
        }

        if (!hasInitialized || desiredGen != activeGen)
        {
            hasInitialized = true;
            activeGen = desiredGen;
            activeState = desiredState;
            frameIdx = 0;
            settled = false;
        }

        const AnimSequenceSpec spec = getSpec(activeState);

        ULONGLONG frameStart = 0;
        if (!settled)
        {
            frameStart = GetTickCount64();
            PushHeroFrame(spec.baseResId + frameIdx);
        }

        DWORD waitMs = INFINITE;
        if (!settled)
        {
            if (spec.loop || (frameIdx + 1 < spec.frameCount) || spec.advanceToScanning)
            {
                // Compensate for PNG decode/push overhead inside LogonUI.exe to maintain steady ~32 FPS pacing.
                const ULONGLONG elapsed = GetTickCount64() - frameStart;
                waitMs = (elapsed < spec.frameDelayMs)
                             ? static_cast<DWORD>(spec.frameDelayMs - elapsed)
                             : 1;
            }
            else
            {
                settled = true;
                waitMs = INFINITE;
            }
        }

        HANDLE handles[2] = { _animStopEvent, _animWakeEvent };
        const DWORD waitRes = WaitForMultipleObjects(2, handles, FALSE, waitMs);
        if (waitRes == WAIT_OBJECT_0 || waitRes == WAIT_FAILED)
        {
            break;
        }
        if (waitRes == WAIT_OBJECT_0 + 1)
        {
            continue;
        }

        if (spec.loop)
        {
            frameIdx = (frameIdx + 1) % spec.frameCount;
        }
        else if (frameIdx + 1 < spec.frameCount)
        {
            ++frameIdx;
        }
        else if (spec.advanceToScanning)
        {
            EnterCriticalSection(&_cs);
            if (_animGeneration == activeGen && _animState == HeroAnimState::Entering)
            {
                _animState = HeroAnimState::Scanning;
                ++_animGeneration;
            }
            LeaveCriticalSection(&_cs);
        }
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
            pEvents->SetFieldState(
                this,
                FID_RETRY_SUBMIT,
                scanning ? CPFS_HIDDEN : CPFS_DISPLAY_IN_SELECTED_TILE);
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
        SetHeroAnimState(HeroAnimState::Idle);
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
        if (provider != nullptr)
        {
            provider->AddRef();
        }
        LeaveCriticalSection(&_cs);

        result.Clear();
        UpdateStatusText(kStatusRecognized, false);
        SetHeroAnimState(HeroAnimState::Unlocked);

        if (_cancelEvent != nullptr &&
            WaitForSingleObject(_cancelEvent, kUnlockPreviewWaitMs) == WAIT_OBJECT_0)
        {
            EnterCriticalSection(&_cs);
            WipeVerifiedCredentialsLocked();
            LeaveCriticalSection(&_cs);
            if (provider != nullptr)
            {
                provider->Release();
            }
            return;
        }

        if (provider != nullptr)
        {
            provider->NotifyCredentialsChanged();
            provider->Release();
        }
        return;
    }

    const pipe_client::VerifyStatus verifyStatus = result.status;
    result.Clear();

    EnterCriticalSection(&_cs);
    _scanning = false;
    LeaveCriticalSection(&_cs);

    SetHeroAnimState(HeroAnimState::Failed);

    if (matched)
    {
        UpdateStatusText(kStatusNoPassword, true);
    }
    else if (verifyStatus == pipe_client::VerifyStatus::PipeUnavailable)
    {
        UpdateStatusText(kStatusServiceOffline, true);
    }
    else
    {
        UpdateStatusText(kStatusFailed, true);
    }
}
