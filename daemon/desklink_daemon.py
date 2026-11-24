#!/usr/bin/env python3
"""
DeskLink daemon (Linux / GNOME Wayland) - injecao via uinput.

Nao usa portal, nao abre dialogo, nao pede permissao: cria um mouse e um
teclado virtuais no kernel (/dev/uinput) e o GNOME os trata como hardware.

Requisitos (uma vez):
  - modulo uinput carregado
  - udev: /dev/uinput com grupo input, 0660
  - usuario no grupo input

Protocolo: [u8 tipo][u32 tamanho LE][payload]
  1 Hello JSON | 2 HelloAck JSON | 3 Pointer i32,i32 | 4 Button u8,u8
  5 Wheel i32,i32 | 6 Key u16,u8 | 7 Leave | 8 Heartbeat | 9 Clipboard UTF-8
  10 ClipboardImage PNG | 11 PullClipboard | 12 Hosting JSON {"edge","w","h","epoch"}
  13 HostingAck JSON {"epoch"} (Windows -> Linux)
  14 PointerRel i32,i32 (dx,dy em pixels do Windows ja escalados; modo guest)

KVM bidirecional (modo "guest"): o daemon tambem le o teclado/mouse FISICOS do
Linux (evdev, sem dependencias) e, ao empurrar o ponteiro alem de GuestEdge,
assume o Windows enviando 12 (Hosting). So depois do 13 (HostingAck, timeout
500 ms) o daemon captura (EVIOCGRAB) e envia 3-6 com coordenadas em pixels do
Windows; sem ack a sessao e cancelada e nada e capturado. Exige Hello v=2 com
caps ["inbound"] (Windows com InboundEnabled). 7 (Leave, com epoch) do Windows
devolve o controle; Ctrl+Alt+Espaco tambem.
Config (~/.config/desklink/config.json): GuestEnabled (true), GuestEdge
("bottom"), GuestSwitchDelayMs (250), GuestSpeed (1.0).
"""
import errno
import fcntl
import glob
import hashlib
import json
import os
import queue
import re
import select
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time

PORT_TCP = 47810
PORT_UDP = 47811
DISCOVER_MAGIC = b"DESKLINK_DISCOVER1"

# Cliente manda Heartbeat (msg 8) a cada ~5s. Silencio prolonged = cliente
# morreu sem fechar o socket (notebook desligou/reiniciou, queda de rede) e o
# daemon ficaria com self.conn apontando pra conexao morta, recusando todo
# mundo novo.
#
# Margem: o cliente idealmente manda a cada ~5s, mas um cliente antigo/ocioso
# pode mandar a cada ~30s (o TryTake do Pump bloqueia 30s). 90s = folga de 3x
# pro pior caso e 18x pro caso nominal. Nao pode ser menor que o intervalo
# real do cliente, senao derruba conexao saudavel.
HEARTBEAT_TIMEOUT = 90.0

CFG_DIR = os.path.expanduser("~/.config/desklink")
CERT = os.path.join(CFG_DIR, "cert.pem")
KEY = os.path.join(CFG_DIR, "key.pem")
SCREEN_FILE = os.path.join(CFG_DIR, "screen.json")
CONF_FILE = os.path.join(CFG_DIR, "config.json")

# ---- uinput / input-event-codes ----
EV_SYN, EV_KEY, EV_REL, EV_ABS, EV_REP = 0x00, 0x01, 0x02, 0x03, 0x14
SYN_REPORT = 0
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL = 0x00, 0x01, 0x06, 0x08
ABS_X, ABS_Y = 0x00, 0x01
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, BTN_EXTRA = 0x110, 0x111, 0x112, 0x113, 0x114
BTN_MAP = {1: BTN_LEFT, 2: BTN_MIDDLE, 3: BTN_RIGHT, 4: BTN_SIDE, 5: BTN_EXTRA}

UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_SET_ABSBIT = 0x40045567
UI_ABS_SETUP = 0x401C5504
UI_DEV_SETUP = 0x405C5503

ABS_MAX = 32767
BUS_USB = 0x03


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, file=sys.stderr, flush=True)


class UinputDevice:
    def __init__(self, name, keys=(), rels=(), abs_axes=(), repeat=False):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_SYN)
        if repeat:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REP)
        if rels:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)
            for r in rels:
                fcntl.ioctl(self.fd, UI_SET_RELBIT, r)
        if abs_axes:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
            for ax in abs_axes:
                fcntl.ioctl(self.fd, UI_SET_ABSBIT, ax)
                info = struct.pack("iiiiii", 0, 0, ABS_MAX, 0, 0, 0)
                fcntl.ioctl(self.fd, UI_ABS_SETUP, struct.pack("HH", ax, 0) + info)
        for k in keys:
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, k)

        setup = struct.pack("HHHH", BUS_USB, 0x1D6B, 0x0105, 1) \
            + name.encode()[:79].ljust(80, b"\0") + struct.pack("I", 0)
        fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        self._ev = struct.Struct("llHHi")

    def emit(self, etype, code, value):
        os.write(self.fd, self._ev.pack(0, 0, etype, code, value))

    def syn(self):
        self.emit(EV_SYN, SYN_REPORT, 0)

    def close(self):
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except Exception:
            pass
        try:
            os.close(self.fd)
        except Exception:
            pass


