using System.Runtime.InteropServices;
using System.Text.Json;

namespace DeskLink;

internal sealed class InputRouter
{
    private readonly Config _cfg;
    private readonly LinkClient _link;
    private readonly Native.HookProc _mouseProc;
    private readonly Native.HookProc _keyProc;

    private IntPtr _mouseHook;
    private IntPtr _keyHook;
    private HiddenCursor? _hidden;
    private RawInputWindow? _raw;
    private int _screenW;
    private int _screenH;

    private const int ParkX = 8;
    private const int ParkY = 8;

    // _remote/_inbound: leitura sem lock nos hooks (volatile); transicoes sob _inLock.
    private volatile bool _remote;
    private bool _armed = true;
    private int _vx;
    private int _vy;
    private DateTime _edgeSince = DateTime.MinValue;
    private int _overflow;
    private readonly HashSet<int> _keysDown = new();

    // ---- KVM bidirecional: o Linux controla este Windows ----
    // _inLock e o lock de estado (_remote/_inbound). Nunca bloquear nele dentro de hook: use TryEnter.
    private readonly object _inLock = new();
    private volatile bool _inbound;
    private int _inboundEpoch;
    private string _inboundEdge = "top";   // borda de saida do Windows (oposta a do Linux)
    private int _ix;
    private int _iy;
    private int _inOverflow;
    private readonly HashSet<int> _inKeys = new();
    private readonly HashSet<int> _inButtons = new();
    private const int InboundExitOverflow = 40;

    public InputRouter(Config cfg, LinkClient link)
    {
        _cfg = cfg;
        _link = link;
        _mouseProc = MouseHook;
        _keyProc = KeyHook;
    }

    private string Edge => (_cfg.Edge ?? "top").Trim().ToLowerInvariant();

    public void Start()
    {
        _screenW = Native.GetSystemMetrics(Native.SM_CXSCREEN);
        _screenH = Native.GetSystemMetrics(Native.SM_CYSCREEN);
        _hidden = new HiddenCursor(ParkX, ParkY);
        _raw = new RawInputWindow();
        _raw.MouseMoved += OnRawMove;

        _mouseHook = Native.SetWindowsHookEx(Native.WH_MOUSE_LL, _mouseProc, Native.GetModuleHandle(null), 0);
        _keyHook = Native.SetWindowsHookEx(Native.WH_KEYBOARD_LL, _keyProc, Native.GetModuleHandle(null), 0);

        Log.Write($"hooks instalados (mouse={_mouseHook}, teclado={_keyHook}, tela={_screenW}x{_screenH}, borda={Edge})");
    }

    // ------------------------------------------------------------- hooks ----
    private IntPtr MouseHook(int nCode, IntPtr wParam, IntPtr lParam)
    {
        try
        {
            if (nCode >= 0)
            {
                var info = Marshal.PtrToStructure<Native.MSLLHOOKSTRUCT>(lParam);
                if (HandleMouse((int)wParam, info.pt, info.mouseData, info.flags))
                    return (IntPtr)1;
            }
        }
        catch (Exception ex)
        {
            Log.Write("hook mouse: " + ex);
        }
        return Native.CallNextHookEx(_mouseHook, nCode, wParam, lParam);
    }

    private IntPtr KeyHook(int nCode, IntPtr wParam, IntPtr lParam)
    {
        try
        {
            if (nCode >= 0)
            {
                var info = Marshal.PtrToStructure<Native.KBDLLHOOKSTRUCT>(lParam);
                if (HandleKey((int)wParam, info))
                    return (IntPtr)1;
            }
        }
        catch (Exception ex)
        {
            Log.Write("hook teclado: " + ex);
        }
        return Native.CallNextHookEx(_keyHook, nCode, wParam, lParam);
    }

