#include "pipe_client.h"

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <sstream>

namespace
{

constexpr DWORD kPipeConnectWaitSliceMs = 100;

bool IsCancelSignaled(HANDLE cancelEvent)
{
    return cancelEvent != nullptr && WaitForSingleObject(cancelEvent, 0) == WAIT_OBJECT_0;
}

DWORD RemainingTimeoutMs(ULONGLONG deadlineTick)
{
    const ULONGLONG now = GetTickCount64();
    if (now >= deadlineTick)
    {
        return 0;
    }
    const ULONGLONG diff = deadlineTick - now;
    return diff > MAXDWORD ? MAXDWORD : static_cast<DWORD>(diff);
}

enum class IoWaitOutcome
{
    Completed,
    Cancelled,
    TimedOut,
    Failed
};

IoWaitOutcome WaitForOverlappedIo(
    HANDLE hPipe,
    OVERLAPPED& ov,
    HANDLE cancelEvent,
    DWORD waitTimeoutMs,
    DWORD& outBytesTransferred,
    DWORD& outWin32Error)
{
    outBytesTransferred = 0;
    outWin32Error = ERROR_SUCCESS;

    HANDLE handles[2] = { ov.hEvent, cancelEvent };
    const DWORD handleCount = (cancelEvent != nullptr) ? 2u : 1u;

    const DWORD waitRes = WaitForMultipleObjects(handleCount, handles, FALSE, waitTimeoutMs);
    if (waitRes == WAIT_OBJECT_0)
    {
        if (!GetOverlappedResult(hPipe, &ov, &outBytesTransferred, FALSE))
        {
            outWin32Error = GetLastError();
            return IoWaitOutcome::Failed;
        }
        return IoWaitOutcome::Completed;
    }

    // Cancel pending I/O and wait for the kernel to release OVERLAPPED safely.
    CancelIoEx(hPipe, &ov);
    DWORD dummyBytes = 0;
    GetOverlappedResult(hPipe, &ov, &dummyBytes, TRUE);

    if (waitRes == WAIT_OBJECT_0 + 1)
    {
        outWin32Error = ERROR_OPERATION_ABORTED;
        return IoWaitOutcome::Cancelled;
    }
    if (waitRes == WAIT_TIMEOUT)
    {
        outWin32Error = WAIT_TIMEOUT;
        return IoWaitOutcome::TimedOut;
    }

    outWin32Error = GetLastError();
    return IoWaitOutcome::Failed;
}

// Passwords are only ever accepted from a pipe server running as LocalSystem. A
// non-privileged process that races the service for the pipe name (e.g. while the
// service restarts) therefore cannot feed LogonUI forged credentials or harvest requests.
bool IsPipeServerLocalSystem(HANDLE hPipe)
{
    ULONG serverPid = 0;
    if (!GetNamedPipeServerProcessId(hPipe, &serverPid) || serverPid == 0)
    {
        return false;
    }

    HANDLE hProcess = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, serverPid);
    if (hProcess == nullptr)
    {
        return false;
    }

    bool isSystem = false;
    HANDLE hToken = nullptr;
    if (OpenProcessToken(hProcess, TOKEN_QUERY, &hToken))
    {
        DWORD needed = 0;
        GetTokenInformation(hToken, TokenUser, nullptr, 0, &needed);
        if (needed > 0)
        {
            std::vector<BYTE> buffer(needed);
            if (GetTokenInformation(hToken, TokenUser, buffer.data(), needed, &needed))
            {
                const auto* tokenUser = reinterpret_cast<const TOKEN_USER*>(buffer.data());
                isSystem = IsWellKnownSid(tokenUser->User.Sid, WinLocalSystemSid) != FALSE;
            }
        }
        CloseHandle(hToken);
    }
    CloseHandle(hProcess);
    return isSystem;
}

