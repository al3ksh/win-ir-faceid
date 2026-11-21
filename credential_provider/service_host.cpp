#include <windows.h>

#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <string>
#include <vector>

namespace
{

constexpr wchar_t kServiceName[] = L"WinIRFaceIDService";
constexpr ULONGLONG kMaxStartupLogBytes = 2ULL * 1024ULL * 1024ULL;
constexpr DWORD kRestartDelayMs = 2000;

SERVICE_STATUS_HANDLE g_statusHandle = nullptr;
SERVICE_STATUS g_serviceStatus{};
HANDLE g_stopEvent = nullptr;
std::filesystem::path g_cliRepoRoot;
bool g_consoleMode = false;

std::filesystem::path GetExecutablePath()
{
    std::vector<wchar_t> buf(MAX_PATH, L'\0');
    for (;;)
    {
        const DWORD len = GetModuleFileNameW(nullptr, buf.data(), static_cast<DWORD>(buf.size()));
        if (len == 0)
        {
            return {};
        }
        if (len < buf.size() - 1)
        {
            return std::filesystem::path(std::wstring(buf.data(), len));
        }
        if (buf.size() >= 32768)
        {
            return {};
        }
        buf.resize(buf.size() * 2, L'\0');
    }
}

bool IsValidRepoRoot(const std::filesystem::path& candidate)
{
    if (candidate.empty())
    {
        return false;
    }
    std::error_code ec;
    const auto pythonExe = candidate / L".venv" / L"Scripts" / L"python.exe";
    const auto cliPy = candidate / L"engine" / L"cli.py";
    return std::filesystem::is_regular_file(pythonExe, ec) &&
           std::filesystem::is_regular_file(cliPy, ec);
}

std::filesystem::path DetectRepoRootFromExe()
{
    const auto exePath = GetExecutablePath();
    if (exePath.empty())
    {
        return {};
    }

    auto current = exePath.parent_path();
    while (!current.empty())
    {
        if (IsValidRepoRoot(current))
        {
            return current;
        }
        const auto parent = current.parent_path();
        if (parent == current)
        {
            break;
        }
        current = parent;
    }
    return {};
}

bool ParseCommandLineArgs(
    int argc,
    wchar_t* argv[],
    std::filesystem::path& outRepoRoot,
    bool& outConsoleMode,
    bool& outHelp)
{
    outHelp = false;
    for (int i = 1; i < argc; ++i)
    {
        const std::wstring arg = argv[i];
        if (arg == L"--console")
        {
            outConsoleMode = true;
        }
        else if (arg == L"--help" || arg == L"-h" || arg == L"/?")
        {
            outHelp = true;
        }
        else if (arg == L"--repo-root")
        {
            if (i + 1 >= argc)
            {
                return false;
            }
            outRepoRoot = std::filesystem::path(argv[++i]);
        }
        else if (arg.rfind(L"--repo-root=", 0) == 0)
        {
            outRepoRoot = std::filesystem::path(arg.substr(12));
        }
        else
        {
            return false;
        }
    }
    return true;
}

std::string FormatTimestampPrefix()
{
    SYSTEMTIME st{};
    GetLocalTime(&st);
    char buf[64]{};
    std::snprintf(
        buf,
        sizeof(buf),
        "%04u-%02u-%02u %02u:%02u:%02u [service_host] ",
        st.wYear,
        st.wMonth,
        st.wDay,
        st.wHour,
        st.wMinute,
        st.wSecond);
    return std::string(buf);
}

std::string WideToUtf8(const std::wstring& wide)
{
    if (wide.empty())
    {
        return {};
    }
    const int needed = WideCharToMultiByte(
        CP_UTF8,
        0,
        wide.data(),
        static_cast<int>(wide.size()),
        nullptr,
        0,
        nullptr,
        nullptr);
    if (needed <= 0)
    {
        return {};
    }
    std::string utf8(static_cast<size_t>(needed), '\0');
    WideCharToMultiByte(
        CP_UTF8,
        0,
        wide.data(),
        static_cast<int>(wide.size()),
        utf8.data(),
        needed,
        nullptr,
        nullptr);
    return utf8;
}

void WriteHostLog(HANDLE hLog, bool consoleMode, const std::wstring& message)
{
    const std::string line = FormatTimestampPrefix() + WideToUtf8(message) + "\r\n";
    if (consoleMode)
    {
        std::fwrite(line.data(), 1, line.size(), stdout);
        std::fflush(stdout);
    }
    if (hLog != INVALID_HANDLE_VALUE && hLog != nullptr)
    {
        LARGE_INTEGER zero{};
        SetFilePointerEx(hLog, zero, nullptr, FILE_END);
        DWORD written = 0;
        WriteFile(hLog, line.data(), static_cast<DWORD>(line.size()), &written, nullptr);
        FlushFileBuffers(hLog);
    }
}

HANDLE OpenServiceLogFile(const std::filesystem::path& repoRoot)
{
    std::error_code ec;
    const auto dataDir = repoRoot / L"data";
    std::filesystem::create_directories(dataDir, ec);

    const auto logPath = dataDir / L"service.log";
    const auto rotatedPath = dataDir / L"service.log.1";

    bool truncateExisting = false;
    WIN32_FILE_ATTRIBUTE_DATA fad{};
    if (GetFileAttributesExW(logPath.c_str(), GetFileExInfoStandard, &fad))
    {
        const ULONGLONG fileSize =
            (static_cast<ULONGLONG>(fad.nFileSizeHigh) << 32) | fad.nFileSizeLow;
        if (fileSize > kMaxStartupLogBytes)
        {
            if (!MoveFileExW(logPath.c_str(), rotatedPath.c_str(), MOVEFILE_REPLACE_EXISTING))
            {
                truncateExisting = true;
            }
        }
    }

    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.lpSecurityDescriptor = nullptr;
    sa.bInheritHandle = TRUE;

    const DWORD disposition = truncateExisting ? CREATE_ALWAYS : OPEN_ALWAYS;
    HANDLE hFile = CreateFileW(
        logPath.c_str(),
        GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        &sa,
        disposition,
        FILE_ATTRIBUTE_NORMAL,
        nullptr);
    if (hFile != INVALID_HANDLE_VALUE)
    {
        LARGE_INTEGER zero{};
        SetFilePointerEx(hFile, zero, nullptr, FILE_END);
    }
    return hFile;
}

HANDLE OpenInheritableNullInput()
{
    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.lpSecurityDescriptor = nullptr;
    sa.bInheritHandle = TRUE;

    return CreateFileW(
        L"NUL",
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        &sa,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        nullptr);
}

HANDLE CreateKillOnCloseJob()
{
    HANDLE hJob = CreateJobObjectW(nullptr, nullptr);
    if (hJob == nullptr)
    {
        return nullptr;
    }

    JOBOBJECT_EXTENDED_LIMIT_INFORMATION jeli{};
    jeli.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!SetInformationJobObject(
            hJob,
            JobObjectExtendedLimitInformation,
            &jeli,
            sizeof(jeli)))
    {
        CloseHandle(hJob);
        return nullptr;
    }
    return hJob;
}

