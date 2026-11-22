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
; The LocalSystem service runs code from {app}; keep it in admin-only Program Files.
DisableDirPage=yes
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
; setup.ps1 runs hidden from [Code] (CurStepChanged) so its exit code is checked.
Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: "-m engine.cli gui"; WorkingDir: "{app}"; Description: "Launch WinIRFaceID to enroll your face"; Flags: postinstall nowait skipifsilent runasoriginaluser; Check: SetupSucceeded

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\credential_provider\uninstall.ps1"""; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; RunOnceId: "UnregisterCredentialProvider"
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\uninstall_service.ps1"""; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; RunOnceId: "UninstallService"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\wheels"
Type: filesandordirs; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\engine\__pycache__"
Type: filesandordirs; Name: "{app}\engine\ui\__pycache__"
Type: files; Name: "{app}\setup.log"

[Code]
var
  SetupOk: Boolean;
  SetupExitCode: Integer;
  PurgeData: Boolean;

function SetupSucceeded: Boolean;
begin
  Result := SetupOk;
end;

procedure StopAppProcesses;
var
  ResultCode: Integer;
begin
  // Stop the service and any process running from the install dir (GUI, Python runtime)
  // so bin, runtime, .venv and data files are not locked.
  Exec('sc.exe', 'stop WinIRFaceIDService', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec('powershell.exe',
    '-NoProfile -ExecutionPolicy Bypass -Command "$r=''' + ExpandConstant('{app}') + '\''; ' +
    'Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and ' +
    '$_.ExecutablePath.StartsWith($r, [StringComparison]::OrdinalIgnoreCase) } | ' +
    'ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1200);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopAppProcesses;
  Result := '';
end;

procedure RunAppSetup;
var
  LogPath: String;
  Params: String;
begin
  LogPath := ExpandConstant('{app}\setup.log');
  Params := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\scripts\setup.ps1') +
            '" -NoLaunch -InPlace -LogPath "' + LogPath + '"';

  WizardForm.StatusLabel.Caption := 'Setting up the Python runtime, AI models and lock screen service...';
  WizardForm.FilenameLabel.Caption := 'This can take a few minutes. Log: ' + LogPath;
  WizardForm.ProgressGauge.Style := npbstMarquee;
  try
    if not Exec('powershell.exe', Params, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, SetupExitCode) then
      SetupExitCode := -1;
  finally
    WizardForm.ProgressGauge.Style := npbstNormal;
  end;

  SetupOk := (SetupExitCode = 0);
  if not SetupOk then
    SuppressibleMsgBox(
      'WinIRFaceID files were copied to ' + ExpandConstant('{app}') + ', but the final setup step failed ' +
      '(exit code ' + IntToStr(SetupExitCode) + ').' + #13#10#13#10 +
      'Face unlock is NOT active yet.' + #13#10#13#10 +
      'Details are in the setup log:' + #13#10 + LogPath + #13#10#13#10 +
      'Fix the cause and run this installer again.',
      mbCriticalError, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    RunAppSetup;
end;

function GetCustomSetupExitCode: Integer;
begin
  // Unattended (/SILENT) installs report a failed setup step to the caller.
  if SetupOk then
    Result := 0
  else
    Result := 1;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
  PythonExe: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    // Runs before [UninstallRun] and before files are removed, so the venv is still usable.
    PurgeData := False;
    if not UninstallSilent then
      PurgeData := MsgBox(
        'Also delete your saved face profiles and the stored Windows password?' + #13#10#13#10 +
        'Yes - remove everything (recommended unless you plan to reinstall).' + #13#10 +
        'No - keep them in ' + ExpandConstant('{app}\data') + ' for a later reinstall.',
        mbConfirmation, MB_YESNO) = IDYES;
    if PurgeData then
    begin
      StopAppProcesses;
      PythonExe := ExpandConstant('{app}\.venv\Scripts\python.exe');
      // Deletes the TPM password key and the data directory.
      if FileExists(PythonExe) then
        Exec(PythonExe, '-m engine.cli purge-data --yes', ExpandConstant('{app}'), SW_HIDE,
          ewWaitUntilTerminated, ResultCode);
    end;
  end
  else if CurUninstallStep = usPostUninstall then
  begin
    if PurgeData then
      DelTree(ExpandConstant('{app}\data'), True, True, True);
    // Removes the install dir only when nothing (e.g. kept data) is left in it.
    RemoveDir(ExpandConstant('{app}'));
  end;
end;