HANDLE OpenPipeWithCancel(ULONGLONG deadlineTick, HANDLE cancelEvent, DWORD& outError)
{
    outError = ERROR_SUCCESS;

    for (;;)
    {
        if (IsCancelSignaled(cancelEvent))
        {
            outError = ERROR_OPERATION_ABORTED;
            return INVALID_HANDLE_VALUE;
        }

        // Least-privilege access (no FILE_CREATE_PIPE_INSTANCE) and an identification-only
        // impersonation level: the server may learn who we are but never act as us.
        HANDLE hPipe = CreateFileW(
            kPipeName,
            GENERIC_READ | FILE_WRITE_DATA,
            0,
            nullptr,
            OPEN_EXISTING,
            FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION,
            nullptr);

        if (hPipe != INVALID_HANDLE_VALUE)
        {
            // Pipe is created in byte read mode by the server; no handle state change needed.
            if (!IsPipeServerLocalSystem(hPipe))
            {
                CloseHandle(hPipe);
                outError = ERROR_ACCESS_DENIED;
                return INVALID_HANDLE_VALUE;
            }
            return hPipe;
        }

        const DWORD err = GetLastError();
        if (err != ERROR_PIPE_BUSY)
        {
            outError = err;
            return INVALID_HANDLE_VALUE;
        }

        const DWORD remaining = RemainingTimeoutMs(deadlineTick);
        if (remaining == 0)
        {
            outError = ERROR_SEM_TIMEOUT;
            return INVALID_HANDLE_VALUE;
        }

        const DWORD slice = std::min(remaining, kPipeConnectWaitSliceMs);
        if (!WaitNamedPipeW(kPipeName, slice))
        {
            const DWORD waitErr = GetLastError();
            if (waitErr != ERROR_SEM_TIMEOUT && waitErr != ERROR_FILE_NOT_FOUND)
            {
                // Keep retrying until deadline unless pipe vanished permanently.
                Sleep(std::min<DWORD>(20, slice));
            }
        }
    }
}

IoWaitOutcome WriteAllOverlapped(
    HANDLE hPipe,
    const std::string& payload,
    HANDLE cancelEvent,
    ULONGLONG deadlineTick,
    DWORD& outWin32Error)
{
    outWin32Error = ERROR_SUCCESS;

    HANDLE ioEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (ioEvent == nullptr)
    {
        outWin32Error = GetLastError();
        return IoWaitOutcome::Failed;
    }

    size_t totalWritten = 0;
    IoWaitOutcome outcome = IoWaitOutcome::Completed;

    while (totalWritten < payload.size())
    {
        if (IsCancelSignaled(cancelEvent))
        {
            outWin32Error = ERROR_OPERATION_ABORTED;
            outcome = IoWaitOutcome::Cancelled;
            break;
        }

        const DWORD remaining = RemainingTimeoutMs(deadlineTick);
        if (remaining == 0)
        {
            outWin32Error = WAIT_TIMEOUT;
            outcome = IoWaitOutcome::TimedOut;
            break;
        }

        ResetEvent(ioEvent);
        OVERLAPPED ov{};
        ov.hEvent = ioEvent;

        const DWORD chunkLen = static_cast<DWORD>(payload.size() - totalWritten);
        DWORD written = 0;
        const BOOL ok = WriteFile(
            hPipe,
            payload.data() + totalWritten,
            chunkLen,
            &written,
            &ov);

        if (ok)
        {
            totalWritten += written;
            continue;
        }

        const DWORD err = GetLastError();
        if (err != ERROR_IO_PENDING)
        {
            outWin32Error = err;
            outcome = IoWaitOutcome::Failed;
            break;
        }

        DWORD asyncWritten = 0;
        outcome = WaitForOverlappedIo(hPipe, ov, cancelEvent, remaining, asyncWritten, outWin32Error);
        if (outcome != IoWaitOutcome::Completed)
        {
            break;
        }
        totalWritten += asyncWritten;
    }

    CloseHandle(ioEvent);
    return outcome;
}

