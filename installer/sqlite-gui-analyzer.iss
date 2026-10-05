; Inno Setup script for SQLite GUI Analyzer (Inno Setup 6).
;
; Build the PyInstaller onedir folder first (see sqlite-gui-analyzer.spec), then:
;   ISCC /DAppVersion=<VERSION> /DDistDir=<dist>\SQLiteGUIAnalyzer /DOutputDir=<out> installer\sqlite-gui-analyzer.iss
; <VERSION> is VERSION in src/constants.py (python tools/release.py version).
;
; One setup.exe installs either for the current user (default, no administrator rights) or for
; all users: the wizard asks, or pass /CURRENTUSER or /ALLUSERS. Silent install:
;   SQLiteGUIAnalyzer-<VERSION>-setup.exe /VERYSILENT /CURRENTUSER

#ifndef AppVersion
  #error Pass the version: ISCC /DAppVersion=X.Y.Z (VERSION in src/constants.py)
#endif
#ifndef DistDir
  #error Pass the PyInstaller onedir folder: ISCC /DDistDir=...\SQLiteGUIAnalyzer
#endif
#ifndef OutputDir
  #define OutputDir "Output"
#endif

; VersionInfoVersion takes numbers only: drop a pre-release suffix (X.Y.Z-rc1 -> X.Y.Z).
#if Pos("-", AppVersion) > 0
  #define NumericVersion Copy(AppVersion, 1, Pos("-", AppVersion) - 1)
#else
  #define NumericVersion AppVersion
#endif

#define AppName "SQLite GUI Analyzer"
#define AppId "SQLiteGUIAnalyzer"
#define AppExeName "SQLiteGUIAnalyzer.exe"
#define AppPublisher "Akhil Dara"
#define AppURL "https://github.com/akhil-dara/sqlite-gui-analyzer"
#define ProgId "SQLiteGUIAnalyzer.Database"

#if !FileExists(AddBackslash(DistDir) + AppExeName)
  #error DistDir does not contain SQLiteGUIAnalyzer.exe: build the onedir folder first
#endif

[Setup]
; AppId identifies the product for upgrades and uninstall: never change it.
AppId={{E08D94DF-5A85-4371-85AC-88457E77ED24}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
; Per-user by default; the wizard (or /ALLUSERS) can switch to an all-users install.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
#if Ver < EncodeVer(6, 3, 0)
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
#else
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
#endif
; The bundled Python supports Windows 10 and newer.
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename={#AppId}-{#AppVersion}-setup
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName} {#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ChangesAssociations=yes
VersionInfoVersion={#NumericVersion}
VersionInfoTextVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#NumericVersion}
VersionInfoProductTextVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Setup

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "openwith"; Description: "Offer {#AppName} in ""Open with"" for .db, .sqlite, .sqlite3 and .db3 files"; GroupDescription: "File types:"; Flags: unchecked

[InstallDelete]
; An upgrade replaces the bundled runtime completely (file names change between versions).
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; "Open with" entries only (the default program for these extensions is left alone). HKA is
; HKEY_CURRENT_USER for a per-user install and HKEY_LOCAL_MACHINE for an all-users install.
Root: HKA; Subkey: "Software\Classes\{#ProgId}"; ValueType: string; ValueName: ""; ValueData: "SQLite database"; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\{#ProgId}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExeName},0"; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\{#ProgId}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: openwith
; Extension keys this created are removed again on uninstall when nothing else uses them.
Root: HKA; Subkey: "Software\Classes\.db"; Flags: uninsdeletekeyifempty; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.db\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.sqlite"; Flags: uninsdeletekeyifempty; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.sqlite\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.sqlite3"; Flags: uninsdeletekeyifempty; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.sqlite3\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.db3"; Flags: uninsdeletekeyifempty; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.db3\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExeName}"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#AppName}"; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExeName}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExeName}\SupportedTypes"; ValueType: string; ValueName: ".db"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExeName}\SupportedTypes"; ValueType: string; ValueName: ".sqlite"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExeName}\SupportedTypes"; ValueType: string; ValueName: ".sqlite3"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExeName}\SupportedTypes"; ValueType: string; ValueName: ".db3"; ValueData: ""; Tasks: openwith

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