void ReportServiceStatus(DWORD currentState, DWORD win32ExitCode, DWORD waitHintMs)
{
    static DWORD checkPoint = 1;

    if (g_statusHandle == nullptr)
    {
        return;
    }

    g_serviceStatus.dwServiceType = SERVICE_WIN32_OWN_PROCESS;
    g_serviceStatus.dwCurrentState = currentState;
    g_serviceStatus.dwWin32ExitCode = win32ExitCode;
    g_serviceStatus.dwServiceSpecificExitCode = 0;
    g_serviceStatus.dwWaitHint = waitHintMs;

    if (currentState == SERVICE_START_PENDING ||
        currentState == SERVICE_STOP_PENDING ||
        currentState == SERVICE_STOPPED)
    {
        g_serviceStatus.dwControlsAccepted = 0;
    }
    else
    {
        g_serviceStatus.dwControlsAccepted = SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN;
    }

    if (currentState == SERVICE_RUNNING || currentState == SERVICE_STOPPED)
    {
        g_serviceStatus.dwCheckPoint = 0;
    }
    else
    {
        g_serviceStatus.dwCheckPoint = checkPoint++;
    }

    SetServiceStatus(g_statusHandle, &g_serviceStatus);
}

DWORD WINAPI ServiceCtrlHandler(
    DWORD dwControl,
    DWORD /*dwEventType*/,
    LPVOID /*lpEventData*/,
    LPVOID /*lpContext*/)
{
    switch (dwControl)
    {
    case SERVICE_CONTROL_STOP:
    case SERVICE_CONTROL_SHUTDOWN:
        ReportServiceStatus(SERVICE_STOP_PENDING, NO_ERROR, 5000);
        if (g_stopEvent != nullptr)
        {
            SetEvent(g_stopEvent);
        }
        return NO_ERROR;

    case SERVICE_CONTROL_INTERROGATE:
        ReportServiceStatus(g_serviceStatus.dwCurrentState, g_serviceStatus.dwWin32ExitCode, 0);
        return NO_ERROR;

    default:
        return ERROR_CALL_NOT_IMPLEMENTED;
    }
}