class Mouse(UinputDevice):
    """Ponteiro absoluto (estilo usb-tablet): sem aceleracao, posicao exata."""

    def __init__(self):
        super().__init__("DeskLink Virtual Tablet",
                         keys=(BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, BTN_EXTRA),
                         rels=(REL_WHEEL, REL_HWHEEL),
                         abs_axes=(ABS_X, ABS_Y))

    def move_abs(self, x, y, w, h):
        ax = int(x * ABS_MAX / max(1, w - 1))
        ay = int(y * ABS_MAX / max(1, h - 1))
        self.emit(EV_ABS, ABS_X, ax)
        self.emit(EV_ABS, ABS_Y, ay)
        self.syn()

    def button(self, btn, down):
        self.emit(EV_KEY, BTN_MAP.get(btn, BTN_LEFT), 1 if down else 0)
        self.syn()

    def wheel(self, dx, dy):
        if dx:
            self.emit(EV_REL, REL_HWHEEL, int(round(dx / 120.0)) or (1 if dx > 0 else -1))
        if dy:
            self.emit(EV_REL, REL_WHEEL, int(round(dy / 120.0)) or (1 if dy > 0 else -1))
        self.syn()


class Keyboard(UinputDevice):
    def __init__(self):
        codes = list(range(1, 128)) + list(range(0x160, 0x180))
        super().__init__("DeskLink Virtual Keyboard", keys=codes, repeat=True)

    def key(self, code, down):
        self.emit(EV_KEY, int(code), 1 if down else 0)
        self.syn()


# ------------------------------------------------------------------ config --


def load_json(path, key=None):
    try:
        with open(path, "r") as f:
            d = json.load(f)
            return d.get(key) if key else d
    except Exception:
        return None


def save_json(path, **data):
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log("falha ao salvar", path, e)


def detect_screen():
    """Resolucao atual: melhor modo do DRM, senao cache, senao 1920x1080."""
    try:
        for base in sorted(os.listdir("/sys/class/drm")):
            if "-" not in base:
                continue
            modes = "/sys/class/drm/%s/modes" % base
            status = "/sys/class/drm/%s/status" % base
            try:
                if open(status).read().strip() != "connected":
                    continue
                line = open(modes).readline().strip()
                if "x" in line:
                    w, h = line.split("x")
                    return int(w), int(h)
            except Exception:
                continue
    except Exception:
        pass
    c = load_json(SCREEN_FILE)
    if c and c.get("w"):
        return c["w"], c["h"]
    return 1920, 1080


# ------------------------------------------------------------------ TLS -----


def ensure_cert():
    os.makedirs(CFG_DIR, exist_ok=True)
    if not (os.path.exists(CERT) and os.path.exists(KEY)):
        log("gerando certificado TLS...")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", KEY, "-out", CERT, "-days", "3650",
                        "-subj", "/CN=desklink"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.chmod(KEY, 0o600)


def fingerprint():
    out = subprocess.run(["openssl", "x509", "-in", CERT, "-noout", "-fingerprint", "-sha256"],
                         capture_output=True, text=True, check=True).stdout
    return out.strip().split("=", 1)[1].replace(":", "").upper()


# -------------------------------------------------------------- clipboard ---

_wl_lock = threading.Lock()
_wl_proc = [None]


def set_clipboard(text):
    _wl_copy(("wl-copy", "--type", "text/plain;charset=utf-8"), text.encode("utf-8", "replace"))


def set_clipboard_image(png):
    _wl_copy(("wl-copy", "--type", "image/png"), png)


def _wl_copy(cmd, data):
    with _wl_lock:
        old = _wl_proc[0]
        if old is not None and old.poll() is None:
            try:
                old.kill()
            except Exception:
                pass
        try:
            p = subprocess.Popen(list(cmd), stdin=subprocess.PIPE)
            p.stdin.write(data)
            p.stdin.close()
            _wl_proc[0] = p
        except Exception as e:
            log("wl-copy falhou:", e)


def clipboard_types():
    try:
        out = subprocess.run(["wl-paste", "--list-types"], capture_output=True, timeout=2)
        if out.returncode == 0:
            return out.stdout.decode("utf-8", "replace").split()
    except Exception:
        pass
    return []


def read_clipboard():
    """Retorna ('text', str) ou ('image', png_bytes) ou None."""
    types = clipboard_types()
    if not types:
        return None

    if "image/png" in types:
        try:
            out = subprocess.run(["wl-paste", "--type", "image/png"], capture_output=True, timeout=5)
            if out.returncode == 0 and out.stdout:
                return ("image", out.stdout)
        except Exception:
            pass

    try:
        out = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, timeout=3)
        if out.returncode == 0:
            return ("text", out.stdout.decode("utf-8", "replace"))
    except Exception:
        pass
    return None


# ------------------------------------------------------- guest (evdev) -----

