import argparse
import ctypes
import ctypes.wintypes as wt
import os
import struct
import sys
import threading
import time
import tkinter as tk
import zlib
from dataclasses import dataclass
from tkinter import ttk

# --- Win32 helpers (Windows) ---
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002   # proven: FUN_140074d60@0x140074d60 (ev_3433a31c...) id 1 -> 2
MOUSEEVENTF_LEFTUP = 0x0004     # proven: FUN_140074da8@0x140074da8 (ev_b4249f50...) id 1 -> 4
MOUSEEVENTF_RIGHTDOWN = 0x0008  # proven: FUN_140074d60 id 2 -> 8
MOUSEEVENTF_RIGHTUP = 0x0010    # proven: FUN_140074da8 id 2 -> 0x10
MOUSEEVENTF_MIDDLEDOWN = 0x0020 # proven: FUN_140074d60 id 3 -> 0x20
MOUSEEVENTF_MIDDLEUP = 0x0040   # proven: FUN_140074da8 id 3 -> 0x40
MOUSEEVENTF_ABSOLUTE = 0x8000   # proven cursor move: FUN_14008b7fc@0x14008b7fc (ev_9164e5a1...)

# Proven AutoIt Opt defaults (FUN_140008850@0x140008850, ev_f0b9db6972...): d10m0 / d10m0 / d250m0
MOUSE_CLICK_DELAY_MS_DEFAULT = 10       # MouseClickDelay, slot param1+0x3b
MOUSE_CLICK_DOWN_DELAY_MS_DEFAULT = 10  # MouseClickDownDelay, slot param1+0x1dc
MOUSE_CLICK_DRAG_DELAY_MS_DEFAULT = 250 # MouseClickDragDelay, slot param1+0x3c
MOUSE_COORD_MODE_DEFAULT = 1            # MouseCoordMode, slot param1+0x1cc, range 0..2

