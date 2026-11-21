#include <initguid.h>
#include "common.h"
#include "provider.h"

#include <credentialprovider.h>
#include <propkey.h>
#include <shlguid.h>
#include <shlwapi.h>
#include <new>

namespace
{

LONG g_dllRefCount = 0;

class WinIRFaceIDClassFactory final : public IClassFactory
{
public:
    WinIRFaceIDClassFactory()
    {
        DllAddRef();
    }

    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) override
    {
        static const QITAB qit[] = {
            QITABENT(WinIRFaceIDClassFactory, IClassFactory),
            { 0 }
        };
        return QISearch(this, qit, riid, ppv);
    }

    IFACEMETHODIMP_(ULONG) AddRef() override
    {
        return static_cast<ULONG>(InterlockedIncrement(&_refCount));
    }

    IFACEMETHODIMP_(ULONG) Release() override
    {
        const LONG count = InterlockedDecrement(&_refCount);
        if (count == 0)
        {
            delete this;
        }
        return static_cast<ULONG>(count);
    }

    IFACEMETHODIMP CreateInstance(
        IUnknown* pUnkOuter,
        REFIID riid,
        void** ppv) override
    {
        if (ppv == nullptr)
        {
            return E_POINTER;
        }
        *ppv = nullptr;

        if (pUnkOuter != nullptr)
        {
            return CLASS_E_NOAGGREGATION;
        }

        auto* provider = new (std::nothrow) WinIRFaceIDProvider();
        if (provider == nullptr)
        {
            return E_OUTOFMEMORY;
        }

        const HRESULT hr = provider->QueryInterface(riid, ppv);
        provider->Release();
        return hr;
    }

    IFACEMETHODIMP LockServer(BOOL fLock) override
    {
        if (fLock)
        {
            DllAddRef();
        }
        else
        {
            DllRelease();
        }
        return S_OK;
    }

private:
    ~WinIRFaceIDClassFactory()
    {
        DllRelease();
    }

    LONG _refCount = 1;
};

} // namespace

void DllAddRef()
{
    InterlockedIncrement(&g_dllRefCount);
}

void DllRelease()
{
    InterlockedDecrement(&g_dllRefCount);
}

BOOL APIENTRY DllMain(HMODULE hModule, DWORD ulReasonForCall, LPVOID /*lpReserved*/)
{
    if (ulReasonForCall == DLL_PROCESS_ATTACH)
    {
        DisableThreadLibraryCalls(hModule);
    }
    return TRUE;
}

STDAPI DllCanUnloadNow()
{
    return (g_dllRefCount == 0) ? S_OK : S_FALSE;
}

STDAPI DllGetClassObject(REFCLSID rclsid, REFIID riid, void** ppv)
{
    if (ppv == nullptr)
    {
        return E_POINTER;
    }
    *ppv = nullptr;

    if (!IsEqualCLSID(rclsid, CLSID_WinIRFaceIDProvider))
    {
        return CLASS_E_CLASSNOTAVAILABLE;
    }

    auto* factory = new (std::nothrow) WinIRFaceIDClassFactory();
    if (factory == nullptr)
    {
        return E_OUTOFMEMORY;
    }

    const HRESULT hr = factory->QueryInterface(riid, ppv);
    factory->Release();
    return hr;
}