EVIOCGRAB = 0x40044590
EVIOCGNAME = 0x81004506          # _IOR('E', 0x06, char[256])
KEY_A, KEY_ENTER, KEY_SPACE = 30, 28, 57
KEY_CTRL = (29, 97)
KEY_ALT = (56, 100)
REL_WHEEL_CODE, REL_HWHEEL_CODE = REL_WHEEL, REL_HWHEEL
BTN_REV = {v: k for k, v in BTN_MAP.items()}
GUEST_PUSH_PX = 60       # empurrao minimo alem da borda (unidades brutas do mouse)
GUEST_OVERSHOOT = 100    # quanto o ponteiro virtual pode passar da borda de saida
UNGRAB_TIMEOUT = 2.0
WRITE_TIMEOUT = 5.0
ACK_TIMEOUT = 0.5        # espera pelo HostingAck antes de capturar
GUEST_RX_WATCHDOG = 10.0  # sem nada do Windows por tanto tempo = devolve o controle
OPPOSITE = {"top": "bottom", "bottom": "top", "left": "right", "right": "left"}


def _eviocgbit(ev, ln):
    return 0x80000000 | (ln << 16) | 0x4500 | (0x20 + ev)


def _bit(buf, n):
    i = n >> 3
    return i < len(buf) and bool(buf[i] >> (n & 7) & 1)


class EvDev:
    def __init__(self, path, fd, kind, name=""):
        self.path = path
        self.fd = fd
        self.name = name
        self.kind = kind          # "mouse" | "keyboard"
        self.pressed = set()
        self.grabbed = False
        self.grab_ok = True       # False: EVIOCGRAB falhou (ex.: EINVAL no Wayland); nao insiste
        self.grab_skip = False
        self.dx = self.dy = self.wx = self.wy = 0

    def grab(self, on):
        try:
            fcntl.ioctl(self.fd, EVIOCGRAB, 1 if on else 0)
            self.grabbed = on
        except OSError as e:
            if on:
                if not self.grab_skip:
                    log("EVIOCGRAB falhou em", self.path, e, "(segue sem captura)")
                self.grab_ok = False
                self.grab_skip = True
            else:
                self.grabbed = False

    def close(self):
        try:
            os.close(self.fd)   # fechar o fd tambem solta o grab
        except OSError:
            pass