    // ------------------------------------------------------------- mouse ----
    private bool HandleMouse(int msg, Native.POINT pt, int data, uint flags)
    {
        // eventos que nos mesmos injetamos (Linux -> Windows) passam direto
        if (_inbound && (flags & Native.LLMHF_INJECTED) != 0)
            return false;

        if (!_cfg.Enabled || !_link.IsConnected)
        {
            if (_remote) ExitRemote();
            return false;
        }

        switch (msg)
        {
            case Native.WM_MOUSEMOVE:
                return OnMove(pt);

            case Native.WM_LBUTTONDOWN: return SendButton(1, true);
            case Native.WM_LBUTTONUP: return SendButton(1, false);
            case Native.WM_MBUTTONDOWN: return SendButton(2, true);
            case Native.WM_MBUTTONUP: return SendButton(2, false);
            case Native.WM_RBUTTONDOWN: return SendButton(3, true);
            case Native.WM_RBUTTONUP: return SendButton(3, false);
            case Native.WM_XBUTTONDOWN: return SendButton((short)(data >> 16) == 1 ? (byte)4 : (byte)5, true);
            case Native.WM_XBUTTONUP: return SendButton((short)(data >> 16) == 1 ? (byte)4 : (byte)5, false);
            case Native.WM_MOUSEWHEEL: return SendWheel(0, (short)(data >> 16));
            case Native.WM_MOUSEHWHEEL: return SendWheel((short)(data >> 16), 0);
            default:
                return false;
        }
    }

    private bool OnEdge(Native.POINT pt)
    {
        return Edge switch
        {
            "bottom" => pt.Y >= _screenH - 2,
            "left" => pt.X <= 1,
            "right" => pt.X >= _screenW - 2,
            _ => pt.Y <= 1
        };
    }

    private bool OnMove(Native.POINT pt)
    {
        if (!_remote)
        {
            // Linux esta controlando este Windows: nao disputa o controle
            if (_inbound)
            {
                _edgeSince = DateTime.MinValue;
                return false;
            }

            if (OnEdge(pt))
            {
                if (!_armed) return false;
                if (_edgeSince == DateTime.MinValue)
                {
                    _edgeSince = DateTime.UtcNow;
                }
                else if ((DateTime.UtcNow - _edgeSince).TotalMilliseconds >= _cfg.SwitchDelayMs)
                {
                    EnterRemote(pt);
                }
            }
            else
            {
                _edgeSince = DateTime.MinValue;
                _armed = true;
            }
            return false;
        }

        // o movimento remoto vem do Raw Input (deltas do dispositivo);
        // aqui so mantemos o cursor local parado e escondido.
        Native.SetCursorPos(ParkX, ParkY);
        return true;
    }

    private void OnRawMove(int dx, int dy)
    {
        if (!_remote || !_cfg.Enabled || !_link.IsConnected) return;

        int rw = Math.Max(1, _link.RemoteWidth);
        int rh = Math.Max(1, _link.RemoteHeight);

        double sx = (double)rw / Math.Max(1, _screenW) * _cfg.Speed;
        double sy = (double)rh / Math.Max(1, _screenH) * _cfg.Speed;

        int nx = _vx + (int)Math.Round(dx * sx);
        int ny = _vy + (int)Math.Round(dy * sy);

        // quanto o cursor passou da borda de retorno neste evento
        int over = Edge switch
        {
            "bottom" => -ny,
            "left" => nx - (rw - 1),
            "right" => -nx,
            _ => ny - (rh - 1)
        };

        if (over > 0)
        {
            // so volta depois de empurrar ~40px de proposito: arrasto que
            // raspa na borda nao corta mais
            _overflow += over;
            if (_overflow >= 40)
            {
                ExitRemote();
                return;
            }
        }
        else
        {
            _overflow = 0;
        }

        _vx = Math.Clamp(nx, 0, rw - 1);
        _vy = Math.Clamp(ny, 0, rh - 1);
        _link.Send(Proto.PointerMsg(_vx, _vy));
    }