IoWaitOutcome ReadLineOverlapped(
    HANDLE hPipe,
    HANDLE cancelEvent,
    ULONGLONG deadlineTick,
    std::string& outLine,
    DWORD& outWin32Error)
{
    outLine.clear();
    outWin32Error = ERROR_SUCCESS;

    HANDLE ioEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (ioEvent == nullptr)
    {
        outWin32Error = GetLastError();
        return IoWaitOutcome::Failed;
    }

    std::string buffer;
    buffer.reserve(512);

    char chunk[1024]{};
    IoWaitOutcome outcome = IoWaitOutcome::Failed;

    for (;;)
    {
        if (IsCancelSignaled(cancelEvent))
        {
            outWin32Error = ERROR_OPERATION_ABORTED;
            outcome = IoWaitOutcome::Cancelled;
            break;
        }

        const DWORD remaining = RemainingTimeoutMs(deadlineTick);
        if (remaining == 0)
        {
            outWin32Error = WAIT_TIMEOUT;
            outcome = IoWaitOutcome::TimedOut;
            break;
        }

        ResetEvent(ioEvent);
        OVERLAPPED ov{};
        ov.hEvent = ioEvent;

        DWORD bytesRead = 0;
        const BOOL ok = ReadFile(hPipe, chunk, sizeof(chunk), &bytesRead, &ov);
        if (!ok)
        {
            const DWORD err = GetLastError();
            if (err == ERROR_IO_PENDING)
            {
                outcome = WaitForOverlappedIo(hPipe, ov, cancelEvent, remaining, bytesRead, outWin32Error);
                if (outcome != IoWaitOutcome::Completed)
                {
                    break;
                }
            }
            else if (err == ERROR_MORE_DATA)
            {
                // Partial message in byte mode; continue reading.
                GetOverlappedResult(hPipe, &ov, &bytesRead, FALSE);
            }
            else if (err == ERROR_BROKEN_PIPE && !buffer.empty())
            {
                outcome = IoWaitOutcome::Completed;
                break;
            }
            else
            {
                outWin32Error = err;
                outcome = IoWaitOutcome::Failed;
                break;
            }
        }

        if (bytesRead == 0)
        {
            if (!buffer.empty())
            {
                outcome = IoWaitOutcome::Completed;
            }
            else
            {
                outWin32Error = ERROR_BROKEN_PIPE;
                outcome = IoWaitOutcome::Failed;
            }
            break;
        }

        if (buffer.size() + bytesRead > kMaxPipeMessageBytes)
        {
            outWin32Error = ERROR_INSUFFICIENT_BUFFER;
            outcome = IoWaitOutcome::Failed;
            break;
        }

        buffer.append(chunk, bytesRead);
        const size_t newlinePos = buffer.find('\n');
        if (newlinePos != std::string::npos)
        {
            outLine.assign(buffer.data(), newlinePos);
            if (!outLine.empty() && outLine.back() == '\r')
            {
                outLine.pop_back();
            }
            outcome = IoWaitOutcome::Completed;
            break;
        }
    }

    SecureZeroMemory(chunk, sizeof(chunk));
    SecureWipeString(buffer);
    CloseHandle(ioEvent);
    return outcome;
}

// Lightweight strict top-level JSON helpers for IPC responses.

size_t SkipWhitespace(const std::string& s, size_t pos)
{
    while (pos < s.size() && std::isspace(static_cast<unsigned char>(s[pos])))
    {
        ++pos;
    }
    return pos;
}

