; -*- mode: ini -*-
; Generic dsh-pet-standalone onedir installer (Inno Setup 6)
;
; Compile (defaults build the webm-chat variant):
;   E:\tools\InnoSetup6\ISCC.exe packaging\dsh-pet.iss
;
; Other variants (override defines on the command line):
;   E:\tools\InnoSetup6\ISCC.exe /DMyAppShortName=dsh-pet-standalone-webm `
;       /DMyAppExeName=dsh-pet-standalone-webm.exe `
;       /DMyAppDir=..\dist-onedir\dsh-pet-standalone-webm `
;       /DMyAppId=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx `
;       /DMyAppDisplay="dsh-pet-standalone (WebM)" packaging\dsh-pet.iss
;
; Output: dist-onedir\<shortname>-setup.exe

#ifndef MyAppShortName
#define MyAppShortName "dsh-pet-standalone-webm-chat"
#endif
#ifndef MyAppExeName
#define MyAppExeName "dsh-pet-standalone-webm-chat.exe"
#endif
#ifndef MyAppDir
#define MyAppDir "..\dist-onedir\dsh-pet-standalone-webm-chat"
#endif
#ifndef MyAppId
; NOTE: value must include the double-brace escaping required by AppId ({{GUID})
#define MyAppId "{{3424d6cc-af3c-4383-8797-ab520b923aa6}"
#endif
#ifndef MyAppDisplay
#define MyAppDisplay "dsh-pet-standalone (WebM Chat)"
#endif
#ifndef MyAppVersion
; 默认版本与 pet/__init__.py 的 __version__ 一致；CI 构建时通过
; /DMyAppVersion=<版本> 从单一来源注入（见 build-windows.yml）。
#define MyAppVersion "4.3.0"
#endif

[Setup]
AppId={#MyAppId}
AppName={#MyAppDisplay}
AppVersion={#MyAppVersion}
AppPublisher=merzlin
; 安装包图标（待机封面帧生成，scripts/make_icon.py）
SetupIconFile=..\assets\icon.ico
; Per-user install, no admin needed; user may pick any drive in the wizard
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#MyAppShortName}
DisableProgramGroupPage=yes
OutputDir=..\dist-onedir
OutputBaseFilename={#MyAppShortName}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 在线更新会以静默方式覆盖现有 onedir；由 Inno 负责关闭占用文件的旧进程。
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

; 注：这里刻意没有 [UninstallDelete]。
; 旧版有一条 `Type: filesandordirs; Name: "{app}\_MEI*"`，是按名字通配删除——
; `_MEI数字` 是 PyInstaller 的解包目录命名，安装目录里出现的同名目录并不保证是
; 本产品的（任何以本目录为工作目录启动的 onefile 程序都会在这里解包）。没有归属
; 证据就不删：本产品自己的 onefile 运行目录由 pet/runtime_cleanup.py 按归属标记
; 清理（见下方 [Code] 的卸载清理调用）。

[Code]
{ 卸载清理必须在删除 exe 之前跑：exe 一旦被删，清理实现就没了。
  usUninstall 是卸载流程里最早的步骤，此处的 Exec 先于文件删除。
  清理只删本产品 own 的东西（自启项 / Claude hooks / DSH 桥接插件），
  失败必须留痕：退出码写进卸载日志，交互式卸载还会弹一次提示。
  注：本 .iss 是 UTF-8 无 BOM（Inno 按 ANSI 读取），因此**字符串字面量只用 ASCII**，
  中文说明留在注释里；面向用户的详细结果由 Python 侧写进 uninstall-cleanup.log。}
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
  begin
    Log('dsh-pet uninstall cleanup skipped: executable not found: ' + CleanupExe);
    exit;
  end;
  { 工作目录先取系统临时目录：onefile 形态会用 --runtime-tmpdir 在当前目录解包，
    指到安装目录会在卸载过程中再造一个解包目录；TEMP 不可用时退回安装目录本身
    （它一定存在，因为 exe 就在那里）。}
  WorkDir := ExpandConstant('{%TEMP}');
  if (WorkDir = '') or (not DirExists(WorkDir)) then
    WorkDir := ExpandConstant('{app}');
  if Exec(CleanupExe, '--uninstall-cleanup', WorkDir,
          SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    if ResultCode = 0 then
      Log('dsh-pet uninstall cleanup finished (exit 0)')
    else
    begin
      Log('dsh-pet uninstall cleanup failed for some steps (exit '
        + IntToStr(ResultCode) + ')');
      SuppressibleMsgBox('dsh-pet uninstall cleanup did not fully succeed (exit code '
        + IntToStr(ResultCode) + '). Autostart, Claude hooks or the DSH bridge plugin'
        + ' may still be present; see uninstall-cleanup.log in the configuration'
        + ' directory for details.', mbError, MB_OK, IDOK);
    end;
  end
  else
  begin
    Log('dsh-pet uninstall cleanup could not start: ' + SysErrorMessage(ResultCode));
    SuppressibleMsgBox('Could not run the dsh-pet uninstall cleanup program: '
      + SysErrorMessage(ResultCode), mbError, MB_OK, IDOK);
  end;
end;