    private bool SendButton(byte btn, bool down)
    {
        if (!_remote) return false;
        _link.Send(Proto.ButtonMsg(btn, down));
        Log.Write($"botao {btn} {(down ? "down" : "up")} em ({_vx},{_vy})");
        return true;
    }

    private bool SendWheel(int dx, int dy)
    {
        if (!_remote) return false;
        _link.Send(Proto.WheelMsg(dx, dy));
        return true;
    }

    // ---------------------------------------------------------- teclado -----
    private bool HandleKey(int msg, Native.KBDLLHOOKSTRUCT k)
    {
        bool down = msg == Native.WM_KEYDOWN || msg == Native.WM_SYSKEYDOWN;

        // teclas injetadas por nos (Linux -> Windows) nao disparam atalho nem roteamento
        if (_inbound && (k.flags & Native.LLKHF_INJECTED) != 0)
            return false;

        if (_cfg.ToggleHotkeyEnabled && k.vkCode == 0x20 && CtrlAltDown())
        {
            if (down)
            {
                // SendInput (soltar teclas) dentro do hook sob lock pode travar: adia para o pool
                if (_inbound) ThreadPool.QueueUserWorkItem(_ => ExitInbound(notifyServer: true, "atalho"));
                else if (_remote) ExitRemote();
                else if (_link.IsConnected) EnterRemote(new Native.POINT { X = _screenW / 2, Y = _screenH / 2 });
            }
            return true;
        }

        if (!_remote || !_link.IsConnected) return false;

        bool extended = (k.flags & 0x01) != 0;
        if (!KeyMap.TryMap(k.scanCode, extended, out int code)) return false;

        // O Windows repete o keydown enquanto a tecla esta pressionada. Repassar
        // cada repeticao reinicia o timer de repeticao do Linux e a tecla "trava".
        // Mandamos so o primeiro press (modificadores sempre passam, para nunca
        // ficar com Shift preso).
        bool modifier = code is 42 or 54 or 29 or 97 or 56 or 100 or 125 or 126;
        if (down)
        {
            if (!modifier && !_keysDown.Add(code)) return true;
            _keysDown.Add(code);
        }
        else
        {
            _keysDown.Remove(code);
        }

        _link.Send(Proto.KeyMsg(code, down));
        return true;
    }

    private static bool CtrlAltDown() =>
        (Native.GetAsyncKeyState(0x11) & 0x8000) != 0 && (Native.GetAsyncKeyState(0x12) & 0x8000) != 0;

    // ------------------------------------------------ entrada do Linux -------
    // Chamado na thread de leitura do LinkClient. Msgs: 12 Hosting (Linux assumiu),
    // 3 Pointer (absoluto em pixels do Windows, pode sair da tela na borda de
    // saida), 4 Button, 5 Wheel, 6 Key (evdev), 7 Leave (Linux devolveu o controle).
    public void OnInbound(byte type, byte[] payload)
    {
        try
        {
            if (!_link.IsConnected) return;

            lock (_inLock)
            {
                if (type == Proto.Hosting)
                {
                    HandleHosting(payload);
                    return;
                }

                // 3-6 so valem dentro de uma sessao aceita (Hosting + ack); frames
                // atrasados de uma sessao encerrada sao descartados.
                if (!_inbound) return;

                switch (type)
                {
                    case Proto.Leave:
                        if (EpochMatches(payload)) ExitInbound(notifyServer: false, "servidor devolveu");
                        break;

                    case Proto.Pointer when payload.Length >= 8:
                        InboundPointer(BitConverter.ToInt32(payload, 0), BitConverter.ToInt32(payload, 4));
                        break;

                    case Proto.Button when payload.Length >= 2:
                        InboundButton(payload[0], payload[1] != 0);
                        break;

                    case Proto.Wheel when payload.Length >= 8:
                        InboundWheel(BitConverter.ToInt32(payload, 0), BitConverter.ToInt32(payload, 4));
                        break;

                    case Proto.Key when payload.Length >= 3:
                        InboundKey(BitConverter.ToUInt16(payload, 0), payload[2] != 0);
                        break;
                }
            }
        }
        catch (Exception ex)
        {
            Log.Write("entrada do servidor: " + ex);
        }
    }

