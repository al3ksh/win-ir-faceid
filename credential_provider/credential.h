#pragma once

#include "pipe_client.h"

#include <windows.h>
#include <credentialprovider.h>
#include <string>

class WinIRFaceIDProvider;

enum WinIRFieldId : DWORD
{
    FID_TILE_IMAGE = 0,
    FID_LABEL = 1,
    FID_STATUS = 2,
    FID_RETRY_SUBMIT = 3,
    FID_NUM_FIELDS = 4
};

class WinIRFaceIDCredential final : public ICredentialProviderCredential2
{
public:
    WinIRFaceIDCredential(
        WinIRFaceIDProvider* pProvider,
        CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus,
        std::wstring userSid,
        std::wstring userName);

    // IUnknown
    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) override;
    IFACEMETHODIMP_(ULONG) AddRef() override;
    IFACEMETHODIMP_(ULONG) Release() override;

    // ICredentialProviderCredential
    IFACEMETHODIMP Advise(ICredentialProviderCredentialEvents* pcpce) override;
    IFACEMETHODIMP UnAdvise() override;
    IFACEMETHODIMP SetSelected(BOOL* pbAutoLogon) override;
    IFACEMETHODIMP SetDeselected() override;
    IFACEMETHODIMP GetFieldState(
        DWORD dwFieldID,
        CREDENTIAL_PROVIDER_FIELD_STATE* pcpfs,
        CREDENTIAL_PROVIDER_FIELD_INTERACTIVE_STATE* pcpfis) override;
    IFACEMETHODIMP GetStringValue(DWORD dwFieldID, LPWSTR* ppsz) override;
    IFACEMETHODIMP GetBitmapValue(DWORD dwFieldID, HBITMAP* phbmp) override;
    IFACEMETHODIMP GetCheckboxValue(
        DWORD dwFieldID,
        BOOL* pbChecked,
        LPWSTR* ppszLabel) override;
    IFACEMETHODIMP GetSubmitButtonValue(DWORD dwFieldID, DWORD* pdwAdjacentTo) override;
    IFACEMETHODIMP GetComboBoxValueCount(
        DWORD dwFieldID,
        DWORD* pcItems,
        DWORD* pdwSelectedItem) override;
    IFACEMETHODIMP GetComboBoxValueAt(
        DWORD dwFieldID,
        DWORD dwItem,
        LPWSTR* ppszItem) override;
    IFACEMETHODIMP SetStringValue(DWORD dwFieldID, LPCWSTR psz) override;
    IFACEMETHODIMP SetCheckboxValue(DWORD dwFieldID, BOOL bChecked) override;
    IFACEMETHODIMP SetComboBoxSelectedValue(DWORD dwFieldID, DWORD dwSelectedItem) override;
    IFACEMETHODIMP CommandLinkClicked(DWORD dwFieldID) override;
    IFACEMETHODIMP GetSerialization(
        CREDENTIAL_PROVIDER_GET_SERIALIZATION_RESPONSE* pcpgsr,
        CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION* pcpcs,
        LPWSTR* ppszOptionalStatusText,
        CREDENTIAL_PROVIDER_STATUS_ICON* pcpsiOptionalStatusIcon) override;
    IFACEMETHODIMP ReportResult(
        NTSTATUS ntsStatus,
        NTSTATUS ntsSubstatus,
        LPWSTR* ppszOptionalStatusText,
        CREDENTIAL_PROVIDER_STATUS_ICON* pcpsiOptionalStatusIcon) override;

    // ICredentialProviderCredential2
    IFACEMETHODIMP GetUserSid(LPWSTR* sid) override;

    // Helpers used by WinIRFaceIDProvider
    bool HasVerifiedCredential();
    void DetachProvider();

private:
    ~WinIRFaceIDCredential();

    void StartScan();
    void CancelScanAndWait();
    void UpdateStatusText(const std::wstring& newText, bool updateButtonState);
    void WipeVerifiedCredentialsLocked();

    static DWORD WINAPI ScanThreadProc(LPVOID param);
    void RunScan();

    LONG _refCount = 1;
    CRITICAL_SECTION _cs{};

    WinIRFaceIDProvider* _pProvider = nullptr;
    CREDENTIAL_PROVIDER_USAGE_SCENARIO _cpus = CPUS_INVALID;
    std::wstring _userSid;
    std::wstring _userName;

    ICredentialProviderCredentialEvents* _pCredEvents = nullptr;
    HANDLE _cancelEvent = nullptr;
    HANDLE _hScanThread = nullptr;

    bool _selected = false;
    bool _scanning = false;
    bool _hasVerifiedCred = false;
    std::wstring _statusText = L"Scanning infrared face...";

    std::wstring _verifiedUsername;
    std::wstring _verifiedDomain;
    std::wstring _verifiedPassword;
};
