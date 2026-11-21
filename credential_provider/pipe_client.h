#pragma once

#include "common.h"
#include <windows.h>
#include <string>
#include <vector>

namespace pipe_client
{

enum class VerifyStatus
{
    Matched,
    Failed,
    Cancelled,
    PipeUnavailable,
    Error
};

struct PingResult
{
    bool ok = false;
    std::wstring version;
    std::vector<std::wstring> enrolledUsers;
    std::wstring error;
};

struct VerifyResult
{
    VerifyStatus status = VerifyStatus::Error;
    std::wstring username;
    std::wstring domain;
    std::wstring password;
    double score = 0.0;
    double livenessDelta = 0.0;
    std::wstring reason;

    VerifyResult() = default;
    ~VerifyResult()
    {
        Clear();
    }

    VerifyResult(const VerifyResult&) = delete;
    VerifyResult& operator=(const VerifyResult&) = delete;

    VerifyResult(VerifyResult&& other) noexcept
        : status(other.status)
        , username(std::move(other.username))
        , domain(std::move(other.domain))
        , password(std::move(other.password))
        , score(other.score)
        , livenessDelta(other.livenessDelta)
        , reason(std::move(other.reason))
    {
        other.Clear();
    }

    VerifyResult& operator=(VerifyResult&& other) noexcept
    {
        if (this != &other)
        {
            Clear();
            status = other.status;
            username = std::move(other.username);
            domain = std::move(other.domain);
            password = std::move(other.password);
            score = other.score;
            livenessDelta = other.livenessDelta;
            reason = std::move(other.reason);
            other.Clear();
        }
        return *this;
    }

    void Clear() noexcept
    {
        SecureWipeString(password);
        SecureWipeString(username);
        SecureWipeString(domain);
        reason.clear();
        score = 0.0;
        livenessDelta = 0.0;
        status = VerifyStatus::Error;
    }
};

// Queries the WinIRFaceID Named Pipe server with {"cmd":"ping"}.
PingResult Ping(DWORD timeoutMs = 2000, HANDLE cancelEvent = nullptr);

// Requests face verification via {"cmd":"verify","username":...,"timeout_ms":...}.
// Returns true if a face matched, and returns immediately when cancelEvent is signaled
// without hanging LogonUI.exe.
bool VerifyFace(
    const std::wstring& username,
    DWORD timeoutMs,
    HANDLE cancelEvent,
    VerifyResult& outResult);

// Sends a best-effort {"cmd":"cancel"} request to turn off the IR LED immediately.
bool SendCancel(DWORD timeoutMs = 500);

} // namespace pipe_client