    // Hosting (12): so aqui uma sessao inbound comeca. Recusa com Leave se o
    // inbound esta desligado ou o Windows esta controlando o Linux.
    private void HandleHosting(byte[] payload)
    {
        var (edge, epoch) = ParseHosting(payload);
        if (!_cfg.Enabled || !_cfg.InboundEnabled || _remote)
        {
            Log.Write("Hosting recusado (inbound desligado ou Windows controlando o Linux)");
            _link.Send(Proto.LeaveMsg(epoch));
            return;
        }
        EnterInbound(edge, epoch);
        _link.Send(Proto.HostingAckMsg(epoch));
    }

    // Leave sem epoch (servidor antigo) vale; com epoch so vale o da sessao atual.
    private bool EpochMatches(byte[] payload)
    {
        if (payload.Length == 0) return true;
        try
        {
            using var doc = JsonDocument.Parse(payload);
            if (doc.RootElement.TryGetProperty("epoch", out var e))
                return e.TryGetInt32(out var v) && v == _inboundEpoch;
        }
        catch { }
        return true;
    }

    public void OnLinkLost()
    {
        lock (_inLock)
        {
            if (_inbound) ExitInbound(notifyServer: false, "conexao perdida");
        }
    }

    private static string OppositeEdge(string linuxEdge) => linuxEdge switch
    {
        "top" => "bottom",
        "left" => "right",
        "right" => "left",
        _ => "top"
    };

    // payload: {"edge":"bottom","epoch":N} = borda do Linux por onde ele saiu e epoch da sessao
    private static (string edge, int epoch) ParseHosting(byte[] payload)
    {
        string edge = "top";
        int epoch = 0;
        try
        {
            if (payload.Length > 0)
            {
                using var doc = JsonDocument.Parse(payload);
                if (doc.RootElement.TryGetProperty("edge", out var e))
                    edge = OppositeEdge((e.GetString() ?? "bottom").Trim().ToLowerInvariant());
                if (doc.RootElement.TryGetProperty("epoch", out var ep) && ep.TryGetInt32(out var n))
                    epoch = n;
            }
        }
        catch { }
        return (edge, epoch);
    }

    private void EnterInbound(string exitEdge, int epoch)
    {
        // exclusao mutua: se o Windows esta controlando o Linux, ignora
        if (_remote) return;
        if (_inbound) { _inboundEdge = exitEdge; _inboundEpoch = epoch; return; }
        _inboundEdge = exitEdge;
        _inboundEpoch = epoch;
        _inbound = true;
        _inOverflow = 0;
        _inKeys.Clear();
        _inButtons.Clear();
        _edgeSince = DateTime.MinValue;
        Log.Write($"Linux assumiu o controle (saida pela borda {exitEdge})");
    }

    private void InboundPointer(int x, int y)
    {
        int w = Math.Max(2, _screenW);
        int h = Math.Max(2, _screenH);

        int over = _inboundEdge switch
        {
            "bottom" => y - (h - 1),
            "left" => -x,
            "right" => x - (w - 1),
            _ => -y
        };

        if (over > 0)
        {
            _inOverflow = over;
            if (over >= InboundExitOverflow)
            {
                ExitInbound(notifyServer: true, "empurrou alem da borda");
                return;
            }
        }
        else
        {
            _inOverflow = 0;
        }

        _ix = Math.Clamp(x, 0, w - 1);
        _iy = Math.Clamp(y, 0, h - 1);
        // absoluto normalizado 0..65535 sobre a tela primaria
        int nx = (int)((long)_ix * 65535 / (w - 1));
        int ny = (int)((long)_iy * 65535 / (h - 1));
        var i = new Native.INPUT { type = Native.INPUT_MOUSE };
        i.mi = new Native.MOUSEINPUT
        {
            dx = nx,
            dy = ny,
            dwFlags = Native.MOUSEEVENTF_MOVE | Native.MOUSEEVENTF_ABSOLUTE
        };
        Send(i);
    }

