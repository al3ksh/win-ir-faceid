; WinIRFaceID Windows 11 x64 Installer Script (Inno Setup 6)
; Compile with: ISCC.exe /DAppVersion=0.1.0 installer\WinIRFaceID.iss

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{49C8AEC3-2A43-4BED-987D-DE31E44DF41D}
AppName=WinIRFaceID
AppVersion={#AppVersion}
AppVerName=WinIRFaceID {#AppVersion}
AppPublisher=al3ksh
AppPublisherURL=https://github.com/al3ksh/win-ir-faceid
AppSupportURL=https://github.com/al3ksh/win-ir-faceid/issues
AppUpdatesURL=https://github.com/al3ksh/win-ir-faceid/releases
DefaultDirName={autopf}\WinIRFaceID
DefaultGroupName=WinIRFaceID
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
SetupIconFile=..\engine\ui\app.ico
UninstallDisplayIcon={app}\engine\ui\app.ico
WizardStyle=modern
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=WinIRFaceID-Setup-v{#AppVersion}-x64
Compression=lzma2
SolidCompression=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\stage\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\WinIRFaceID"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: "-m engine.cli gui"; WorkingDir: "{app}"; IconFilename: "{app}\engine\ui\app.ico"; Comment: "WinIRFaceID - Active IR Face Unlock"
Name: "{autodesktop}\WinIRFaceID"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: "-m engine.cli gui"; WorkingDir: "{app}"; IconFilename: "{app}\engine\ui\app.ico"; Comment: "WinIRFaceID - Active IR Face Unlock"; Tasks: desktopicon

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\setup.ps1"" -NoLaunch -UserAccount ""{username}"""; WorkingDir: "{app}"; StatusMsg: "Setting up Python runtime, AI models, and Windows lock screen service..."; Flags: waituntilterminated
Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: "-m engine.cli gui"; WorkingDir: "{app}"; Description: "Launch WinIRFaceID to enroll your face"; Flags: postinstall nowait skipifsilent runasoriginaluser

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\credential_provider\uninstall.ps1"""; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; RunOnceId: "UnregisterCredentialProvider"
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\uninstall_service.ps1"""; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; RunOnceId: "UninstallService"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\engine\__pycache__"
Type: filesandordirs; Name: "{app}\engine\ui\__pycache__"
