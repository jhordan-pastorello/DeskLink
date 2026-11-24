# DeskLink

**Mouse e teclado compartilhados entre Windows e Linux (GNOME/Wayland), como se fossem um só computador.**

Encoste o ponteiro no topo da tela do notebook: o teclado e o touchpad passam a controlar o
servidor Linux. Volte descendo até a borda inferior (ou com `Ctrl+Alt+Espaço`). A área de
transferência vai junto — texto **e imagens** (aquele print do GNOME que você cola com `Ctrl+V`).
E a pasta `Z:` mapeada no Windows é o mesmo diretório do Linux: arraste arquivos entre os dois.

Sem portal, sem diálogo de permissão, sem root em runtime. Sobe sozinho no login dos dois lados.

```
┌──────────────── Windows (notebook) ────────────────┐        ┌──── Linux (servidor GNOME) ────┐
│  DeskLink.exe  (invisível, tarefa de logon)        │        │  desklink.service (systemd)     │
│  ├─ WH_MOUSE_LL / WH_KEYBOARD_LL  (captura)        │  TCP   │  ├─ TCP 47810 + TLS             │
│  ├─ Raw Input                     (deltas)         │ ─────► │  ├─ /dev/uinput: tablet + kbd   │
│  ├─ UDP 47811 discovery                            │  4.7k  │  ├─ clipboard (wl-copy/paste)   │
│  └─ clipboard texto + PNG                          │  TLS   │  └─ UDP 47811 discovery         │
└────────────────────────────────────────────────────┘        └─────────────────────────────────┘
```

---

## Índice