    private static void Send(Native.INPUT input)
    {
        var arr = new[] { input };
        if (Native.SendInput(1, arr, Marshal.SizeOf<Native.INPUT>()) == 0)
            Log.Write("SendInput falhou: " + Marshal.GetLastWin32Error());
    }

    private static void SendMouse(uint flags, uint data = 0)
    {
        var i = new Native.INPUT { type = Native.INPUT_MOUSE };
        i.mi = new Native.MOUSEINPUT { dwFlags = flags, mouseData = data };
        Send(i);
    }

    private void InboundButton(byte btn, bool down)
    {
        uint flag; uint data = 0;
        switch (btn)
        {
            case 1: flag = down ? Native.MOUSEEVENTF_LEFTDOWN : Native.MOUSEEVENTF_LEFTUP; break;
            case 2: flag = down ? Native.MOUSEEVENTF_MIDDLEDOWN : Native.MOUSEEVENTF_MIDDLEUP; break;
            case 3: flag = down ? Native.MOUSEEVENTF_RIGHTDOWN : Native.MOUSEEVENTF_RIGHTUP; break;
            case 4: flag = down ? Native.MOUSEEVENTF_XDOWN : Native.MOUSEEVENTF_XUP; data = 1; break;
            case 5: flag = down ? Native.MOUSEEVENTF_XDOWN : Native.MOUSEEVENTF_XUP; data = 2; break;
            default: return;
        }
        if (down) _inButtons.Add(btn); else _inButtons.Remove(btn);
        SendMouse(flag, data);
    }

    private void InboundWheel(int dx, int dy)
    {
        if (dy != 0) SendMouse(Native.MOUSEEVENTF_WHEEL, unchecked((uint)dy));
        if (dx != 0) SendMouse(Native.MOUSEEVENTF_HWHEEL, unchecked((uint)dx));
    }

    private void InboundKey(int evdev, bool down)
    {
        if (!KeyMap.TryEvdevToScan(evdev, out ushort scan, out bool ext)) return;
        if (down) _inKeys.Add(evdev); else _inKeys.Remove(evdev);
        SendKey(scan, ext, down);
    }

    private static void SendKey(ushort scan, bool ext, bool down)
    {
        uint flags = Native.KEYEVENTF_SCANCODE;
        if (ext) flags |= Native.KEYEVENTF_EXTENDEDKEY;
        if (!down) flags |= Native.KEYEVENTF_KEYUP;
        var i = new Native.INPUT { type = Native.INPUT_KEYBOARD };
        i.ki = new Native.KEYBDINPUT { wScan = scan, dwFlags = flags };
        Send(i);
    }

    // Chamado dentro de _inLock (ou pelo hook, via atalho: pega o lock aqui).
    private void ExitInbound(bool notifyServer, string reason)
    {
        lock (_inLock)
        {
            if (!_inbound) return;
            _inbound = false;
            _inOverflow = 0;

            // solta tudo o que o Linux deixou pressionado
            foreach (var code in _inKeys.ToArray())
                if (KeyMap.TryEvdevToScan(code, out ushort scan, out bool ext)) SendKey(scan, ext, false);
            _inKeys.Clear();
            foreach (var btn in _inButtons.ToArray()) InboundButton(btn, false);
            _inButtons.Clear();

            if (notifyServer && _link.IsConnected)
                _link.Send(Proto.LeaveMsg(_inboundEpoch));

            // afasta o cursor da borda e desarma, senao a borda de entrada
            // (topo) reenviaria o controle ao Linux na hora
            if (reason != "conexao perdida")
            {
                int x = Math.Clamp(_ix, 0, _screenW - 1);
                int y = Math.Clamp(_iy, 0, _screenH - 1);
                (int x, int y) back = _inboundEdge switch
                {
                    "bottom" => (x, _screenH - 3),
                    "left" => (2, y),
                    "right" => (_screenW - 3, y),
                    _ => (x, 3)
                };
                Native.SetCursorPos(back.x, back.y);
            }
            _armed = false;
            _edgeSince = DateTime.MinValue;
            Log.Write("Linux devolveu o controle: " + reason);
        }
    }

