; Inno Setup script for Eve-Strait.
;
; Wraps the Nuitka --standalone program folder (dist\Eve-Strait) into an
; installer, the same shape pyfa ships: a folder-based app plus an installer,
; never a self-extracting single exe. Build it with:
;
;   scripts\build_exe.ps1          (calls ISCC automatically when installed)
;   ISCC.exe dist_assets\win\eve-strait.iss   (manually)
;
; Install Inno Setup with:  winget install JRSoftware.InnoSetup

#define AppName "Eve-Strait"
#define AppExe  "eve-strait.exe"
; CI passes the tag version with /DAppVersion=1.2.3
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{4C1B9E4E-6F3A-4C6E-9F2B-1E7A2D5C8B10}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppName}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Per-user install by default: no UAC prompt, and elevation is one more thing
; for endpoint protection to look at.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\..\dist
OutputBaseFilename={#AppName}-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=eve-strait.ico
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; The whole Nuitka standalone folder.
Source: "..\..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Map data and settings live in %LOCALAPPDATA%\eve-strait and are intentionally
; left in place, so reinstalling keeps your Client ID, token and cached SDE.
Type: dirifempty; Name: "{app}"
