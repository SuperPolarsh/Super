; Super — Windows installer (Inno Setup 6)
; Build with: installer\build_release.ps1
; Per-user install so updates need no UAC. Super.exe is the small launcher;
; the app itself lives in {app}\app (unpacked once, so it starts fast).

#define AppName "Super"
#define AppExeName "Super.exe"
#define AppPublisher "Super"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{B1E4D0A2-5C3F-4E88-9A17-2F6D8C4B7E31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\Super
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\release
OutputBaseFilename=Super-Setup-{#AppVersion}
SetupIconFile=..\assets\super.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}.0
VersionInfoProductName={#AppName}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} installer

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\release\Super.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\release\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Parameters: "--no-check"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Cached update downloads live outside the install folder.
Type: filesandordirs; Name: "{localappdata}\Super\updates"
Type: filesandordirs; Name: "{localappdata}\Super\downloads"
Type: filesandordirs; Name: "{app}\app"
Type: files; Name: "{app}\version.txt"

[Code]
// Keeps the bootstrapper's "already up to date" check accurate after the app
// has updated itself in place (the registry version only changes on reinstall).
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    SaveStringToFile(ExpandConstant('{app}\version.txt'), '{#AppVersion}', False);
end;