    // ------------------------------------------------------------ estado ----
    private void EnterRemote(Native.POINT at)
    {
        // chamado no hook: nunca bloqueia; se o lock esta ocupado tenta no proximo evento
        if (!Monitor.TryEnter(_inLock)) return;
        try { EnterRemoteLocked(at); }
        finally { Monitor.Exit(_inLock); }
    }

    private void EnterRemoteLocked(Native.POINT at)
    {
        if (!_link.IsConnected || _remote || _inbound) return;

        int rw = Math.Max(1, _link.RemoteWidth);
        int rh = Math.Max(1, _link.RemoteHeight);

        // ponto de entrada: mesma proporcao do eixo compartilhado
        double relX = (double)at.X / Math.Max(1, _screenW);
        double relY = (double)at.Y / Math.Max(1, _screenH);

        _remote = true;
        _edgeSince = DateTime.MinValue;
        _overflow = 0;
        _keysDown.Clear();

        switch (Edge)
        {
            case "bottom":
                _vx = Math.Clamp((int)(relX * rw), 0, rw - 1);
                _vy = 0;
                break;
            case "left":
                _vx = rw - 1;
                _vy = Math.Clamp((int)(relY * rh), 0, rh - 1);
                break;
            case "right":
                _vx = 0;
                _vy = Math.Clamp((int)(relY * rh), 0, rh - 1);
                break;
            default: // top
                _vx = Math.Clamp((int)(relX * rw), 0, rw - 1);
                _vy = rh - 1;
                break;
        }

        _hidden?.Show();
        var clip = new Native.RECT { Left = ParkX, Top = ParkY, Right = ParkX + 1, Bottom = ParkY + 1 };
        Native.ClipCursor(ref clip);
        Native.SetCursorPos(ParkX, ParkY);
        _link.Send(Proto.PointerMsg(_vx, _vy));
        Log.Write($"entrou no Linux local=({at.X},{at.Y}) -> remoto=({_vx},{_vy}) borda={Edge}");
    }

    private void ExitRemote()
    {
        if (!_remote) return;
        if (!Monitor.TryEnter(_inLock)) return;
        try { ExitRemoteLocked(); }
        finally { Monitor.Exit(_inLock); }
    }

    private void ExitRemoteLocked()
    {
        if (!_remote) return;
        _remote = false;
        _edgeSince = DateTime.MinValue;
        _overflow = 0;
        _keysDown.Clear();

        _link.Send(Proto.LeaveMsg());
        // puxa o clipboard do Linux agora (foi copiado algo enquanto estavamos la)
        _link.Send(Proto.PullClipboardMsg());
        Native.ClipCursor(IntPtr.Zero);
        _hidden?.Hide();

        int rw = Math.Max(1, _link.RemoteWidth);
        int rh = Math.Max(1, _link.RemoteHeight);

        int lx = Math.Clamp((int)((double)_vx / rw * _screenW), 0, _screenW - 1);
        int ly = Math.Clamp((int)((double)_vy / rh * _screenH), 0, _screenH - 1);

        (int x, int y) back = Edge switch
        {
            "bottom" => (lx, _screenH - 3),
            "left" => (_screenW - 3, ly),
            "right" => (2, ly),
            _ => (lx, 3)
        };

        Native.SetCursorPos(back.x, back.y);
        _armed = false;
        Log.Write($"voltou ao Windows local=({back.x},{back.y}) vindo de=({_vx},{_vy})");
    }
}
