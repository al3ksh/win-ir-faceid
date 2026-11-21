#include "pipe_client.h"

#include <cstdlib>
#include <iostream>
#include <string>

namespace
{

void PrintUsage()
{
    std::wcout
        << L"Usage:\n"
        << L"  pipe_client_test.exe ping\n"
        << L"  pipe_client_test.exe verify [username] [timeout_ms]\n"
        << L"  pipe_client_test.exe cancel\n";
}

} // namespace

int wmain(int argc, wchar_t* argv[])
{
    if (argc < 2)
    {
        PrintUsage();
        return 2;
    }

    const std::wstring cmd = argv[1];
    if (cmd == L"ping")
    {
        const pipe_client::PingResult res = pipe_client::Ping();
        if (!res.ok)
        {
            std::wcerr << L"[ping] FAILED: " << res.error << L"\n";
            return 1;
        }

        std::wcout << L"[ping] OK (version=" << res.version << L", enrolled_users=[";
        for (size_t i = 0; i < res.enrolledUsers.size(); ++i)
        {
            if (i > 0) std::wcout << L", ";
            std::wcout << res.enrolledUsers[i];
        }
        std::wcout << L"])\n";
        return 0;
    }

    if (cmd == L"verify")
    {
        std::wstring username;
        DWORD timeoutMs = kDefaultVerifyTimeoutMs;

        if (argc >= 3)
        {
            username = argv[2];
        }
        if (argc >= 4)
        {
            const unsigned long parsed = std::wcstoul(argv[3], nullptr, 10);
            if (parsed > 0)
            {
                timeoutMs = static_cast<DWORD>(parsed);
            }
        }

        std::wcout << L"[verify] Sending verify request (username=\""
                   << username << L"\", timeout_ms=" << timeoutMs << L")...\n";

        pipe_client::VerifyResult res;
        const bool matched = pipe_client::VerifyFace(username, timeoutMs, nullptr, res);
        if (matched)
        {
            std::wcout
                << L"[verify] MATCHED\n"
                << L"  username       : " << res.username << L"\n"
                << L"  domain         : " << res.domain << L"\n"
                << L"  password_saved : " << (res.password.empty() ? L"no" : L"yes") << L"\n"
                << L"  score          : " << res.score << L"\n"
                << L"  liveness_delta : " << res.livenessDelta << L"\n";
            res.Clear();
            return 0;
        }

        std::wcerr << L"[verify] NOT MATCHED (reason=" << res.reason << L")\n";
        res.Clear();
        return 1;
    }

    if (cmd == L"cancel")
    {
        const bool ok = pipe_client::SendCancel();
        if (ok)
        {
            std::wcout << L"[cancel] OK\n";
            return 0;
        }
        std::wcerr << L"[cancel] FAILED\n";
        return 1;
    }

    PrintUsage();
    return 2;
}
