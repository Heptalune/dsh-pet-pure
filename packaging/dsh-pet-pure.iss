; -*- mode: ini -*-
; Pure desktop-pet installer (Inno Setup 6) - pet only, no AI/no optional modules.
;
; Compile:
;   ISCC.exe packaging\dsh-pet-pure.iss
;
; Prerequisite: scripts\build_pure.ps1 has produced dist-onedir\dsh-pet-pure\.
; Output: dist-onedir\dsh-pet-pure-setup.exe
;
; NOTE: strings are ASCII-only (this file is UTF-8 no BOM, read as ANSI by Inno).

#ifndef MyAppShortName
#define MyAppShortName "dsh-pet-pure"
#endif
#ifndef MyAppExeName
#define MyAppExeName "dsh-pet-pure.exe"
#endif
#ifndef MyAppDir
#define MyAppDir "..\dist-onedir\dsh-pet-pure"
#endif
#ifndef MyAppId
#define MyAppId "{{7e1b6d94-2c58-4b7f-9a30-d8c9e6a1f5b2}"
#endif
#ifndef MyAppDisplay
#define MyAppDisplay "dsh-pet-pure"
#endif
#ifndef MyAppVersion
; Keep in sync with pet/__init__.py __version__; inject via /DMyAppVersion=<v>.
#define MyAppVersion "1.0.0"
#endif

[Setup]
AppId={#MyAppId}
AppName={#MyAppDisplay}
AppVersion={#MyAppVersion}
AppPublisher=Heptalune
SetupIconFile=..\assets\icon.ico
; Pure variant stores its config under dsh-pet-standalone-pure (pet/config.py
; reads packaging/build_variant.py VARIANT='pure'), fully isolated from the
; full variants - both can be installed side by side.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#MyAppShortName}
DisableProgramGroupPage=yes
OutputDir=..\dist-onedir
OutputBaseFilename={#MyAppShortName}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64bitMode=x64compatible
CloseApplications=yes
CloseApplicationsFilter=*.exe
RestartApplications=no
UninstallDisplayName={#MyAppDisplay}

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#MyAppDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppShortName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppShortName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppShortName}}"; Flags: nowait postinstall

[Code]
{ Uninstall cleanup must run before the exe is deleted (same as dsh-pet.iss). }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  CleanupExe: String;
  WorkDir: String;
  ResultCode: Integer;
begin
  if CurUninstallStep <> usUninstall then
    exit;
  CleanupExe := ExpandConstant('{app}\{#MyAppExeName}');
  if not FileExists(CleanupExe) then
    exit;
  WorkDir := ExpandConstant('{%TEMP}');
  if (WorkDir = '') or (not DirExists(WorkDir)) then
    WorkDir := ExpandConstant('{app}');
  if Exec(CleanupExe, '--uninstall-cleanup', WorkDir,
          SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    if ResultCode = 0 then
      Log('dsh-pet-pure uninstall cleanup finished (exit 0)')
    else
      Log('dsh-pet-pure uninstall cleanup exit code ' + IntToStr(ResultCode));
  end
  else
    Log('dsh-pet-pure uninstall cleanup could not start: ' + SysErrorMessage(ResultCode));
end;