BOOL WINAPI ConsoleCtrlHandler(DWORD ctrlType)
{
    switch (ctrlType)
    {
    case CTRL_C_EVENT:
    case CTRL_BREAK_EVENT:
    case CTRL_CLOSE_EVENT:
    case CTRL_SHUTDOWN_EVENT:
        if (g_stopEvent != nullptr)
        {
            SetEvent(g_stopEvent);
        }
        return TRUE;

    default:
        return FALSE;
    }
}

void RunSupervisorLoop(const std::filesystem::path& repoRoot, bool consoleMode)
{
    HANDLE hLog = INVALID_HANDLE_VALUE;
    HANDLE hNullIn = INVALID_HANDLE_VALUE;
    if (!consoleMode)
    {
        hLog = OpenServiceLogFile(repoRoot);
        hNullIn = OpenInheritableNullInput();
    }

    HANDLE hJob = CreateKillOnCloseJob();
    if (hJob == nullptr)
    {
        WriteHostLog(
            hLog,
            consoleMode,
            L"Warning: CreateJobObjectW failed (error=" + std::to_wstring(GetLastError()) + L")");
    }

    const std::filesystem::path pythonExe = repoRoot / L".venv" / L"Scripts" / L"python.exe";
    WriteHostLog(hLog, consoleMode, L"Starting WinIRFaceID supervisor (repo=" + repoRoot.wstring() + L")");

    while (WaitForSingleObject(g_stopEvent, 0) != WAIT_OBJECT_0)
    {
        std::wstring cmdLine =
            L"\"" + pythonExe.wstring() + L"\" -u -m engine.cli serve --service-mode";
        std::vector<wchar_t> cmdBuf(cmdLine.begin(), cmdLine.end());
        cmdBuf.push_back(L'\0');

        STARTUPINFOW si{};
        si.cb = sizeof(si);
        if (!consoleMode)
        {
            si.dwFlags = STARTF_USESTDHANDLES;
            si.hStdInput = (hNullIn != INVALID_HANDLE_VALUE) ? hNullIn : nullptr;
            si.hStdOutput = (hLog != INVALID_HANDLE_VALUE) ? hLog : nullptr;
            si.hStdError = (hLog != INVALID_HANDLE_VALUE) ? hLog : nullptr;
        }

        PROCESS_INFORMATION pi{};
        DWORD creationFlags = CREATE_SUSPENDED;
        if (!consoleMode)
        {
            creationFlags |= CREATE_NO_WINDOW;
        }

        const BOOL started = CreateProcessW(
            pythonExe.c_str(),
            cmdBuf.data(),
            nullptr,
            nullptr,
            TRUE,
            creationFlags,
            nullptr,
            repoRoot.c_str(),
            &si,
            &pi);

        if (!started)
        {
            const DWORD err = GetLastError();
            WriteHostLog(
                hLog,
                consoleMode,
                L"CreateProcessW failed (error=" + std::to_wstring(err) + L"), retrying in 2s");
            if (WaitForSingleObject(g_stopEvent, kRestartDelayMs) == WAIT_OBJECT_0)
            {
                break;
            }
            continue;
        }

        if (hJob != nullptr)
        {
            if (!AssignProcessToJobObject(hJob, pi.hProcess))
            {
                WriteHostLog(
                    hLog,
                    consoleMode,
                    L"Warning: AssignProcessToJobObject failed (error=" +
                        std::to_wstring(GetLastError()) + L")");
            }
        }

        ResumeThread(pi.hThread);
        CloseHandle(pi.hThread);
        pi.hThread = nullptr;

        WriteHostLog(
            hLog,
            consoleMode,
            L"Launched Python IPC server (pid=" + std::to_wstring(pi.dwProcessId) + L")");

        HANDLE waitHandles[2] = { g_stopEvent, pi.hProcess };
        const DWORD waitRes = WaitForMultipleObjects(2, waitHandles, FALSE, INFINITE);

        if (waitRes == WAIT_OBJECT_0)
        {
            WriteHostLog(hLog, consoleMode, L"Stop requested; terminating child process");
            if (hJob != nullptr)
            {
                TerminateJobObject(hJob, 0);
            }
            else
            {
                TerminateProcess(pi.hProcess, 0);
            }
            WaitForSingleObject(pi.hProcess, 3000);
            CloseHandle(pi.hProcess);
            break;
        }

        if (waitRes == WAIT_OBJECT_0 + 1)
        {
            DWORD exitCode = 0;
            GetExitCodeProcess(pi.hProcess, &exitCode);
            CloseHandle(pi.hProcess);
            WriteHostLog(
                hLog,
                consoleMode,
                L"Python IPC server exited unexpectedly (exitCode=" +
                    std::to_wstring(exitCode) + L"), restarting in 2s");

            if (WaitForSingleObject(g_stopEvent, kRestartDelayMs) == WAIT_OBJECT_0)
            {
                break;
            }
            continue;
        }

        const DWORD waitErr = GetLastError();
        WriteHostLog(
            hLog,
            consoleMode,
            L"WaitForMultipleObjects failed (error=" + std::to_wstring(waitErr) + L")");
        TerminateProcess(pi.hProcess, 1);
        CloseHandle(pi.hProcess);
        break;
    }

    WriteHostLog(hLog, consoleMode, L"WinIRFaceID supervisor stopped");

    if (hJob != nullptr)
    {
        CloseHandle(hJob);
    }
    if (hNullIn != INVALID_HANDLE_VALUE)
    {
        CloseHandle(hNullIn);
    }
    if (hLog != INVALID_HANDLE_VALUE)
    {
        CloseHandle(hLog);
    }
}

