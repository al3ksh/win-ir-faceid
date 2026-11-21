#pragma once

#include <windows.h>
#include <guiddef.h>
#include <cstdint>
#include <string>
#include <vector>

// {49C8AEC3-2A43-4BED-987D-DE31E44DF41D}
// Unique CLSID for the WinIRFaceID Credential Provider V2 COM server.
// Keep synchronized with register.reg and unregister.reg.
inline constexpr GUID CLSID_WinIRFaceIDProvider = {
    0x49c8aec3,
    0x2a43,
    0x4bed,
    { 0x98, 0x7d, 0xde, 0x31, 0xe4, 0x4d, 0xf4, 0x1d }
};

inline constexpr wchar_t kPipeName[] = L"\\\\.\\pipe\\WinIRFaceID";
inline constexpr DWORD kMaxPipeMessageBytes = 65536; // 64 KB per IPC_PROTOCOL.md
inline constexpr DWORD kDefaultVerifyTimeoutMs = 8000;

// COM DLL reference counting (implemented in dllmain.cpp)
void DllAddRef();
void DllRelease();

// Securely wipes mutable string buffers containing sensitive data.
inline void SecureWipeString(std::wstring& s)
{
    if (!s.empty())
    {
        SecureZeroMemory(s.data(), s.size() * sizeof(wchar_t));
    }
    s.clear();
}

inline void SecureWipeString(std::string& s)
{
    if (!s.empty())
    {
        SecureZeroMemory(s.data(), s.size() * sizeof(char));
    }
    s.clear();
}

// UTF-8 <-> UTF-16 conversion and lightweight JSON helpers (implemented in pipe_client.cpp)
std::string WideToUtf8(const std::wstring& wide);
std::wstring Utf8ToWide(const std::string& utf8);
std::string JsonEscape(const std::string& raw);

bool TryGetJsonString(const std::string& json, const char* key, std::string& outValue);
bool TryGetJsonDouble(const std::string& json, const char* key, double& outValue);
bool TryGetJsonStringArray(const std::string& json, const char* key, std::vector<std::string>& outValues);
