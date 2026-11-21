cmake_minimum_required(VERSION 3.20)
project(WinIRFaceIDProvider LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_compile_definitions(
    UNICODE
    _UNICODE
    WIN32_LEAN_AND_MEAN
    NOMINMAX
)

if(MSVC)
    add_compile_options(/W4 /EHsc /permissive-)
endif()

# Windows Credential Provider V2 COM DLL (thin IPC client for LogonUI.exe)
add_library(WinIRFaceIDProvider SHARED
    dllmain.cpp
    provider.cpp
    credential.cpp
    pipe_client.cpp
    WinIRFaceIDProvider.def
)

set_target_properties(WinIRFaceIDProvider PROPERTIES
    PREFIX ""
    OUTPUT_NAME "WinIRFaceIDProvider"
)

target_link_libraries(WinIRFaceIDProvider PRIVATE
    credui
    secur32
    shlwapi
    advapi32
    ole32
    user32
    gdi32
)

# Standalone console test client to verify Named Pipe IPC without LogonUI.exe
add_executable(pipe_client_test
    pipe_client_test.cpp
    pipe_client.cpp
)

target_link_libraries(pipe_client_test PRIVATE
    advapi32
    user32
)