void WINAPI ServiceMain(DWORD dwArgc, LPWSTR* lpszArgv)
{
    g_statusHandle = RegisterServiceCtrlHandlerExW(kServiceName, ServiceCtrlHandler, nullptr);
    if (g_statusHandle == nullptr)
    {
        return;
    }

    ReportServiceStatus(SERVICE_START_PENDING, NO_ERROR, 5000);

    std::filesystem::path repoRoot = g_cliRepoRoot;
    if (repoRoot.empty() && dwArgc > 1 && lpszArgv != nullptr)
    {
        bool unusedConsole = false;
        bool unusedHelp = false;
        ParseCommandLineArgs(static_cast<int>(dwArgc), lpszArgv, repoRoot, unusedConsole, unusedHelp);
    }
    if (repoRoot.empty())
    {
        repoRoot = DetectRepoRootFromExe();
    }

    if (!IsValidRepoRoot(repoRoot))
    {
        ReportServiceStatus(SERVICE_STOPPED, ERROR_FILE_NOT_FOUND, 0);
        return;
    }

    g_stopEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (g_stopEvent == nullptr)
    {
        ReportServiceStatus(SERVICE_STOPPED, GetLastError(), 0);
        return;
    }

    ReportServiceStatus(SERVICE_RUNNING, NO_ERROR, 0);
    RunSupervisorLoop(repoRoot, false);

    CloseHandle(g_stopEvent);
    g_stopEvent = nullptr;
    ReportServiceStatus(SERVICE_STOPPED, NO_ERROR, 0);
}

void PrintUsage()
{
    std::wprintf(
        L"Usage:\n"
        L"  WinIRFaceIDService.exe [--repo-root <path>] [--console]\n"
        L"Options:\n"
        L"  --repo-root <path>  Path to win-ir-faceid repository root (auto-detected if omitted)\n"
        L"  --console           Run in console foreground instead of SCM service dispatcher\n");
}

} // namespace

int wmain(int argc, wchar_t* argv[])
{
    bool showHelp = false;
    if (!ParseCommandLineArgs(argc, argv, g_cliRepoRoot, g_consoleMode, showHelp))
    {
        PrintUsage();
        return 2;
    }

    if (showHelp)
    {
        PrintUsage();
        return 0;
    }

    if (g_consoleMode)
    {
        std::filesystem::path repoRoot = g_cliRepoRoot.empty()
            ? DetectRepoRootFromExe()
            : g_cliRepoRoot;
        if (!IsValidRepoRoot(repoRoot))
        {
            std::fwprintf(
                stderr,
                L"Error: Invalid repo root '%ls' (expected .venv\\Scripts\\python.exe and engine\\cli.py).\n",
                repoRoot.empty() ? L"<auto-detect failed>" : repoRoot.c_str());
            return 1;
        }

        g_stopEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        if (g_stopEvent == nullptr)
        {
            std::fwprintf(stderr, L"Error: CreateEventW failed (%lu).\n", GetLastError());
            return 1;
        }

        SetConsoleCtrlHandler(ConsoleCtrlHandler, TRUE);
        RunSupervisorLoop(repoRoot, true);
        SetConsoleCtrlHandler(ConsoleCtrlHandler, FALSE);

        CloseHandle(g_stopEvent);
        g_stopEvent = nullptr;
        return 0;
    }

    SERVICE_TABLE_ENTRYW dispatchTable[] = {
        { const_cast<LPWSTR>(kServiceName), ServiceMain },
        { nullptr, nullptr }
    };

    if (!StartServiceCtrlDispatcherW(dispatchTable))
    {
        const DWORD err = GetLastError();
        if (err == ERROR_FAILED_SERVICE_CONTROLLER_CONNECT)
        {
            std::fwprintf(
                stderr,
                L"WinIRFaceIDService must be started by Windows SCM, or run with --console for foreground testing.\n");
        }
        else
        {
            std::fwprintf(stderr, L"StartServiceCtrlDispatcherW failed (error=%lu).\n", err);
        }
        return static_cast<int>(err);
    }

    return 0;
}