- [Como funciona](#como-funciona)
- [Requisitos](#requisitos)
- [Instalação](#instalação)
- [Uso](#uso)
- [Configuração](#configuração)
- [Protocolo](#protocolo)
- [Solução de problemas](#solução-de-problemas)
- [Limitações](#limitações)
- [Desinstalar](#desinstalar)
- [Estrutura do projeto](#estrutura-do-projeto)

---

## Como funciona

### Captura (Windows)
Um processo **sem interface** (`WinExe`, sem janela e sem ícone de bandeja) roda com privilégio
máximo, iniciado por uma tarefa agendada no logon:

- hooks de baixo nível (`WH_MOUSE_LL` / `WH_KEYBOARD_LL`) interceptam e **suprimem** o input local
  quando o controle está no Linux;
- **Raw Input** fornece os deltas reais do dispositivo — funciona inclusive durante arrasto/captura,
  quando `SetCursorPos` é ignorado pelo Windows;
- o cursor local é estacionado numa janela 2×2 com cursor nulo (`ClipCursor`), então ele não
  aparece nem vaza para a tela errada.

### Injeção (Linux)
Um daemon Python cria dispositivos **virtuais no kernel** via `/dev/uinput`:

| Dispositivo | Tipo | Por quê |
|---|---|---|
| `DeskLink Virtual Tablet` | ponteiro **absoluto** (`ABS_X`/`ABS_Y`) + botões + scroll | posição exata, **sem aceleração** e sem deriva |
| `DeskLink Virtual Keyboard` | teclado (`EV_KEY`) | keycodes físicos (evdev), independente de layout |

O GNOME trata os dois como hardware real. Não há portal, screencast, X11 ou permissão a pedir.

Os dispositivos são **criados ao entrar** no Linux e **destruídos ao voltar** — assim, quando você
usa o teclado/mouse físicos do servidor, não existe dispositivo DeskLink nenhum no sistema
(importante para jogos que enumeram teclados, tipo Wine).

### Troca de telas
- Borda configurável (`top` por padrão). Permanecer `SwitchDelayMs` na borda troca o controle.
- A posição na borda é mapeada **proporcionalmente** e o movimento é escalado por
  `resolução_remota / resolução_local` (ex.: 2560/1920 = 1,333).
- Para voltar, é preciso permanecer ~180 ms na borda oposta (evita cortar arrasto).
- `Ctrl+Alt+Espaço` alterna a qualquer momento.

### Rede
- **Descoberta**: o cliente faz broadcast UDP em `47811`; o daemon responde com nome, porta e
  resolução real.
- **Transporte**: TCP `47810` com **TLS**. Certificado autoassinado, com **pinning (TOFU)** na
  primeira conexão — o fingerprint fica salvo em `config.json` e é validado nas seguintes.

### Clipboard
- Texto: `CF_UNICODETEXT` ↔ `text/plain;charset=utf-8`.
- Imagens: PNG ↔ `image/png` (é o formato que o print do GNOME usa).
- **Sob demanda**: o Linux só lê o clipboard quando você **volta para o Windows** (mensagem
  `PullClipboard`) e na conexão. Não há polling — e isso é de propósito: no Wayland, ler a
  seleção acorda o **dono** dela, e um jogo/app pesado pode congelar por um instante a cada
  leitura.
- Se quiser o modo antigo (polling de 1 s), crie `~/.config/desklink/config.json` com
  `{"clipboard": "poll"}`.

---

## Requisitos

| Lado | Requisitos |
|---|---|
| **Windows** | Windows 10 v1809+ ou 11 (x64) |
| **Linux** | GNOME em **Wayland** (testado no Ubuntu 26.04) · Python 3.8+ · `wl-clipboard` · acesso a `/dev/uinput` (grupo `input`, configurado uma vez) |

Rede: as duas máquinas na mesma LAN (ou Tailscale/VPN), portas `47810/tcp` e `47811/udp`.

---

## Instalação

Baixe os instaladores na página de [**Releases**](https://github.com/jhordan-pastorello/trnspositor-tuch-teclado/releases/latest).

### Windows

1. Baixe `DeskLink-Setup-<versão>.exe` e execute (pede permissão de administrador).
2. Pronto: o agente inicia no logon, sem janela. Não precisa instalar o .NET (vem embutido).

### Linux (GNOME/Wayland)

Ubuntu/Debian:

```bash
sudo apt install ./desklink_<versão>_all.deb
```

Fedora/RHEL:

```bash
sudo dnf install ./desklink-<versão>.noarch.rpm
```

O pacote instala o daemon, a regra udev do `/dev/uinput`, carrega o módulo `uinput` e ativa o
serviço `systemd --user`. Dependências (`python3`, `openssl`, `wl-clipboard`) vêm automaticamente.
Se o serviço não subir sozinho: `systemctl --user enable --now desklink`.

Firewall (portas `47810/tcp` e `47811/udp`):

```bash
# firewalld (Fedora)
sudo firewall-cmd --permanent --add-port=47810/tcp --add-port=47811/udp && sudo firewall-cmd --reload
# ufw (Ubuntu)
sudo ufw allow from 192.168.0.0/16 to any port 47810 proto tcp
sudo ufw allow from 192.168.0.0/16 to any port 47811 proto udp
```

### Instalação manual (a partir do código)

```bash
# Linux
./tools/install-linux.sh && sudo ./tools/setup-uinput.sh
```

```powershell
# Windows (PowerShell como Administrador, requer .NET 10 SDK)
.\tools\install-windows.ps1
```

### Publicar uma versão (mantenedores)

```bash
git tag v1.0.0 && git push origin v1.0.0
```

O workflow `release` compila o instalador Windows (Inno Setup), gera `.deb` e `.rpm` (nfpm),
e anexa tudo, com `SHA256SUMS.txt`, à release.

---

## Uso

| Ação | Como |
|---|---|
| Ir para o Linux | encoste no **topo** da tela do notebook e segure ~250 ms |
| Voltar para o Windows | desça até a **borda inferior** do Linux e **empurre ~40 px** além dela (ou `Ctrl+Alt+Espaço`) |
| Alternar a qualquer momento | `Ctrl+Alt+Espaço` |
| Bordas diferentes | `Edge` no `config.json` |

Ao trocar, o cursor do Windows "desaparece" (fica numa janela invisível) e o teclado passa a
digitar no Linux — inclusive acentos, `Ctrl+C/V`, `Alt+Tab` e atalhos do desktop.

### Uso bidirecional (Linux → Windows)

O mesmo link também funciona ao contrário: o **teclado e mouse físicos do Linux** passam a
controlar o Windows. Pelo daemon, que lê os dispositivos físicos via evdev (sem dependências
extras) e captura (`EVIOCGRAB`) só enquanto controla o Windows.

> **Opt-in:** o sentido Linux → Windows vem **desligado**. Para ativar, ponha
> `"InboundEnabled": true` no `config.json` do Windows (e reinicie a tarefa). Sem isso o
> Windows não anuncia a capacidade `inbound` no `Hello`, o daemon nunca captura o teclado/mouse
> do Linux e recusa a sessão. Mesmo ligado, o daemon só captura **depois do `HostingAck`**
> (msg 13) do Windows; sem ack em 500 ms a sessão é cancelada e nada é capturado.

| Ação | Como |
|---|---|
| Ir para o Windows | empurre o mouse contra a **borda inferior** do Linux (~60 unidades além da borda, por `GuestSwitchDelayMs`) |
| Voltar para o Linux | suba até a **borda superior** do Windows e **empurre ~40 px** além dela |
| Voltar a qualquer momento | `Ctrl+Alt+Espaço` (no teclado físico do Linux, ou no Windows) |
| Desligar | `"GuestEnabled": false` no `~/.config/desklink/config.json` do Linux (reinicie o daemon) ou deixe `InboundEnabled` em `false` (padrão) no Windows |

- Os dois sentidos são **mutuamente exclusivos**: com o Windows controlando o Linux, o daemon
  pausa a leitura física; com o Linux controlando o Windows, a borda do Windows não troca.
- Exige acesso de leitura a `/dev/input/event*` (grupo `input`, o mesmo do uinput).
- O Wayland não expõe a posição do cursor, então o daemon a **estima** pelos deltas do mouse.
  Se a estimativa estiver errada logo após iniciar, empurre contra a borda uma vez para
  recalibrar.
- Teclas e botões presos são soltos ao voltar, e a captura só é feita/liberada com tudo solto.
- Segurança: se o link ficar mudo por mais de 10 s durante o controle, o daemon libera a captura
  sozinho (watchdog). A captura é liberada antes de enviar o `Leave`.
- O fluxo Windows → Linux (borda superior do notebook) continua igual.

A margem de 40 px na volta existe de propósito: encostar na borda durante um arrasto não corta
mais a seleção; para voltar é preciso empurrar o ponteiro para fora.

---

## Arquivos

O servidor expõe uma pasta SMB e o instalador mapeia no Windows como **`Z:`**:

```
Z:\                    ↔  /home/storage/Transferencia  (no Linux)
```

Arraste arquivos entre o Explorer e o Nautilus normalmente — é o mesmo diretório, sem cópia
manual e com a velocidade da rede local.

Configuração (no servidor, uma vez):

```bash
sudo ./tools/setup-samba.sh
```

No Windows, para remapear (outro PC, ou após trocar senha):

```powershell
net use Z: \\192.168.1.30\Transferencia /user:storage <senha> /persistent:yes
```

Alternativa sem SMB: o **LocalSend** (instalado nos dois lados) serve para envios pontuais —
abra o app, arraste o arquivo e o outro lado aceita o pareamento.

---

## Configuração

`%LOCALAPPDATA%\DeskLink\config.json` (criado no primeiro start):

| Campo | Padrão | Descrição |
|---|---|---|
| `Edge` | `top` | Borda que leva ao Linux: `top`, `bottom`, `left`, `right` |
| `SwitchDelayMs` | `250` | Tempo parado na borda antes de trocar |
| `Speed` | `1.0` | Multiplicador extra de velocidade (1.0 = proporcional) |
| `Enabled` | `true` | Liga/desliga a troca de telas |
| `ServerHost` | `""` | IP fixo do servidor (vazio = descoberta automática) |
| `ServerPort` | `47810` | Porta TCP |
| `DiscoveryPort` | `47811` | Porta UDP de descoberta |
| `ToggleHotkeyEnabled` | `true` | Habilita `Ctrl+Alt+Espaço` |
| `ServerFingerprint` | `""` | Preenchido sozinho no primeiro contato (TOFU) |
| `InboundEnabled` | `false` | Opt-in: aceita o Linux controlando este Windows (KVM bidirecional). Sem `true`, o Linux nunca captura o teclado/mouse |

No Linux, `~/.config/desklink/config.json` (opcional):

| Campo | Padrão | Descrição |
|---|---|---|
| `GuestEnabled` | `true` | Liga/desliga o controle do Windows pelo teclado/mouse do Linux |
| `GuestEdge` | `bottom` | Borda do Linux que leva ao Windows: `top`, `bottom`, `left`, `right` |
| `GuestSwitchDelayMs` | `250` | Tempo empurrando contra a borda antes de trocar |
| `GuestSpeed` | `1.0` | Multiplicador do movimento do mouse no Windows |

> Suavidade (v0.2.3): o daemon envia deltas (`PointerRel`, msg 14) somados e coalescidos (até ~500 Hz, `TCP_NODELAY`) e escala o movimento pela razão entre as telas. Se o cursor no Windows estiver rápido ou lento demais, ajuste `GuestSpeed` (ex.: `0.8` ou `1.3`).

> Wayland/grab (v0.2.5): em alguns compositores o `EVIOCGRAB` falha com `EINVAL` (visto em mouse com fio). O daemon registra a falha uma vez, não insiste e passa a encaminhar mouse e teclado ao Windows mesmo sem captura; nesse caso o cursor do Linux também se mexe. O daemon lê no máximo 1 mouse (primeiro `eventN`) e 1 teclado (prefere nome Logitech/USB, senão o menor `eventN`). Na entrada, o daemon envia a posição absoluta inicial (msg 3) e o Windows só detecta saída por overflow depois do primeiro posicionamento.

Depois de editar: `Stop-ScheduledTask DeskLink; Start-ScheduledTask DeskLink`.

**Logs**
- Windows: `%LOCALAPPDATA%\DeskLink\desklink.log`
- Linux: `journalctl --user -u desklink -f`

---

## Protocolo

Cada mensagem: `[u8 tipo][u32 tamanho LE][payload]`.

| Tipo | Nome | Payload | Direção |
|---:|---|---|---|
| 1 | `Hello` | JSON `{v,name,w,h,caps}`; `v=2`, `caps:["inbound"]` só se `InboundEnabled` e `Enabled` | W → L |
| 2 | `HelloAck` | JSON `{v,name,w,h}` | L → W |
| 3 | `Pointer` | `i32 x, i32 y` (absoluto, espaço do servidor) | W → L |
| 4 | `Button` | `u8 botão (1=esq,2=meio,3=dir,4=extra), u8 down` | W → L |
| 5 | `Wheel` | `i32 dx, i32 dy` (unidades de 120, igual evdev) | W → L |
| 6 | `Key` | `u16 keycode evdev, u8 down` | W → L |
| 7 | `Leave` | — (libera teclas/botões); em modo guest, JSON `{epoch}` (descartado se for de sessão antiga) devolve o controle, ou recusa um `Hosting` | ambos |
| 8 | `Heartbeat` | — | W → L |
| 9 | `Clipboard` | UTF-8 | ambos |
| 10 | `ClipboardImage` | PNG | ambos |
| 11 | `PullClipboard` | — (pede o clipboard do Linux) | W → L |
| 12 | `Hosting` | JSON `{edge,w,h,epoch}`: o Linux pede para assumir o Windows (KVM bidirecional) | L → W |
| 13 | `HostingAck` | JSON `{epoch}`: o Windows aceitou; só então o Linux captura (`EVIOCGRAB`) | W → L |
| 14 | `PointerRel` | `i32 dx, i32 dy` (pixels do Windows já escalados; modo guest, aplicado com `SendInput MOVE`) | L → W |

No modo guest (veja *Uso bidirecional*), as mensagens 3–6 e 14 também seguem **L → W**:
O Windows só trata 3–6 e 14 dentro de uma sessão iniciada por `Hosting` (12) e aceita (13); fora dela descarta.
`Pointer` em **pixels do Windows** (pode sair da tela até 100 px na borda de saída, para
o Windows medir o empurrão), `Key` com keycodes evdev. O Windows injeta com `SendInput`.

Handshake:

```
cliente                                   servidor
  │ ── TLS (certificado autoassinado) ──►   │
  │ ── Hello {w,h} ─────────────────────►   │
  │ ◄──────────────── HelloAck {w,h} ──    │
  │ ── Pointer/Button/Wheel/Key ────────►   │  → /dev/uinput
  │ ── Clipboard / ClipboardImage ──────►   │  → wl-copy
  │ ◄──────────────── Clipboard(Image) ──   │  ← wl-paste
```

---

## Solução de problemas

| Sintoma | Causa provável / solução |
|---|---|
| "servidor não encontrado" | firewall (47810/47811), sub-redes diferentes ou Wi-Fi com isolamento. Defina `ServerHost` com o IP |
| Conecta e cai a cada ~8 s | versão antiga do daemon/cliente (protocolo `u16`). Atualize os dois |
| Ponteiro não aparece no Linux | `/dev/uinput` sem permissão → `sudo ./tools/setup-uinput.sh` e relogar |
| Ponteiro aparece em canto/posição errada | dispositivo relativo antigo ainda ativo → `systemctl --user restart desklink` |
| Teclado com layout trocado | o layout do Linux deve ser o mesmo do Windows (`pt` nos dois) |
| Colar imagem não funciona | precisa de `wl-clipboard` e o app de destino deve aceitar PNG no clipboard |
| Janelas UAC não recebem input | a tarefa precisa de `RunLevel Highest` (o instalador já configura) |

Diagnóstico rápido:

```bash
# Linux
systemctl --user status desklink
journalctl --user -u desklink -n 50
ls -l /dev/uinput
python3 -c "import os; os.close(os.open('/dev/uinput', os.O_WRONLY)); print('uinput ok')"

# Windows
Get-ScheduledTask -TaskName DeskLink | Select State
Get-Content "$env:LOCALAPPDATA\DeskLink\desklink.log" -Tail 30
```

---

## Limitações

- **Gestos de 3/4 dedos** do touchpad são consumidos pelo Windows antes de qualquer aplicativo —
  não há como encaminhá-los ao Linux. Movimento, clique, scroll e teclado funcionam.
- **Clipboard**: apenas texto e PNG (imagens). O sentido Linux → Windows é capturado **quando
  você volta para o Windows** (ou na conexão) — não há sincronização em tempo real para não
  acordar o app dono da seleção.
- Arrastar/selecionar usa Raw Input (deltas do dispositivo); para voltar ao Windows é preciso
  empurrar ~40 px além da borda.
- Keycodes são físicos (evdev): o layout do Linux deve ser o mesmo do Windows.
- Um cliente por vez (um notebook controlando um servidor).

---

## Desinstalar

**Windows**

```powershell
Unregister-ScheduledTask -TaskName DeskLink -Confirm:$false
Remove-Item "$env:LOCALAPPDATA\DeskLink" -Recurse -Force
```

**Linux**

```bash
systemctl --user disable --now desklink
rm -rf ~/.local/share/desklink ~/.config/systemd/user/desklink.service
sudo rm -f /etc/udev/rules.d/60-desklink-uinput.rules /etc/modules-load.d/desklink-uinput.conf
```

---

## Estrutura do projeto

```
DeskLink/
├─ src/DeskLink.Windows/          # agente Windows (C#/.NET 10, WinExe, sem UI)
│  ├─ Program.cs                  # single instance + message loop
│  ├─ InputRouter.cs              # hooks, borda, estado remoto
│  ├─ RawInputWindow.cs           # WM_INPUT → deltas do dispositivo
│  ├─ HiddenCursor.cs             # janela 2×2 com cursor nulo
│  ├─ LinkClient.cs               # descoberta UDP, TLS, reconexão
│  ├─ ClipboardSync.cs            # texto + PNG (thread STA)
│  ├─ KeyMap.cs                   # scan code set 1 → evdev
│  ├─ Proto.cs                    # framing do protocolo
│  ├─ Config.cs / Log.cs / Native.cs
│  └─ app.manifest                # DPI PerMonitorV2
├─ daemon/desklink_daemon.py      # uinput, TLS, discovery, clipboard
├─ tools/
│  ├─ install-windows.ps1         # publica + tarefa agendada
│  ├─ install-linux.sh            # daemon + systemd --user
│  ├─ setup-uinput.sh             # udev + módulo + grupo (sudo, uma vez)
│  ├─ desklink.service            # unit systemd --user
│  ├─ 60-desklink-uinput.rules    # regra udev
│  └─ desklink-modules.conf       # carrega uinput no boot
└─ README.md
```

### Compilar do zero

```powershell
dotnet publish src/DeskLink.Windows -c Release -o "$env:LOCALAPPDATA\DeskLink\app"
```

```bash
./tools/install-linux.sh
```

---

Feito para uso diário numa mesa com notebook Windows + servidor Linux. Se algo quebrar,
o log dos dois lados diz exatamente o que aconteceu.