# Hotkey: mechanism proven (FUN_140009898@0x140009898 calls RegisterHotKey; parser FUN_1400258b8).
# The specific key (commonly F6 in OP AutoClicker) is script-payload unknown (estimate).
VK_F6 = 0x75
PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class INPUT(ctypes.Structure):
    class _I(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]

    _anonymous_ = ("i",)
    _fields_ = [("type", wt.DWORD), ("i", _I)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def key_down(vk):

    return (user32.GetAsyncKeyState(vk) & 0x8000) != 0


def swap_buttons_registry():
    """Mirror FUN_140001bd4@0x140001bd4 (ev_b8e3167d0f06c272...):
    reads HKCU\\Control Panel\\Mouse 'SwapMouseButtons' and returns true if it starts with '1'.
    Falls back to GetSystemMetrics(SM_SWAPBUTTON)."""
    try:
        HKEY_CURRENT_USER = 0x80000001
        KEY_READ = 0x20019
        hkey = wt.HKEY()
        if advapi32.RegOpenKeyExW(HKEY_CURRENT_USER, "Control Panel\\Mouse", 0, KEY_READ, ctypes.byref(hkey)) == 0:
            try:
                data = ctypes.create_unicode_buffer(64)
                size = wt.DWORD(ctypes.sizeof(data))
                typ = wt.DWORD()
                if advapi32.RegQueryValueExW(hkey, "SwapMouseButtons", 0, ctypes.byref(typ), data, ctypes.byref(size)) == 0:
                    return bool(data.value) and data.value[0] == '1'
            finally:
                advapi32.RegCloseKey(hkey)
    except Exception:
        pass
    try:
        return bool(user32.GetSystemMetrics(23))
    except Exception:
        return False


def proven_delay(ms: int):
    """Mirror FUN_1400753f0@0x1400753f0 (ev_46fcf372ee3f6e5d...):
    if ms<0 return; if ms==0 or ms>14 or QPC fails -> Sleep(ms);
    else (1..14ms) high-res loop: Sleep(0)+QPC until elapsed_ms>=ms."""
    ms = int(ms)
    if ms < 0:
        return
    if ms == 0 or ms > 14:
        kernel32.Sleep(ms)
        return
    try:
        start = ctypes.c_longlong()
        freq = ctypes.c_longlong()
        if not kernel32.QueryPerformanceCounter(ctypes.byref(start)) or not kernel32.QueryPerformanceFrequency(ctypes.byref(freq)) or freq.value <= 0:
            kernel32.Sleep(ms)
            return
        now = ctypes.c_longlong()
        target = start.value + (freq.value * ms) // 1000
        while True:
            kernel32.Sleep(0)
            kernel32.QueryPerformanceCounter(ctypes.byref(now))
            if now.value >= target:
                break
    except Exception:
        time.sleep(ms / 1000.0)


def resolve_button(button: str):
    """Resolve mouse button using AutoIt-proven rules from FUN_140074c80@0x140074c80
    (ev_51913eb1a692493eb3d7f02eefe851d0d23d44c2c599f114c5b0bb18c8aad4a1):
    case-insensitive; empty/LEFT->left, RIGHT->right, MIDDLE->middle,
    MAIN/PRIMARY->swap-aware primary, MENU/SECONDARY->swap-aware secondary, else invalid->left."""
    b = (button or "").strip().lower()
    swapped = swap_buttons_registry()
    if b in ("", "left"):
        return "Left"
    if b == "right":
        return "Right"
    if b == "middle":
        return "Middle"
    if b in ("main", "primary"):
        return "Right" if swapped else "Left"
    if b in ("menu", "secondary"):
        return "Left" if swapped else "Right"
    return "Left"


def _mouse_event(flags):
    inp = INPUT(type=0)
    inp.mi = MOUSEINPUT(0, 0, 0, int(flags), 0, None)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def send_click(button: str, click_type: str, down_delay_ms=None, click_delay_ms=None):
    """Per-click actuation mirrors FUN_140088904@0x140088904 (ev_803f3533..., MOUSECLICKDRAG builtin):
    for each click: down(FUN_140074d60) -> delay(param1+0x1dc MouseClickDownDelay)
                 -> up(FUN_140074da8)   -> delay(param1+0x3b  MouseClickDelay).
    Proven defaults: down 10ms, delay 10ms (FUN_140008850). 'Double' = 2 actuations."""
    if down_delay_ms is None:
        down_delay_ms = MOUSE_CLICK_DOWN_DELAY_MS_DEFAULT
    if click_delay_ms is None:
        click_delay_ms = MOUSE_CLICK_DELAY_MS_DEFAULT
    resolved = resolve_button(button)
    mapping = {
        "Left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "Right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "Middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }
    down, up = mapping.get(resolved, mapping["Left"])
    times = 2 if str(click_type).lower().startswith("double") else 1
    for _ in range(times):
        _mouse_event(down)
        proven_delay(down_delay_ms)
        _mouse_event(up)
        proven_delay(click_delay_ms)


@dataclass
class Settings:
    hours: int
    minutes: int
    seconds: int
    millis: int
    button: str
    click_type: str
    repeat_mode: str  # "until" or "count"
    repeat_count: int

    def interval(self):
        # UI repeat cadence (script-payload unknown). Separate from proven MouseClickDelay.
        total = (
            max(0, self.hours) * 3600000
            + max(0, self.minutes) * 60000
            + max(0, self.seconds) * 1000
            + max(0, self.millis)
        )
        return max(0.001, total / 1000.0)


# --- Minimal PNG encoder (stdlib only) ---
def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def save_png_rgba(path: str, w: int, h: int, rgba: bytes) -> None:
    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)
        raw.extend(rgba[y * stride : (y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 6)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(_png_chunk(b"IHDR", ihdr))
        f.write(_png_chunk(b"IDAT", idat))
        f.write(_png_chunk(b"IEND", b""))


def capture_window_png(title: str, path: str) -> bool:
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return False
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    w = max(1, rect.right - rect.left)
    h = max(1, rect.bottom - rect.top)
    hdc_screen = user32.GetDC(None)
    if not hdc_screen:
        return False
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    if not hdc_mem:
        user32.ReleaseDC(None, hdc_screen)
        return False
    hbm = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    if not hbm:
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)
        return False
    old = gdi32.SelectObject(hdc_mem, hbm)
    pw = getattr(user32, "PrintWindow", None)
    ok = False
    if pw:
        try:
            ok = bool(pw(hwnd, hdc_mem, PW_RENDERFULLCONTENT))
        except Exception:
            ok = False
    if not ok:
        gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, rect.left, rect.top, SRCCOPY)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB
    buf = (ctypes.c_ubyte * (w * h * 4))()
    lines = gdi32.GetDIBits(hdc_mem, hbm, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
    gdi32.SelectObject(hdc_mem, old)
    gdi32.DeleteObject(hbm)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(None, hdc_screen)
    if not lines:
        return False
    data = bytearray(buf)
    for i in range(0, len(data), 4):
        data[i], data[i + 2] = data[i + 2], data[i]
    import os
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    save_png_rgba(path, w, h, bytes(data))
    return True


# --- UI (modern black/purple) ---
BG = "#07070c"; CARD = "#10101a"; CARD_ALT = "#171726"; CARD_BORDER = "#2a1f4a"
TEXT = "#f5f3ff"; MUTED = "#9b93b8"; SOFT = "#c4b5fd"; ACCENT = "#8b5cf6"; ACCENT_HOVER = "#a78bfa"; ACCENT_DEEP = "#6d28d9"
SUCCESS = "#8b5cf6"; SUCCESS_HOVER = "#a78bfa"; DANGER = "#f43f5e"; DANGER_HOVER = "#fb7185"
FIELD = "#0d0d16"; FIELD_BORDER = "#332a52"; BADGE_BG = "#1d1533"


class BetterClickerApp:
    def __init__(self, root: tk.Tk, screenshot_path: str | None):
        self.root = root
        self.screenshot_path = screenshot_path
        self.running = False
        self.stop_event = threading.Event()
        self.worker = None
        self.clicks_done = 0
        self.lock = threading.Lock()
        self.hotkey_vk = VK_F6
        self.hotkey_name = "F6"
        self.hotkey_was = False
        self.capturing_hotkey = False
        self.frame_count = 0
        self._screenshot_attempts = 0

        # UI defaults: script-payload unknown (estimates). Proven runtime defaults used for actuation
        # are MouseClickDownDelay/MouseClickDelay 10ms (FUN_140008850@0x140008850, FUN_140088904@0x140088904).
        self.hours = tk.IntVar(value=0)
        self.minutes = tk.IntVar(value=0)
        self.seconds = tk.IntVar(value=0)
        self.millis = tk.IntVar(value=100)  # estimate (script-payload unknown)
        self.button = tk.StringVar(value="Left")
        self.click_type = tk.StringVar(value="Single")
        self.repeat_mode = tk.StringVar(value="until")
        self.repeat_count = tk.IntVar(value=1)
        self.status_var = tk.StringVar(value="Ready to click")
        self.state_text = tk.StringVar(value="Idle")

        self._setup_style()
        self._build()
        self._poll()

    def _resource_path(self, *parts):
        bases = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bases.append(meipass)
        bases.append(os.path.dirname(os.path.abspath(__file__)))
        # Fallback to user-provided source icon location.
        bases.append(r"C:\Users\umber\Downloads\Neuer Ordner")
        for base in bases:
            p = os.path.join(base, *parts)
            if os.path.exists(p):
                return p
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)

    def _apply_icon(self):
        for rel in [("assets", "icon.png"), ("icon.png",)]:
            try:
                path = self._resource_path(*rel)
                if not path or not os.path.exists(path):
                    continue
                img = tk.PhotoImage(file=path)
                self.root.iconphoto(True, img)
                self._icon_img = img  # keep reference
                return
            except Exception:
                continue
        # Best-effort .ico for Windows title bar/taskbar path.
        try:
            ico = self._resource_path("assets", "icon.ico")
            if ico and os.path.exists(ico):
                self.root.iconbitmap(ico)
        except Exception:
            pass

    def _setup_style(self):
        self.root.title("BetterClicker")
        self.root.configure(bg=BG)
        self.root.geometry("700x540")
        self.root.minsize(660, 520)
        self._apply_icon()
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        base_font = ("Segoe UI", 10)
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=FIELD, font=base_font, borderwidth=0)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Soft.TLabel", background=BG, foreground=SOFT)
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI Semibold", 22))
        style.configure("Subtitle.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 10))
        style.configure("Section.TLabel", background=CARD, foreground=TEXT, font=("Segoe UI Semibold", 11))
        style.configure("Hint.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Badge.TLabel", background=BADGE_BG, foreground=SOFT, font=("Segoe UI Semibold", 9), padding=(10, 4))
        style.configure("StateIdle.TLabel", background=BG, foreground=MUTED, font=("Segoe UI Semibold", 10))
        style.configure("StateRun.TLabel", background=BG, foreground=ACCENT_HOVER, font=("Segoe UI Semibold", 10))
        style.configure("TEntry", fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT, padding=4)
        style.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=TEXT, arrowcolor=SOFT, padding=4)
        style.map("TCombobox", fieldbackground=[("readonly", FIELD), ("focus", FIELD)], foreground=[("readonly", TEXT)], selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)])
        try:
            self.root.option_add("*TCombobox*Listbox.background", FIELD)
            self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
            self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_DEEP)
            self.root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        except Exception:
            pass
        style.configure("TRadiobutton", background=CARD, foreground=TEXT, font=base_font, indicatorcolor=FIELD)
        style.map("TRadiobutton", background=[("active", CARD)], foreground=[("active", TEXT)], indicatorcolor=[("selected", ACCENT), ("!selected", FIELD_BORDER)])
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 11), padding=(18, 11), background=ACCENT, foreground="white", borderwidth=0)
        style.map("Accent.TButton", background=[("!disabled", ACCENT), ("active", ACCENT_HOVER), ("pressed", ACCENT_DEEP)], foreground=[("!disabled", "white")])
        style.configure("Danger.TButton", font=("Segoe UI Semibold", 11), padding=(18, 11), background=DANGER, foreground="white", borderwidth=0)
        style.map("Danger.TButton", background=[("!disabled", DANGER), ("active", DANGER_HOVER), ("pressed", "#e11d48")], foreground=[("!disabled", "white")])
        style.configure("Pick.TButton", font=("Segoe UI Semibold", 9), padding=(12, 6), background=ACCENT_DEEP, foreground="white", borderwidth=0)
        style.map("Pick.TButton", background=[("active", ACCENT), ("!disabled", ACCENT_DEEP)], foreground=[("!disabled", "white")])
        style.configure("Ghost.TButton", font=("Segoe UI", 9), padding=(10, 5), background=CARD_ALT, foreground=SOFT, borderwidth=0)
        style.map("Ghost.TButton", background=[("active", CARD_BORDER)], foreground=[("active", TEXT)])

    def _card(self, parent, title, hint=None):
        outer = tk.Frame(parent, bg=CARD, highlightbackground=CARD_BORDER, highlightthickness=1, bd=0)
        inner = ttk.Frame(outer, style="Card.TFrame", padding=14)
        inner.pack(fill="both", expand=True)
        head = ttk.Frame(inner, style="Card.TFrame")
        head.pack(fill="x", pady=(0, 10))
        ttk.Label(head, text=title, style="Section.TLabel").pack(side="left")
        if hint:
            ttk.Label(head, text=hint, style="Hint.TLabel").pack(side="right")
        return outer, inner

    def _spin(self, parent, var, from_, to, width=6):
        return tk.Spinbox(parent, from_=from_, to=to, textvariable=var, width=width, font=("Segoe UI", 10), bg=FIELD, fg=TEXT, disabledbackground=FIELD, disabledforeground=MUTED, insertbackground=TEXT, readonlybackground=FIELD, buttonbackground=CARD_ALT, buttoncursor="hand2", relief="flat", bd=0, highlightthickness=1, highlightbackground=FIELD_BORDER, highlightcolor=ACCENT)

    def _build(self):
        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill="both", expand=True)
        header = tk.Frame(shell, bg=BG)
        header.pack(fill="x", padx=22, pady=(20, 10))
        left = ttk.Frame(header); left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text="BetterClicker", style="Title.TLabel").pack(anchor="w")
        ttk.Label(left, text="Modern auto clicker • black & violet theme", style="Subtitle.TLabel").pack(anchor="w", pady=(2, 0))
        ttk.Label(left, text='by "@Akari-my Github"', style="Soft.TLabel").pack(anchor="w", pady=(2, 0))
        right = ttk.Frame(header); right.pack(side="right", anchor="n")
        self.hotkey_badge = ttk.Label(right, text=f"{self.hotkey_name} Start/Stop", style="Badge.TLabel")
        self.hotkey_badge.pack(anchor="e", pady=(0, 6))
        self.state_label = ttk.Label(right, textvariable=self.state_text, style="StateIdle.TLabel")
        self.state_label.pack(anchor="e")
        tk.Frame(shell, bg=ACCENT_DEEP, height=2, bd=0, highlightthickness=0).pack(fill="x", padx=22, pady=(0, 10))
        content = tk.Frame(shell, bg=BG)
        content.pack(fill="both", expand=True, padx=22, pady=4)
        c, inner = self._card(content, "Click interval", "Time between clicks (UI cadence)")
        c.pack(fill="x", pady=(0, 10))
        row = ttk.Frame(inner, style="Card.TFrame"); row.pack(fill="x")
        for i, (label, var, mx) in enumerate([("Hours", self.hours, 24), ("Minutes", self.minutes, 59), ("Seconds", self.seconds, 59), ("Milliseconds", self.millis, 999)]):
            cell = ttk.Frame(row, style="Card.TFrame"); cell.pack(side="left", fill="x", expand=True, padx=(0, 10 if i < 3 else 0))
            ttk.Label(cell, text=label, style="Card.TLabel").pack(anchor="w")
            self._spin(cell, var, 0, mx, width=7).pack(anchor="w", fill="x", pady=(4, 0), ipady=4)
        mid = ttk.Frame(content); mid.pack(fill="x", pady=(0, 10))
        mid.columnconfigure(0, weight=1); mid.columnconfigure(1, weight=1)
        c1, i1 = self._card(mid, "Click options", "Button & style")
        c1.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        g = ttk.Frame(i1, style="Card.TFrame"); g.pack(fill="x"); g.columnconfigure(1, weight=1)
        ttk.Label(g, text="Mouse button", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Combobox(g, textvariable=self.button, values=["Left", "Right", "Middle"], state="readonly", width=12).grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=6)
        ttk.Label(g, text="Click type", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Combobox(g, textvariable=self.click_type, values=["Single", "Double"], state="readonly", width=12).grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=6)
        c2, i2 = self._card(mid, "Repeat", "How many clicks")
        c2.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        r1 = ttk.Frame(i2, style="Card.TFrame"); r1.pack(fill="x", anchor="w", pady=(0, 6))
        ttk.Radiobutton(r1, text="Repeat", variable=self.repeat_mode, value="count").pack(side="left")
        self._spin(r1, self.repeat_count, 1, 1000000, width=8).pack(side="left", padx=8, ipady=3)
        ttk.Label(r1, text="times", style="Card.TLabel").pack(side="left")
        ttk.Radiobutton(i2, text="Repeat until stopped", variable=self.repeat_mode, value="until").pack(anchor="w", pady=(4, 0))
        c3, i3 = self._card(content, "Hotkey", "Change activation key")
        c3.pack(fill="x", pady=(0, 10))
        hr = ttk.Frame(i3, style="Card.TFrame"); hr.pack(fill="x")
        self.hotkey_label = ttk.Label(hr, text=f"Current: {self.hotkey_name}", style="Card.TLabel")
        self.hotkey_label.pack(side="left")
        ttk.Button(hr, text="Change hotkey", style="Pick.TButton", command=self.capture_hotkey).pack(side="left", padx=(12, 0))
        ttk.Label(i3, text="Press the button, then press any key to set it (Esc to cancel).", style="Hint.TLabel").pack(anchor="w", pady=(8, 0))
        bottom = tk.Frame(shell, bg=BG); bottom.pack(fill="x", padx=20, pady=(4, 18))
        self.start_btn = ttk.Button(bottom, text=f"Start ({self.hotkey_name})", style="Accent.TButton", command=self.toggle); self.start_btn.pack(side="left")
        status_wrap = ttk.Frame(bottom); status_wrap.pack(side="left", fill="x", expand=True, padx=14)
        ttk.Label(status_wrap, textvariable=self.status_var, style="Muted.TLabel").pack(anchor="w")
        ttk.Label(status_wrap, text="Hotkey works while the app is open", style="Soft.TLabel").pack(anchor="w")

    def _refresh_hotkey_ui(self):
        label = f"{self.hotkey_name} Start/Stop"
        try:
            self.hotkey_badge.config(text=label)
            self.hotkey_label.config(text=f"Current: {self.hotkey_name}")
            if self.running:
                self.start_btn.config(text=f"Stop ({self.hotkey_name})")
            else:
                self.start_btn.config(text=f"Start ({self.hotkey_name})")
        except Exception:
            pass

    @staticmethod
    def _hotkey_display(vk: int, keysym: str) -> str:
        ks = (keysym or "").strip()
        if len(ks) == 1 and ks.isprintable():
            return ks.upper()
        if ks:
            return ks
        if 0x70 <= vk <= 0x87:
            return f"F{vk - 0x6F}"
        return f"VK_{vk}"

    def capture_hotkey(self):
        if self.capturing_hotkey:
            return
        self.capturing_hotkey = True
        self.status_var.set("Press a key for Start/Stop (Esc to cancel)...")
        dlg = tk.Toplevel(self.root)
        dlg.title("Set Hotkey")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        frm = tk.Frame(dlg, bg=BG, padx=20, pady=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Press any key to use as Start/Stop hotkey", style="Muted.TLabel").pack(anchor="w")
        ttk.Label(frm, text="Esc cancels", style="Soft.TLabel").pack(anchor="w", pady=(4, 0))

        def finish(vk=None, keysym=None):
            if not self.capturing_hotkey:
                try:
                    dlg.destroy()
                except Exception:
                    pass
                return
            self.capturing_hotkey = False
            if vk is not None and vk != 0x1B:
                self.hotkey_vk = int(vk)
                self.hotkey_name = self._hotkey_display(int(vk), keysym or "")
                self.status_var.set(f"Hotkey set to {self.hotkey_name}")
                self._refresh_hotkey_ui()
            else:
                self.status_var.set("Hotkey change cancelled")
            try:
                dlg.destroy()
            except Exception:
                pass

        def on_key(event):
            vk = getattr(event, "keycode", None)
            if vk is None:
                return "break"
            finish(vk, getattr(event, "keysym", ""))
            return "break"

        dlg.bind("<KeyPress>", on_key)
        dlg.protocol("WM_DELETE_WINDOW", lambda: finish(None, None))
        dlg.focus_force()

    def get_settings(self) -> Settings:
        def iv(v, d=0):
            try: return int(v.get())
            except Exception: return d
        return Settings(
            hours=iv(self.hours), minutes=iv(self.minutes), seconds=iv(self.seconds), millis=iv(self.millis),
            button=self.button.get(), click_type=self.click_type.get(), repeat_mode=self.repeat_mode.get(),
            repeat_count=max(1, iv(self.repeat_count, 1)),
        )

    def toggle(self):
        if self.running:
            self.stop()
        else:
            self.start()

    def _set_state(self, running: bool):
        self.state_text.set("Running" if running else "Idle")
        try:
            self.state_label.configure(style="StateRun.TLabel" if running else "StateIdle.TLabel")
        except Exception:
            pass

    def start(self):
        if self.running:
            return
        # Stop any stale run handle (defensive against inconsistent state).
        try:
            self.stop_event.set()
        except Exception:
            pass
        s = self.get_settings()
        self.stop_event = threading.Event()
        stop_event = self.stop_event
        self.clicks_done = 0
        self.running = True
        self.start_btn.config(text=f"Stop ({self.hotkey_name})", style="Danger.TButton")
        self._set_state(True)
        self.status_var.set("Clicking...")
        self.worker = threading.Thread(target=self._run_clicks, args=(s, stop_event), daemon=True)
        self.worker.start()

    def stop(self):
        try:
            self.stop_event.set()
        except Exception:
            pass
        self.running = False
        self.start_btn.config(text=f"Start ({self.hotkey_name})", style="Accent.TButton")
        self._set_state(False)
        self.status_var.set(f"Stopped ({self.clicks_done} clicks)")

    def _run_clicks(self, s: Settings, stop_event: threading.Event):
        interval = s.interval()  # UI cadence (estimated/script-payload unknown)
        max_clicks = s.repeat_count if s.repeat_mode == "count" else None
        while not stop_event.is_set():
            try:
                # Actuation uses proven FUN_140088904 pattern: down->DownDelay(10)->up->ClickDelay(10)
                send_click(s.button, s.click_type,
                           down_delay_ms=MOUSE_CLICK_DOWN_DELAY_MS_DEFAULT,
                           click_delay_ms=MOUSE_CLICK_DELAY_MS_DEFAULT)
            except Exception:
                pass
            with self.lock:
                self.clicks_done += 1
                done = self.clicks_done
            if max_clicks is not None and done >= max_clicks:
                break
            end = time.time() + interval
            while time.time() < end:
                if stop_event.is_set():
                    break
                time.sleep(min(0.015, max(0.001, end - time.time())))
        self.root.after(0, lambda ev=stop_event: self._on_worker_done(ev))

    def _on_worker_done(self, stop_event: threading.Event):
        # Only update UI for the active run.
        if stop_event is not self.stop_event:
            return
        if self.running:
            self.running = False
            self.start_btn.config(text=f"Start ({self.hotkey_name})", style="Accent.TButton")
            self._set_state(False)
            self.status_var.set(f"Finished ({self.clicks_done} clicks)")

    def _poll(self):
        self.frame_count += 1
        try:
            down = key_down(self.hotkey_vk)
            if not self.capturing_hotkey and down and not self.hotkey_was:
                self.toggle()
            self.hotkey_was = down
        except Exception:
            pass
        if self.screenshot_path and self.frame_count >= 120:
            self._screenshot_attempts += 1
            self.root.update_idletasks(); self.root.update()
            ok = capture_window_png(self.root.title(), self.screenshot_path)
            if ok or self._screenshot_attempts >= 8:
                self.root.after(50, self.root.destroy)
                return
        self.root.after(16, self._poll)

    def on_close(self):
        self.stop_event.set(); self.running = False
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--screenshot", dest="screenshot", help="Save a PNG screenshot to this path and exit")
    args, _ = parser.parse_known_args()
    root = tk.Tk()
    app = BetterClickerApp(root, args.screenshot)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()