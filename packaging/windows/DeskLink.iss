; Instalador do DeskLink (Inno Setup 6). Compile com:
;   iscc /DAppVersion=1.0.0 /DSourceExe=caminho\DeskLink.exe packaging\windows\DeskLink.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceExe
  #define SourceExe "..\..\publish\win-x64\DeskLink.exe"
#endif

[Setup]
AppId={{6E1D4A52-9B8C-4C57-A8F4-DE5C11CE7A01}
AppName=DeskLink
AppVersion={#AppVersion}
AppPublisher=Jhordan Pastorello
AppPublisherURL=https://github.com/jhordan-pastorello/trnspositor-tuch-teclado
DefaultDirName={autopf}\DeskLink
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir=..\..\dist
OutputBaseFilename=DeskLink-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=DeskLink
CloseApplications=no

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#SourceExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "register-task.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\register-task.ps1"" -Exe ""{app}\DeskLink.exe"""; Flags: runhidden waituntilterminated; StatusMsg: "Registrando tarefa de inicializacao..."
Filename: "netsh.exe"; Parameters: "advfirewall firewall add rule name=""DeskLink"" dir=in action=allow program=""{app}\DeskLink.exe"" protocol=UDP profile=private"; Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\register-task.ps1"" -Exe ""{app}\DeskLink.exe"" -Remove"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveTask"
Filename: "netsh.exe"; Parameters: "advfirewall firewall delete rule name=""DeskLink"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveFw"