def classify_evdev(path):
    """Abre o device se for mouse/teclado fisico; senao devolve None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        name = bytearray(256)
        fcntl.ioctl(fd, EVIOCGNAME, name)
        name = bytes(name).split(b"\0", 1)[0].decode("utf-8", "replace")
        if name.startswith("DeskLink"):
            os.close(fd)
            return None
        types = bytearray(8)
        fcntl.ioctl(fd, _eviocgbit(0, 8), types)
        keys = bytearray(96)
        if _bit(types, EV_KEY):
            fcntl.ioctl(fd, _eviocgbit(EV_KEY, 96), keys)
        rels = bytearray(8)
        if _bit(types, EV_REL):
            fcntl.ioctl(fd, _eviocgbit(EV_REL, 8), rels)
        if _bit(rels, REL_X) and _bit(rels, REL_Y) and _bit(keys, BTN_LEFT):
            return EvDev(path, fd, "mouse", name)
        if _bit(keys, KEY_A) and _bit(keys, KEY_ENTER) and _bit(keys, KEY_SPACE):
            return EvDev(path, fd, "keyboard", name)
    except OSError:
        pass
    os.close(fd)
    return None


class GuestInput(threading.Thread):
    """Le mouse/teclado fisicos e, no modo guest, os encaminha ao Windows."""

    def __init__(self, d, edge, delay_ms, speed):
        super().__init__(name="guest", daemon=True)
        self.d = d
        self.edge = edge
        self.delay = delay_ms / 1000.0
        self.speed = speed
        self.devs = {}            # path -> EvDev
        self.guest = False
        self.pending = False      # Hosting enviado, aguardando HostingAck
        self.pending_deadline = 0.0
        self.epoch = 0
        self.armed = True
        self.ungrab_deadline = 0.0
        self.push = 0
        self.push_since = None
        self.est_x = d.remote_w // 2   # posicao estimada do cursor do Linux
        self.est_y = d.remote_h // 2
        self.gx = self.gy = 0.0        # ponteiro virtual, em pixels do Windows
        self.lock = threading.RLock()

    # ---- estado ----
    def note_abs(self, x, y):
        """Windows controlou o Linux: sabemos onde o cursor ficou."""
        self.est_x, self.est_y = x, y

    def _over(self, nx, ny):
        w, h = self.d.remote_w, self.d.remote_h
        if self.edge == "bottom":
            return ny - (h - 1)
        if self.edge == "left":
            return -nx
        if self.edge == "right":
            return nx - (w - 1)
        return -ny

    def enter(self):
        d = self.d
        cw, ch = d.client_w, d.client_h
        if d.conn is None or not cw or not ch or d.active or not d.inbound_cap:
            return
        w, h = d.remote_w, d.remote_h
        fx = self.est_x / max(1, w - 1)
        fy = self.est_y / max(1, h - 1)
        if self.edge == "bottom":
            self.gx, self.gy = fx * (cw - 1), 0
        elif self.edge == "top":
            self.gx, self.gy = fx * (cw - 1), ch - 1
        elif self.edge == "left":
            self.gx, self.gy = cw - 1, fy * (ch - 1)
        else:
            self.gx, self.gy = 0, fy * (ch - 1)
        with self.lock:
            if self.guest or self.pending:
                return
            self.epoch += 1
            self.pending = True
            self.pending_deadline = time.time() + ACK_TIMEOUT
            self.push, self.push_since = 0, None
            epoch = self.epoch
        # nao captura nada ainda: so apos o HostingAck (on_ack)
        d.send(12, json.dumps({"edge": self.edge, "w": w, "h": h, "epoch": epoch}).encode())
        log("guest: Hosting enviado (epoch %d), aguardando ack" % epoch)

    def on_ack(self, epoch):
        """Windows aceitou: agora sim assume o controle (captura vem no apply_grab)."""
        with self.lock:
            if not self.pending or epoch != self.epoch:
                return
            self.pending = False
            self.guest = True
        # posicao absoluta inicial: alinha o cursor do Windows na entrada
        self.d.send(3, struct.pack("<ii", int(self.gx), int(self.gy)))
        log("guest: controlando o Windows (borda %s)" % self.edge)

    def cancel_pending(self, reason, notify):
        with self.lock:
            if not self.pending:
                return
            self.pending = False
            self.armed = False
            epoch = self.epoch
        if notify:
            self.d.send(7, json.dumps({"epoch": epoch}).encode())
        log("guest: sessao cancelada (%s)" % reason)

    def on_remote_leave(self, epoch):
        """Leave do Windows. Devolve True se pertencia a sessao guest (atual ou pendente)."""
        with self.lock:
            if epoch is not None and epoch != self.epoch:
                return False          # Leave de sessao antiga
            pending, guest = self.pending, self.guest
        if pending:
            self.cancel_pending("Windows recusou", False)
            return True
        if guest:
            self.exit(False, "Windows devolveu o controle")
            return True
        return epoch is None

    def exit(self, notify, reason):
        with self.lock:
            if not self.guest:
                return
            self.guest = False
            self.armed = False
            self.push, self.push_since = 0, None
            self.ungrab_deadline = time.time() + UNGRAB_TIMEOUT
            epoch = self.epoch
            # libera a captura ANTES de avisar o Windows
            for dev in list(self.devs.values()):
                if dev.grabbed:
                    dev.grab(False)
        if notify:
            self.d.send(7, json.dumps({"epoch": epoch}).encode())
        # o cursor do Linux ficou parado na borda por onde saimos
        w, h = self.d.remote_w, self.d.remote_h
        if self.edge in ("bottom", "top"):
            self.est_y = h - 1 if self.edge == "bottom" else 0
        else:
            self.est_x = w - 1 if self.edge == "right" else 0
        log("guest: de volta ao Linux (%s)" % reason)

    # ---- dispositivos ----
    def scan(self):
        def evnum(p):
            m = re.search(r"(\d+)$", p)
            return int(m.group(1)) if m else 1 << 30

        have = {k: any(x.kind == k for x in self.devs.values()) for k in ("mouse", "keyboard")}
        found = {"mouse": [], "keyboard": []}
        for path in sorted(glob.glob("/dev/input/event*"), key=evnum):
            if path in self.devs:
                continue
            dev = classify_evdev(path)
            if not dev:
                continue
            if have[dev.kind]:
                dev.close()      # no maximo 1 mouse e 1 teclado
                continue
            found[dev.kind].append(dev)
        for kind, devs in found.items():
            if not devs:
                continue
            if kind == "keyboard":
                # prefere Logitech/USB; senao o menor eventN (ja ordenado)
                devs.sort(key=lambda x: 0 if re.search(r"logitech|usb", x.name, re.I) else 1)
            dev = devs[0]
            for x in devs[1:]:
                x.close()
            with self.lock:
                self.devs[dev.path] = dev
            log("guest: lendo %s (%s)" % (dev.path, dev.kind))

    def drop(self, dev):
        with self.lock:
            self.devs.pop(dev.path, None)
        dev.close()

    def close_all(self):
        for dev in list(self.devs.values()):
            self.drop(dev)
        with self.lock:
            self.guest = False
            self.pending = False

    def apply_grab(self):
        now = time.time()
        for dev in list(self.devs.values()):
            if self.guest:
                # so captura com tudo solto: soltar tecla "no escuro" deixaria
                # a tecla presa no compositor
                if not dev.grabbed and not dev.grab_skip and not dev.pressed:
                    dev.grab(True)
            elif dev.grabbed and (not dev.pressed or now >= self.ungrab_deadline):
                dev.grab(False)

    # ---- loop ----
    def run(self):
        last_scan = 0.0
        while True:
            try:
                d = self.d
                if d.conn is None:
                    if self.devs:
                        self.close_all()
                    time.sleep(0.5)
                    continue
                now = time.time()
                if now - last_scan > 5.0:
                    self.scan()
                    last_scan = now
                if self.guest and (d.active or d.conn is None):
                    self.exit(False, "Windows assumiu o Linux")
                if self.pending and (d.active or d.conn is None):
                    self.cancel_pending("conexao/uso do Linux", False)
                elif self.pending and now >= self.pending_deadline:
                    # sem ack: nao captura; avisa o Windows para largar a sessao
                    self.cancel_pending("timeout do HostingAck", True)
                if self.guest and now - d.last_rx > GUEST_RX_WATCHDOG:
                    self.exit(True, "watchdog: Windows sem resposta")
                self.apply_grab()
                if not self.devs:
                    time.sleep(0.5)
                    continue
                by_fd = {dev.fd: dev for dev in list(self.devs.values())}
                r, _, _ = select.select(list(by_fd), [], [], 0.01 if self.guest else 0.1)
                for fd in r:
                    dev = by_fd[fd]
                    try:
                        data = os.read(fd, 24 * 64)
                    except BlockingIOError:
                        continue
                    except OSError as e:
                        if e.errno != errno.EAGAIN:
                            self.drop(dev)
                        continue
                    if not data:
                        self.drop(dev)
                        continue
                    for off in range(0, len(data) - 23, 24):
                        _, _, et, code, val = struct.unpack_from("llHHi", data, off)
                        self.on_event(dev, et, code, val)
            except Exception as e:
                log("guest: erro:", e)
                time.sleep(0.5)

    def _all_pressed(self):
        s = set()
        for dev in list(self.devs.values()):
            s |= dev.pressed
        return s

    def on_event(self, dev, et, code, val):
        d = self.d
        if et == EV_KEY:
            if val == 1:
                dev.pressed.add(code)
            elif val == 0:
                dev.pressed.discard(code)
            if not self.guest:
                return
            if val == 1 and code == KEY_SPACE:
                p = self._all_pressed()
                if any(k in p for k in KEY_CTRL) and any(k in p for k in KEY_ALT):
                    self.exit(True, "Ctrl+Alt+Espaco")
                    return
            if not dev.grabbed and dev.grab_ok:
                return        # captura ainda pendente (teclas pressionadas); sem grab_ok encaminha mesmo assim
            if code in BTN_REV:
                d.send(4, struct.pack("<BB", BTN_REV[code], 1 if val else 0))
            elif code < 0x100:
                # val 2 (repeticao) vira novo keydown: o Windows repete assim
                d.send(6, struct.pack("<HB", code, 1 if val else 0))
        elif et == EV_REL and dev.kind == "mouse":
            if code == REL_X:
                dev.dx += val
            elif code == REL_Y:
                dev.dy += val
            elif code == REL_WHEEL_CODE:
                dev.wy += val
            elif code == REL_HWHEEL_CODE:
                dev.wx += val
        elif et == EV_SYN and code == SYN_REPORT and dev.kind == "mouse":
            dx, dy, wx, wy = dev.dx, dev.dy, dev.wx, dev.wy
            dev.dx = dev.dy = dev.wx = dev.wy = 0
            if d.active:
                return        # Windows controla o Linux: pausa
            if self.guest:
                if wx or wy:
                    d.send(5, struct.pack("<ii", wx * 120, wy * 120))
                if dx or dy:
                    self.guest_move(dx, dy)
            elif dx or dy:
                self.local_move(dx, dy)

    def local_move(self, dx, dy):
        w, h = self.d.remote_w, self.d.remote_h
        nx, ny = self.est_x + dx, self.est_y + dy
        over = self._over(nx, ny)
        self.est_x = max(0, min(w - 1, nx))
        self.est_y = max(0, min(h - 1, ny))
        now = time.time()
        if over > 0:
            if not self.armed:
                return
            self.push += over
            if self.push_since is None:
                self.push_since = now
            if self.push >= GUEST_PUSH_PX and now - self.push_since >= self.delay:
                self.enter()
        elif over < 0:
            self.push, self.push_since = 0, None
            self.armed = True

    def guest_move(self, dx, dy):
        cw, ch = self.d.client_w, self.d.client_h
        opp = OPPOSITE[self.edge]
        # mesma escala do sentido Windows->Linux (OnRawMove): tela do Linux -> tela do Windows
        sx = cw / max(1, self.d.remote_w) * self.speed
        sy = ch / max(1, self.d.remote_h) * self.speed
        nx = self.gx + dx * sx
        ny = self.gy + dy * sy
        xlo = -GUEST_OVERSHOOT if opp == "left" else 0
        xhi = cw - 1 + (GUEST_OVERSHOOT if opp == "right" else 0)
        ylo = -GUEST_OVERSHOOT if opp == "top" else 0
        yhi = ch - 1 + (GUEST_OVERSHOOT if opp == "bottom" else 0)
        ox, oy = self.gx, self.gy
        self.gx = max(xlo, min(xhi, nx))
        self.gy = max(ylo, min(yhi, ny))
        # delta inteiro em pixels do Windows; o resto fracionario fica em gx/gy
        rx = int(self.gx) - int(ox)
        ry = int(self.gy) - int(oy)
        if rx or ry:
            self.d.send(14, struct.pack("<ii", rx, ry))

PTR_MIN_INTERVAL = 0.008   # Pointer absoluto enviado no maximo a ~120 Hz
REL_MIN_INTERVAL = 0.001   # PointerRel enviado no maximo a ~1000 Hz


# ------------------------------------------------------------------ daemon --

_REL_MARK = object()   # marcador de PointerRel na fila de envio


class Daemon:
    def __init__(self):
        self.conn = None
        self.conn_lost = False
        self.buf = b""
        self.last_rx = time.time()
        self.last_clip_key = None
        self._send_lock = threading.Lock()
        self._txq = queue.Queue(maxsize=4096)
        self._ptr_lock = threading.Lock()
        self._ptr_pending = None      # (conn, x, y): so a ultima posicao importa
        self._ptr_queued = False      # ja existe marcador de Pointer na fila
        self._ptr_last = 0.0
        self._rel_pending = None      # (conn, dx, dy): deltas somados
        self._rel_queued = False
        self._rel_last = 0.0
        threading.Thread(target=self._writer_loop, daemon=True, name="writer").start()
        self.remote_w, self.remote_h = detect_screen()
        save_json(SCREEN_FILE, w=self.remote_w, h=self.remote_h)

        # Dispositivos criados SOB DEMANDA (ao entrar no Linux) e destruidos ao
        # voltar: com eles sempre presentes, jogos via Wine enumeram um teclado
        # extra e o teclado fisico "buga" ao segurar tecla.
        self.mouse = None
        self.keyboard = None
        log("tela %dx%d; uinput sera criado ao entrar no Linux" % (self.remote_w, self.remote_h))

        self.client_w = self.client_h = None
        self.inbound_cap = False
        self.guest_input = None
        self.active = False
        self.pressed_keys = set()
        self.pressed_buttons = set()

    def ensure_devices(self):
        if self.mouse is None:
            self.mouse = Mouse()
            self.keyboard = Keyboard()
            log("uinput: dispositivos criados")

    def destroy_devices(self):
        if self.mouse is not None:
            self.mouse.close()
            self.mouse = None
        if self.keyboard is not None:
            self.keyboard.close()
            self.keyboard = None
            log("uinput: dispositivos removidos")

    # ---- entrada ----
    def move(self, x, y):
        self.ensure_devices()
        self.active = True
        if self.guest_input is not None:
            self.guest_input.note_abs(x, y)
        self.mouse.move_abs(x, y, self.remote_w, self.remote_h)

    def button(self, btn, down):
        self.ensure_devices()
        self.mouse.button(btn, down)
        (self.pressed_buttons.add(btn) if down else self.pressed_buttons.discard(btn))

    def key(self, code, down):
        self.ensure_devices()
        self.keyboard.key(code, down)
        (self.pressed_keys.add(code) if down else self.pressed_keys.discard(code))

    def leave(self):
        if self.keyboard is not None:
            for k in list(self.pressed_keys):
                self.keyboard.key(k, 0)
        if self.mouse is not None:
            for b in list(self.pressed_buttons):
                self.mouse.button(b, False)
        self.pressed_keys.clear()
        self.pressed_buttons.clear()
        self.active = False
        self.destroy_devices()

    # ---- rede ----
    def send(self, msg_type, payload=b""):
        """Enfileira o frame; quem escreve no socket e a thread 'writer'.
        Nunca bloqueia o chamador (guest/evdev). Fila cheia: descarta Pointer."""
        conn = self.conn
        if conn is None:
            return
        if msg_type == 3 and len(payload) == 8:
            # coalescencia "latest wins": no maximo um marcador de Pointer na fila
            x, y = struct.unpack("<ii", payload)
            with self._ptr_lock:
                self._ptr_pending = (conn, x, y)
                if self._ptr_queued:
                    return
                self._ptr_queued = True
            try:
                self._txq.put_nowait((conn, None))
            except queue.Full:
                with self._ptr_lock:
                    self._ptr_queued = False
            return
        if msg_type == 14 and len(payload) == 8:
            # coalescencia por soma: nenhum delta se perde
            dx, dy = struct.unpack("<ii", payload)
            with self._ptr_lock:
                p = self._rel_pending
                if p is not None and p[0] is conn:
                    dx += p[1]
                    dy += p[2]
                self._rel_pending = (conn, dx, dy)
                if self._rel_queued:
                    return
                self._rel_queued = True
            try:
                self._txq.put_nowait((conn, _REL_MARK))
            except queue.Full:
                with self._ptr_lock:
                    self._rel_queued = False
            return
        frame = struct.pack("<BI", msg_type, len(payload)) + payload
        item = (conn, frame)   # amarrado a conexao: nao vaza para a proxima
        try:
            self._txq.put_nowait(item)
        except queue.Full:
            if msg_type != 3:
                try:
                    self._txq.put(item, timeout=0.2)
                except queue.Full:
                    log("fila de envio cheia, frame tipo %d perdido" % msg_type)

    def _writer_loop(self):
        while True:
            conn, frame = self._txq.get()
            if frame is _REL_MARK:
                wait = REL_MIN_INTERVAL - (time.monotonic() - self._rel_last)
                if wait > 0:
                    time.sleep(wait)
                with self._ptr_lock:
                    pend = self._rel_pending
                    self._rel_pending = None
                    self._rel_queued = False
                if pend is None:
                    continue
                conn, dx, dy = pend
                payload = struct.pack("<ii", dx, dy)
                frame = struct.pack("<BI", 14, len(payload)) + payload
                self._rel_last = time.monotonic()
            elif frame is None:
                # marcador de Pointer: limita a ~120 Hz e envia so a posicao mais recente
                wait = PTR_MIN_INTERVAL - (time.monotonic() - self._ptr_last)
                if wait > 0:
                    time.sleep(wait)
                with self._ptr_lock:
                    pend = self._ptr_pending
                    self._ptr_pending = None
                    self._ptr_queued = False
                if pend is None:
                    continue
                conn, x, y = pend
                payload = struct.pack("<ii", x, y)
                frame = struct.pack("<BI", 3, len(payload)) + payload
                self._ptr_last = time.monotonic()
            if conn is None or conn is not self.conn:
                continue      # conexao mudou/caiu: descarta
            try:
                with self._send_lock:
                    conn.settimeout(WRITE_TIMEOUT)
                    conn.sendall(frame)
            except (OSError, ssl.SSLError) as e:
                log("escrita falhou/timeout, derrubando conexao:", e)
                self._drop_conn(conn)

    def _drop_conn(self, conn):
        if self.conn is conn:
            self.conn = None
            self.conn_lost = True   # tcp_loop faz leave()+guest_reset()
        try:
            conn.close()
        except Exception:
            pass

    def _clear_txq(self):
        with self._ptr_lock:
            self._ptr_pending = None
            self._ptr_queued = False
            self._rel_pending = None
            self._rel_queued = False
        try:
            while True:
                self._txq.get_nowait()
        except queue.Empty:
            pass

    def handle(self, msg_type, payload):
        if msg_type == 9:
            text = payload.decode("utf-8", "replace")
            self.last_clip_key = "text:" + hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
            set_clipboard(text)
            log("clipboard <- windows (%d chars)" % len(text))
        elif msg_type == 10:
            self.last_clip_key = "image:" + hashlib.sha1(payload).hexdigest()
            set_clipboard_image(payload)
            log("clipboard <- windows (imagem %d bytes)" % len(payload))
        if msg_type == 11:  # PullClipboard: cliente voltou ao Windows
            threading.Thread(target=self.sync_clipboard, daemon=True).start()
        elif msg_type == 1:
            info = json.loads(payload)
            log("cliente:", info.get("name"), info.get("w"), "x", info.get("h"))
            self.client_w, self.client_h = info.get("w"), info.get("h")
            caps = info.get("caps") or []
            self.inbound_cap = info.get("v", 1) >= 2 and "inbound" in caps
            log("inbound (Linux controlar Windows):", "permitido" if self.inbound_cap else "nao permitido")
            self.send(2, json.dumps({"v": 2, "name": socket.gethostname(),
                                     "w": self.remote_w, "h": self.remote_h}).encode())
            threading.Thread(target=self.sync_clipboard, daemon=True).start()
        elif msg_type == 3:
            x, y = struct.unpack("<ii", payload)
            self.move(max(0, min(self.remote_w - 1, x)), max(0, min(self.remote_h - 1, y)))
        elif msg_type == 4:
            btn, down = struct.unpack("<BB", payload)
            self.button(btn, bool(down))
        elif msg_type == 5:
            dx, dy = struct.unpack("<ii", payload)
            self.ensure_devices()
            self.mouse.wheel(dx, dy)
        elif msg_type == 6:
            code, down = struct.unpack("<HB", payload)
            self.key(code, bool(down))
        elif msg_type == 13:
            try:
                epoch = json.loads(payload).get("epoch")
            except Exception:
                return
            if self.guest_input is not None:
                self.guest_input.on_ack(epoch)
        elif msg_type == 7:
            # Windows devolveu o controle (se estavamos no modo guest) ou
            # saiu do Linux (modo normal). Leave com epoch so pertence ao guest.
            epoch = None
            if payload:
                try:
                    epoch = json.loads(payload).get("epoch")
                except Exception:
                    epoch = None
            if self.guest_input is not None and epoch is not None:
                self.guest_input.on_remote_leave(epoch)
                return
            if self.guest_input is not None and self.guest_input.guest:
                self.guest_input.exit(False, "Windows devolveu o controle")
            self.leave()

    def guest_reset(self):
        self.client_w = self.client_h = None
        self.inbound_cap = False
        if self.guest_input is not None:
            self.guest_input.exit(False, "conexao perdida")
            self.guest_input.cancel_pending("conexao perdida", False)

    def tcp_loop(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT, KEY)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", PORT_TCP))
        srv.listen(2)
        srv.settimeout(0.5)
        log("ouvindo em tcp/%d e udp/%d" % (PORT_TCP, PORT_UDP))

        while True:
            if self.conn is None:
                if self.conn_lost:
                    self.conn_lost = False
                    log("conexao perdida (writer), limpando estado")
                    self.leave()
                    self.guest_reset()
                try:
                    raw, addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError as e:
                    log("accept falhou:", e)
                    time.sleep(0.2)
                    continue
                try:
                    raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    tls = ctx.wrap_socket(raw, server_side=True)
                    self.leave()
                    self.guest_reset()
                    self.conn_lost = False
                    self.conn = tls
                    self.buf = b""
                    self._clear_txq()
                    self.last_rx = time.time()
                    log("conectado:", addr[0])
                except (ssl.SSLError, OSError) as e:
                    log("TLS falhou:", e)
                    try:
                        raw.close()
                    except Exception:
                        pass
                    self.conn = None
                continue

            # Cliente morto: o socket fica meio-aberto (notebook desligou ou a
            # rede caiu) e o select() nao devolve EOF nunca, entao o daemon
            # trava em self.conn e nao aceita mais ninguem. O cliente manda
            # Heartbeat (msg 8) a cada ~5s, entao silencio = cliente morto.
            silencio = time.time() - self.last_rx
            if silencio > HEARTBEAT_TIMEOUT:
                log("cliente sem heartbeat por %.0fs, derrubando conexao" % silencio)
                self.leave()
                self.guest_reset()
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.conn = None
                self.buf = b""
                continue

            try:
                conn = self.conn
                if conn is None:
                    continue
                r, _, _ = select.select([conn], [], [], 0.2)
                if conn not in r:
                    continue
                chunk = conn.recv(65536)
                if not chunk:
                    raise ConnectionError("fim")
                self.last_rx = time.time()
                self.buf += chunk
                while len(self.buf) >= 5:
                    mt, ln = struct.unpack("<BI", self.buf[:5])
                    if len(self.buf) < 5 + ln:
                        break
                    payload = self.buf[5:5 + ln]
                    self.buf = self.buf[5 + ln:]
                    try:
                        self.handle(mt, payload)
                    except Exception as e:
                        log("erro no evento", mt, e)
            except (OSError, ConnectionError, ssl.SSLError, ValueError) as e:
                log("desconectado:", e)
                self.leave()
                self.guest_reset()
                try:
                    conn.close()
                except Exception:
                    pass
                self.conn = None
                self.buf = b""

    def clip_loop(self):
        # Modo legado (config "clipboard": "poll"). Desligado por padrao: cada
        # leitura acorda o dono da selecao (jogos via Wine congelam). O modo
        # normal e por demanda (PullClipboard ao voltar para o Windows).
        while True:
            time.sleep(1.0)
            try:
                self.sync_clipboard()
            except Exception as e:
                log("clipboard:", e)

    def sync_clipboard(self):
        if self.conn is None:
            return

        types = clipboard_types()
        if not types:
            return

        text_types = ("text/plain", "text/plain;charset=utf-8", "UTF8_STRING", "STRING")
        if any(t in text_types for t in types):
            try:
                out = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, timeout=2)
            except Exception:
                return
            if out.returncode != 0 or not out.stdout:
                return
            key = "text:" + hashlib.sha1(out.stdout).hexdigest()
            if key == self.last_clip_key:
                return
            self.last_clip_key = key
            text = out.stdout.decode("utf-8", "replace")
            self.send(9, text.encode("utf-8"))
            log("clipboard -> windows (%d chars)" % len(text))
            return

        if "image/png" in types:
            try:
                img = subprocess.run(["wl-paste", "--type", "image/png"], capture_output=True, timeout=3)
            except Exception:
                return
            if img.returncode != 0 or not img.stdout:
                return
            key = "image:" + hashlib.sha1(img.stdout).hexdigest()
            if key == self.last_clip_key:
                return
            self.last_clip_key = key
            self.send(10, img.stdout)
            log("clipboard -> windows (imagem %d bytes)" % len(img.stdout))


class Discovery(threading.Thread):
    daemon = True

    def __init__(self, d):
        super().__init__(name="discovery")
        self.d = d
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        self.sock.bind(("0.0.0.0", PORT_UDP))

    def run(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(1024)
            except OSError:
                continue
            if not data.startswith(DISCOVER_MAGIC):
                continue
            reply = json.dumps({"name": socket.gethostname(), "port": PORT_TCP,
                                "w": self.d.remote_w, "h": self.d.remote_h, "v": 1}).encode()
            try:
                self.sock.sendto(reply, addr)
            except OSError:
                pass


if __name__ == "__main__":
    if not os.access("/dev/uinput", os.W_OK):
        log("SEM acesso a /dev/uinput (grupo input / regra udev?)")
        sys.exit(2)
    ensure_cert()
    log("fingerprint:", fingerprint())
    d = Daemon()
    Discovery(d).start()
    cfg = load_json(CONF_FILE) or {}
    if cfg.get("GuestEnabled", True):
        gedge = str(cfg.get("GuestEdge", "bottom")).strip().lower()
        if gedge not in OPPOSITE:
            log("GuestEdge invalida (%r), usando bottom" % gedge)
            gedge = "bottom"
        try:
            gdelay = int(cfg.get("GuestSwitchDelayMs", 250))
        except (TypeError, ValueError):
            log("GuestSwitchDelayMs invalido, usando 250")
            gdelay = 250
        try:
            gspeed = float(cfg.get("GuestSpeed", 1.0))
        except (TypeError, ValueError):
            log("GuestSpeed invalido, usando 1.0")
            gspeed = 1.0
        d.guest_input = GuestInput(d, gedge, gdelay, gspeed)
        d.guest_input.start()
        log("guest (KVM bidirecional) ligado: borda %s" % gedge)
    else:
        log("guest (KVM bidirecional) desligado (GuestEnabled=false)")
    if cfg.get("clipboard", "pull") == "poll":
        threading.Thread(target=d.clip_loop, daemon=True, name="clip").start()
        log("clipboard em modo poll (1s)")
    else:
        log("clipboard sob demanda (ao voltar para o Windows)")
    try:
        d.tcp_loop()
    except KeyboardInterrupt:
        d.leave()
