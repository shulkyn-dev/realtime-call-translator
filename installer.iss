; installer.iss — Inno Setup 6 скрипт установщика Realtime Translator (ЭТАП 3).
;
; Per-user установка (PrivilegesRequired=lowest) — ставится в
; %LOCALAPPDATA%\Programs\Realtime Translator, без запроса прав администратора,
; как VS Code. AppId — фиксированный GUID, сгенерирован один раз ниже, не менять
; между версиями (иначе апдейт перестанет находить предыдущую установку).
;
; Сборка: сначала пересобрать exe —
;   .venv\Scripts\pyinstaller translator.spec --noconfirm
; затем скомпилировать установщик —
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss

#define MyAppName "Realtime Translator"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "MakeFlows"
#define MyAppExeName "RealtimeTranslator.exe"
; AppUserModelID — тот же, что приложение выставляет себе в app.py run()
; (SetCurrentProcessExplicitAppUserModelID) — критично для группировки
; кнопок таскбара под одной иконкой, а не под python.exe/generic
#define MyAppUserModelID "MakeFlows.RealtimeTranslator"

[Setup]
; фиксированный GUID приложения — сгенерирован для этого проекта, не менять
; между версиями (иначе апдейт перестанет находить предыдущую установку).
; "{{" — экранированная открывающая фигурная скобка (синтаксис Inno Setup).
AppId={{23319E9A-72AF-4321-B7BA-F03B075FC817}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Realtime Translator
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; per-user установка — не требует прав администратора, ставится в
; %LOCALAPPDATA%\Programs (autopf разворачивается в userpf при lowest)
PrivilegesRequired=lowest
SetupIconFile=assets\icon.ico
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
OutputDir=installer_out
OutputBaseFilename=RealtimeTranslator-Setup
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\RealtimeTranslator\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
; AppUserModelID у ярлыков должен совпадать с тем, что процесс выставляет
; себе сам (app.py, run()) — иначе Windows не сгруппирует их под одной
; иконкой в таскбаре
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "{#MyAppUserModelID}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; AppUserModelID: "{#MyAppUserModelID}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: postinstall nowait skipifsilent