void AppendUtf8Codepoint(uint32_t cp, std::string& out)
{
    if (cp <= 0x7F)
    {
        out.push_back(static_cast<char>(cp));
    }
    else if (cp <= 0x7FF)
    {
        out.push_back(static_cast<char>(0xC0 | ((cp >> 6) & 0x1F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    }
    else if (cp <= 0xFFFF)
    {
        out.push_back(static_cast<char>(0xE0 | ((cp >> 12) & 0x0F)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    }
    else if (cp <= 0x10FFFF)
    {
        out.push_back(static_cast<char>(0xF0 | ((cp >> 18) & 0x07)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    }
}

bool ParseHex4(const std::string& s, size_t pos, uint32_t& out)
{
    if (pos + 4 > s.size())
    {
        return false;
    }
    uint32_t val = 0;
    for (size_t i = 0; i < 4; ++i)
    {
        const char ch = s[pos + i];
        val <<= 4;
        if (ch >= '0' && ch <= '9')
            val |= static_cast<uint32_t>(ch - '0');
        else if (ch >= 'a' && ch <= 'f')
            val |= static_cast<uint32_t>(ch - 'a' + 10);
        else if (ch >= 'A' && ch <= 'F')
            val |= static_cast<uint32_t>(ch - 'A' + 10);
        else
            return false;
    }
    out = val;
    return true;
}

bool ParseJsonStringAt(const std::string& s, size_t& pos, std::string& out)
{
    out.clear();
    if (pos >= s.size() || s[pos] != '"')
    {
        return false;
    }
    ++pos;
    while (pos < s.size())
    {
        const char ch = s[pos++];
        if (ch == '"')
        {
            return true;
        }
        if (ch == '\\')
        {
            if (pos >= s.size())
            {
                return false;
            }
            const char esc = s[pos++];
            switch (esc)
            {
            case '"': out.push_back('"'); break;
            case '\\': out.push_back('\\'); break;
            case '/': out.push_back('/'); break;
            case 'b': out.push_back('\b'); break;
            case 'f': out.push_back('\f'); break;
            case 'n': out.push_back('\n'); break;
            case 'r': out.push_back('\r'); break;
            case 't': out.push_back('\t'); break;
            case 'u':
            {
                uint32_t cp = 0;
                if (!ParseHex4(s, pos, cp))
                {
                    return false;
                }
                pos += 4;
                if (cp >= 0xD800 && cp <= 0xDBFF && pos + 6 <= s.size() && s[pos] == '\\' && s[pos + 1] == 'u')
                {
                    uint32_t low = 0;
                    if (ParseHex4(s, pos + 2, low) && low >= 0xDC00 && low <= 0xDFFF)
                    {
                        pos += 6;
                        cp = 0x10000 + (((cp - 0xD800) << 10) | (low - 0xDC00));
                    }
                }
                AppendUtf8Codepoint(cp, out);
                break;
            }
            default:
                return false;
            }
        }
        else
        {
            out.push_back(ch);
        }
    }
    return false;
}

bool SkipJsonValue(const std::string& s, size_t& pos)
{
    pos = SkipWhitespace(s, pos);
    if (pos >= s.size())
    {
        return false;
    }

    const char ch = s[pos];
    if (ch == '"')
    {
        std::string scratch;
        const bool ok = ParseJsonStringAt(s, pos, scratch);
        SecureWipeString(scratch);
        return ok;
    }
    if (ch == '{' || ch == '[')
    {
        const char openCh = ch;
        const char closeCh = (openCh == '{') ? '}' : ']';
        int depth = 1;
        ++pos;
        while (pos < s.size() && depth > 0)
        {
            if (s[pos] == '"')
            {
                std::string scratch;
                if (!ParseJsonStringAt(s, pos, scratch))
                {
                    SecureWipeString(scratch);
                    return false;
                }
                SecureWipeString(scratch);
                continue;
            }
            if (s[pos] == openCh) ++depth;
            else if (s[pos] == closeCh) --depth;
            ++pos;
        }
        return depth == 0;
    }

    while (pos < s.size())
    {
        const char c = s[pos];
        if (c == ',' || c == '}' || c == ']' || std::isspace(static_cast<unsigned char>(c)))
        {
            break;
        }
        ++pos;
    }
    return true;
}

bool FindTopLevelValueStart(const std::string& json, const char* targetKey, size_t& outValuePos)
{
    size_t pos = SkipWhitespace(json, 0);
    if (pos >= json.size() || json[pos] != '{')
    {
        return false;
    }
    ++pos;

    while (pos < json.size())
    {
        pos = SkipWhitespace(json, pos);
        if (pos >= json.size() || json[pos] == '}')
        {
            return false;
        }
        if (json[pos] == ',')
        {
            ++pos;
            continue;
        }

        std::string key;
        if (!ParseJsonStringAt(json, pos, key))
        {
            return false;
        }

        pos = SkipWhitespace(json, pos);
        if (pos >= json.size() || json[pos] != ':')
        {
            return false;
        }
        pos = SkipWhitespace(json, pos + 1);

        if (key == targetKey)
        {
            outValuePos = pos;
            return true;
        }

        if (!SkipJsonValue(json, pos))
        {
            return false;
        }
    }

    return false;
}

std::wstring FormatWin32Error(DWORD err)
{
    std::wostringstream oss;
    oss << L"win32_error_" << err;
    return oss.str();
}

} // namespace

std::string WideToUtf8(const std::wstring& wide)
{
    if (wide.empty())
    {
        return {};
    }
    const int needed = WideCharToMultiByte(
        CP_UTF8, 0,
        wide.data(), static_cast<int>(wide.size()),
        nullptr, 0, nullptr, nullptr);
    if (needed <= 0)
    {
        return {};
    }
    std::string out(static_cast<size_t>(needed), '\0');
    WideCharToMultiByte(
        CP_UTF8, 0,
        wide.data(), static_cast<int>(wide.size()),
        out.data(), needed, nullptr, nullptr);
    return out;
}

std::wstring Utf8ToWide(const std::string& utf8)
{
    if (utf8.empty())
    {
        return {};
    }
    const int needed = MultiByteToWideChar(
        CP_UTF8, 0,
        utf8.data(), static_cast<int>(utf8.size()),
        nullptr, 0);
    if (needed <= 0)
    {
        return {};
    }
    std::wstring out(static_cast<size_t>(needed), L'\0');
    MultiByteToWideChar(
        CP_UTF8, 0,
        utf8.data(), static_cast<int>(utf8.size()),
        out.data(), needed);
    return out;
}

std::string JsonEscape(const std::string& raw)
{
    std::string out;
    out.reserve(raw.size() + 8);
    for (unsigned char c : raw)
    {
        switch (c)
        {
        case '"': out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\b': out += "\\b"; break;
        case '\f': out += "\\f"; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        default:
            if (c < 0x20)
            {
                char buf[8]{};
                snprintf(buf, sizeof(buf), "\\u%04x", static_cast<unsigned int>(c));
                out += buf;
            }
            else
            {
                out.push_back(static_cast<char>(c));
            }
            break;
        }
    }
    return out;
}

bool TryGetJsonString(const std::string& json, const char* key, std::string& outValue)
{
    outValue.clear();
    size_t pos = 0;
    if (!FindTopLevelValueStart(json, key, pos))
    {
        return false;
    }
    return ParseJsonStringAt(json, pos, outValue);
}

bool TryGetJsonDouble(const std::string& json, const char* key, double& outValue)
{
    outValue = 0.0;
    size_t pos = 0;
    if (!FindTopLevelValueStart(json, key, pos))
    {
        return false;
    }
    if (pos >= json.size())
    {
        return false;
    }
    const char* start = json.c_str() + pos;
    char* endPtr = nullptr;
    const double parsed = std::strtod(start, &endPtr);
    if (endPtr == start)
    {
        return false;
    }
    outValue = parsed;
    return true;
}

bool TryGetJsonStringArray(const std::string& json, const char* key, std::vector<std::string>& outValues)
{
    outValues.clear();
    size_t pos = 0;
    if (!FindTopLevelValueStart(json, key, pos))
    {
        return false;
    }
    if (pos >= json.size() || json[pos] != '[')
    {
        return false;
    }
    ++pos;

    while (pos < json.size())
    {
        pos = SkipWhitespace(json, pos);
        if (pos >= json.size())
        {
            return false;
        }
        if (json[pos] == ']')
        {
            return true;
        }
        if (json[pos] == ',')
        {
            ++pos;
            continue;
        }
        std::string item;
        if (!ParseJsonStringAt(json, pos, item))
        {
            return false;
        }
        outValues.push_back(std::move(item));
    }
    return false;
}

namespace pipe_client
{

PingResult Ping(DWORD timeoutMs, HANDLE cancelEvent)
{
    PingResult result;
    const ULONGLONG deadline = GetTickCount64() + static_cast<ULONGLONG>(std::max<DWORD>(timeoutMs, 250));

    DWORD winErr = ERROR_SUCCESS;
    HANDLE hPipe = OpenPipeWithCancel(deadline, cancelEvent, winErr);
    if (hPipe == INVALID_HANDLE_VALUE)
    {
        result.error = (winErr == ERROR_OPERATION_ABORTED) ? L"cancelled" : FormatWin32Error(winErr);
        return result;
    }

    const std::string request = "{\"cmd\":\"ping\"}\n";
    IoWaitOutcome writeOutcome = WriteAllOverlapped(hPipe, request, cancelEvent, deadline, winErr);
    if (writeOutcome != IoWaitOutcome::Completed)
    {
        CloseHandle(hPipe);
        result.error = (writeOutcome == IoWaitOutcome::Cancelled) ? L"cancelled"
                     : (writeOutcome == IoWaitOutcome::TimedOut)  ? L"timeout"
                                                                  : FormatWin32Error(winErr);
        return result;
    }

    std::string responseLine;
    IoWaitOutcome readOutcome = ReadLineOverlapped(hPipe, cancelEvent, deadline, responseLine, winErr);
    CloseHandle(hPipe);

    if (readOutcome != IoWaitOutcome::Completed)
    {
        result.error = (readOutcome == IoWaitOutcome::Cancelled) ? L"cancelled"
                     : (readOutcome == IoWaitOutcome::TimedOut)  ? L"timeout"
                                                                 : FormatWin32Error(winErr);
        SecureWipeString(responseLine);
        return result;
    }

    std::string status;
    if (!TryGetJsonString(responseLine, "status", status) || status != "ok")
    {
        std::string reason;
        if (TryGetJsonString(responseLine, "reason", reason))
        {
            result.error = Utf8ToWide(reason);
        }
        else
        {
            result.error = L"invalid_response";
        }
        SecureWipeString(responseLine);
        return result;
    }

    std::string version;
    if (TryGetJsonString(responseLine, "version", version))
    {
        result.version = Utf8ToWide(version);
    }

    std::vector<std::string> users;
    if (TryGetJsonStringArray(responseLine, "enrolled_users", users))
    {
        for (const std::string& u : users)
        {
            result.enrolledUsers.push_back(Utf8ToWide(u));
        }
    }

    SecureWipeString(responseLine);
    result.ok = true;
    return result;
}

bool SendCancel(DWORD timeoutMs)
{
    const ULONGLONG deadline = GetTickCount64() + static_cast<ULONGLONG>(std::max<DWORD>(timeoutMs, 100));
    DWORD winErr = ERROR_SUCCESS;
    HANDLE hPipe = OpenPipeWithCancel(deadline, nullptr, winErr);
    if (hPipe == INVALID_HANDLE_VALUE)
    {
        return false;
    }

    const std::string request = "{\"cmd\":\"cancel\"}\n";
    if (WriteAllOverlapped(hPipe, request, nullptr, deadline, winErr) != IoWaitOutcome::Completed)
    {
        CloseHandle(hPipe);
        return false;
    }

    std::string responseLine;
    const IoWaitOutcome readOutcome = ReadLineOverlapped(hPipe, nullptr, deadline, responseLine, winErr);
    CloseHandle(hPipe);

    std::string status;
    const bool ok = (readOutcome == IoWaitOutcome::Completed)
        && TryGetJsonString(responseLine, "status", status)
        && (status == "ok");
    SecureWipeString(responseLine);
    return ok;
}

bool VerifyFace(
    const std::wstring& username,
    DWORD timeoutMs,
    HANDLE cancelEvent,
    VerifyResult& outResult)
{
    outResult.Clear();
    const DWORD effectiveTimeoutMs = (timeoutMs == 0) ? kDefaultVerifyTimeoutMs : timeoutMs;
    // Allow slightly longer IPC wait than the engine's internal verify timeout so the
    // server can cleanly return its {"status":"failed","reason":"timeout"} JSON frame.
    const ULONGLONG ipcDeadline = GetTickCount64() + static_cast<ULONGLONG>(effectiveTimeoutMs) + 3000ULL;

    DWORD winErr = ERROR_SUCCESS;
    HANDLE hPipe = OpenPipeWithCancel(ipcDeadline, cancelEvent, winErr);
    if (hPipe == INVALID_HANDLE_VALUE)
    {
        if (winErr == ERROR_OPERATION_ABORTED)
        {
            outResult.status = VerifyStatus::Cancelled;
            outResult.reason = L"cancelled";
        }
        else if (winErr == ERROR_FILE_NOT_FOUND || winErr == ERROR_PIPE_BUSY || winErr == ERROR_SEM_TIMEOUT)
        {
            outResult.status = VerifyStatus::PipeUnavailable;
            outResult.reason = L"pipe_unavailable";
        }
        else
        {
            outResult.status = VerifyStatus::Error;
            outResult.reason = FormatWin32Error(winErr);
        }
        return false;
    }

    std::ostringstream reqStream;
    reqStream << "{\"cmd\":\"verify\",\"username\":\""
              << JsonEscape(WideToUtf8(username))
              << "\",\"timeout_ms\":" << effectiveTimeoutMs << "}\n";
    std::string request = reqStream.str();

    const IoWaitOutcome writeOutcome = WriteAllOverlapped(hPipe, request, cancelEvent, ipcDeadline, winErr);
    SecureWipeString(request);
    if (writeOutcome != IoWaitOutcome::Completed)
    {
        CloseHandle(hPipe);
        if (writeOutcome == IoWaitOutcome::Cancelled)
        {
            SendCancel(300);
            outResult.status = VerifyStatus::Cancelled;
            outResult.reason = L"cancelled";
        }
        else if (writeOutcome == IoWaitOutcome::TimedOut)
        {
            outResult.status = VerifyStatus::Failed;
            outResult.reason = L"timeout";
        }
        else
        {
            outResult.status = VerifyStatus::Error;
            outResult.reason = FormatWin32Error(winErr);
        }
        return false;
    }

    std::string responseLine;
    const IoWaitOutcome readOutcome = ReadLineOverlapped(hPipe, cancelEvent, ipcDeadline, responseLine, winErr);
    CloseHandle(hPipe);

    if (readOutcome != IoWaitOutcome::Completed)
    {
        SecureWipeString(responseLine);
        if (readOutcome == IoWaitOutcome::Cancelled)
        {
            SendCancel(300);
            outResult.status = VerifyStatus::Cancelled;
            outResult.reason = L"cancelled";
        }
        else if (readOutcome == IoWaitOutcome::TimedOut)
        {
            SendCancel(300);
            outResult.status = VerifyStatus::Failed;
            outResult.reason = L"timeout";
        }
        else
        {
            outResult.status = VerifyStatus::Error;
            outResult.reason = FormatWin32Error(winErr);
        }
        return false;
    }

    std::string status;
    if (!TryGetJsonString(responseLine, "status", status))
    {
        SecureWipeString(responseLine);
        outResult.status = VerifyStatus::Error;
        outResult.reason = L"invalid_json";
        return false;
    }

    if (status == "matched")
    {
        std::string respUser;
        std::string respDomain;
        std::string respPassword;

        TryGetJsonString(responseLine, "username", respUser);
        TryGetJsonString(responseLine, "domain", respDomain);
        TryGetJsonString(responseLine, "password", respPassword);
        TryGetJsonDouble(responseLine, "score", outResult.score);
        TryGetJsonDouble(responseLine, "liveness_delta", outResult.livenessDelta);

        outResult.username = Utf8ToWide(respUser);
        outResult.domain = Utf8ToWide(respDomain);
        outResult.password = Utf8ToWide(respPassword);
        outResult.status = VerifyStatus::Matched;
        outResult.reason = L"matched";

        SecureWipeString(respPassword);
        SecureWipeString(respUser);
        SecureWipeString(respDomain);
    }
    else if (status == "failed")
    {
        std::string reason;
        if (TryGetJsonString(responseLine, "reason", reason))
        {
            outResult.reason = Utf8ToWide(reason);
        }
        else
        {
            outResult.reason = L"failed";
        }
        outResult.status = (outResult.reason == L"cancelled") ? VerifyStatus::Cancelled : VerifyStatus::Failed;
    }
    else
    {
        outResult.status = VerifyStatus::Error;
        outResult.reason = Utf8ToWide(status);
    }

    SecureWipeString(responseLine);
    return outResult.status == VerifyStatus::Matched;
}

} // namespace pipe_client
