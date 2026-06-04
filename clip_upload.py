#!/usr/bin/env python3
"""
剪贴板图片上传工具 (Windows)
后台运行，按 Ctrl+Alt+U 上传当前剪贴板图片到服务器，路径自动写回剪贴板。
托盘图标，右键退出 / 打开设置 / 检查更新。
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from tkinter import ttk
import tkinter as tk

__version__ = "1.0.0"
REPO_API = "https://api.github.com/repos/nickw116/clip-upload/releases/latest"

# ── 配置 ──────────────────────────────────────────────
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "clip-upload"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "server": "user@your-server.com",
    "remote_path": "/var/www/images",
    "url_prefix": "",
    "file_naming": "datetime",
    "image_format": "png",
    "clipboard_format": "path",
    "hotkey": "ctrl+alt+u",
    "auto_update": True,
    "last_check": "",
}


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    save_config(DEFAULT_CONFIG)
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ── 剪贴板 ────────────────────────────────────────────
def get_clipboard_image():
    try:
        from PIL import ImageGrab
        img = ImageGrab.grabclipboard()
        if img is None:
            return None
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def set_clipboard_text(text):
    try:
        import win32clipboard, win32con
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        win32clipboard.CloseClipboard()
    except Exception:
        import base64
        safe = text.replace("'", "''")
        ps_cmd = f"Set-Clipboard -Value '{safe}'"
        encoded = base64.b64encode(ps_cmd.encode("utf-16-le")).decode()
        subprocess.run(
            ["powershell", "-EncodedCommand", encoded],
            capture_output=True,
        )


# ── 文件名 & 路径 ─────────────────────────────────────
def generate_filename(cfg):
    if cfg.get("file_naming") == "uuid":
        name = uuid.uuid4().hex[:8]
    else:
        name = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{name}.{cfg.get('image_format', 'png')}"


def build_clipboard_content(cfg, filename):
    remote = cfg["remote_path"].rstrip("/")
    fmt = cfg.get("clipboard_format", "path")
    url_prefix = cfg.get("url_prefix", "").rstrip("/")
    if fmt == "url" and url_prefix:
        return f"{url_prefix}/{filename}"
    elif fmt == "markdown" and url_prefix:
        return f"![image]({url_prefix}/{filename})"
    else:
        return f"{remote}/{filename}"


# ── 上传 ──────────────────────────────────────────────
def upload_file(local_path, cfg, filename):
    server = cfg["server"]
    remote_path = cfg["remote_path"].rstrip("/")
    dest = f"{remote_path}/{filename}"

    if server in ("localhost", "127.0.0.1", "local"):
        os.makedirs(remote_path.replace("/", os.sep), exist_ok=True)
        shutil.copy2(local_path, dest.replace("/", os.sep))
    else:
        full_remote = f"{server}:{dest}"
        r = subprocess.run(
            ["scp", "-q", "-o", "ConnectTimeout=10", local_path, full_remote],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())
    return dest


# ── 通知 ──────────────────────────────────────────────
def show_notification(title, message):
    try:
        from win10toast_click import ToastNotifier
        ToastNotifier().show_toast(title, message, duration=3, threaded=True)
        return
    except Exception:
        pass
    try:
        import base64
        safe_title = str(title).replace("'", "''")
        safe_msg = str(message).replace("'", "''")
        ps = (
            "[System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms'); "
            f"$n = New-Object System.Windows.Forms.NotifyIcon; "
            f"$n.Icon = [System.Drawing.SystemIcons]::Information; "
            f"$n.Visible = $true; "
            f"$n.ShowBalloonTip(3000, '{safe_title}', '{safe_msg}', 'Info'); "
            f"Start-Sleep -Seconds 3; "
            f"$n.Dispose()"
        )
        encoded = base64.b64encode(ps.encode("utf-16-le")).decode()
        subprocess.run(
            ["powershell", "-EncodedCommand", encoded],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


# ── 核心上传动作 ──────────────────────────────────────
def do_upload(cfg):
    image_data = get_clipboard_image()
    if not image_data:
        show_notification("Clip Upload", "剪贴板中没有图片，请先截图")
        return

    filename = generate_filename(cfg)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(image_data)
        temp_path = f.name

    try:
        upload_file(temp_path, cfg, filename)
    except Exception as e:
        show_notification("Clip Upload", f"上传失败: {e}")
        return
    finally:
        os.unlink(temp_path)

    content = build_clipboard_content(cfg, filename)
    set_clipboard_text(content)
    show_notification("Clip Upload", content)


# ── 自动更新 ──────────────────────────────────────────
def parse_version(tag):
    return tuple(int(x) for x in tag.lstrip("v").split("."))


def check_for_update(silent=True):
    try:
        import urllib.request
        req = urllib.request.Request(REPO_API, headers={"User-Agent": "ClipUpload"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        tag = data.get("tag_name", "")
        if not tag:
            return None
        remote_ver = parse_version(tag)
        local_ver = parse_version(__version__)
        if remote_ver <= local_ver:
            if not silent:
                show_notification("Clip Upload", f"已是最新版本 v{__version__}")
            return None
        asset_url = None
        for a in data.get("assets", []):
            if a["name"] == "ClipUpload.exe":
                asset_url = a["browser_download_url"]
                break
        if not asset_url:
            return None
        return {"version": tag, "url": asset_url, "notes": data.get("body", "")}
    except Exception as e:
        if not silent:
            show_notification("Clip Upload", f"检查更新失败: {e}")
        return None


def do_update(info, on_quit):
    try:
        import urllib.request
        exe_path = Path(sys.executable)
        new_exe = CONFIG_DIR / "ClipUpload_new.exe"
        updater = CONFIG_DIR / "updater.bat"

        show_notification("Clip Upload", f"正在下载 v{info['version']}...")

        with urllib.request.urlopen(info["url"], timeout=60) as resp:
            with open(new_exe, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)

        bat = f"""@echo off
chcp 65001 >nul
echo 正在更新 ClipUpload...
:retry
del "{exe_path}"
if exist "{exe_path}" (
    timeout /t 1 /nobreak >nul
    goto retry
)
move /y "{new_exe}" "{exe_path}"
start "" "{exe_path}"
del "%~f0"
"""
        with open(updater, "w", encoding="utf-8") as f:
            f.write(bat)

        subprocess.Popen(
            ["cmd", "/c", str(updater)],
            creationflags=subprocess.CREATE_NO_WINDOW,
            close_fds=True,
        )
        on_quit()
    except Exception as e:
        show_notification("Clip Upload", f"更新失败: {e}")


def auto_update_check(cfg, on_quit):
    if not cfg.get("auto_update", True):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if cfg.get("last_check") == today:
        return
    cfg["last_check"] = today
    save_config(cfg)
    info = check_for_update(silent=True)
    if info:
        d = UpdateDialog(
            info,
            on_update=lambda: do_update(info, on_quit),
            on_skip=lambda: None,
        )
        d.show()


# ── 更新确认对话框 ────────────────────────────────────
class UpdateDialog:
    def __init__(self, info, on_update, on_skip):
        self.root = tk.Tk()
        self.root.title("Clip Upload 更新")
        self.root.resizable(False, False)
        self.root.configure(bg="#f5f5f5")
        w, h = 400, 220
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        main = ttk.Frame(self.root, padding=20)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text=f"发现新版本: {info['version']}", font=("", 12, "bold")).pack(
            anchor="w"
        )
        ttk.Label(main, text=f"当前版本: v{__version__}").pack(anchor="w", pady=(4, 8))
        if info.get("notes"):
            notes = info["notes"][:200]
            ttk.Label(main, text="更新内容:").pack(anchor="w")
            txt = tk.Text(main, height=4, width=45, wrap="word", state="normal")
            txt.insert("1.0", notes)
            txt.config(state="disabled")
            txt.pack(fill="x", pady=4)

        btn = ttk.Frame(main)
        btn.pack(pady=12)
        ttk.Button(btn, text="立即更新", command=lambda: self._do(on_update), width=10).pack(
            side="left", padx=6
        )
        ttk.Button(btn, text="跳过", command=lambda: self._do(on_skip), width=10).pack(
            side="left", padx=6
        )

    def _do(self, cb):
        self.root.destroy()
        cb()

    def show(self):
        self.root.mainloop()


# ── 设置窗口 ──────────────────────────────────────────
class SettingsDialog:
    def __init__(self, cfg, on_save=None):
        self.cfg = cfg
        self.on_save = on_save

        self.root = tk.Tk()
        self.root.title("Clip Upload 设置")
        self.root.resizable(False, False)
        self.root.configure(bg="#f5f5f5")

        w, h = 500, 440
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        main = ttk.Frame(self.root, padding=20)
        main.pack(fill="both", expand=True)

        row = 0

        def add_label(text, r):
            ttk.Label(main, text=text).grid(row=r, column=0, sticky="w", pady=6)

        # 版本信息
        header = ttk.Frame(main)
        header.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Label(header, text="Clip Upload", font=("", 14, "bold")).pack(side="left")
        ttk.Label(header, text=f"  v{__version__}", foreground="gray").pack(side="left")
        row += 1

        # 服务器
        add_label("服务器地址:", row)
        self.server_var = tk.StringVar(value=cfg.get("server", ""))
        ttk.Entry(main, textvariable=self.server_var, width=40).grid(
            row=row, column=1, sticky="ew", pady=6, padx=(10, 0)
        )
        row += 1

        # 远程路径
        add_label("远程路径:", row)
        self.path_var = tk.StringVar(value=cfg.get("remote_path", ""))
        ttk.Entry(main, textvariable=self.path_var, width=40).grid(
            row=row, column=1, sticky="ew", pady=6, padx=(10, 0)
        )
        row += 1

        # URL 前缀
        add_label("URL 前缀:", row)
        self.url_var = tk.StringVar(value=cfg.get("url_prefix", ""))
        ttk.Entry(main, textvariable=self.url_var, width=40).grid(
            row=row, column=1, sticky="ew", pady=6, padx=(10, 0)
        )
        row += 1

        # 粘贴板格式
        add_label("粘贴板格式:", row)
        self.fmt_var = tk.StringVar(value=cfg.get("clipboard_format", "path"))
        fmt_frame = ttk.Frame(main)
        fmt_frame.grid(row=row, column=1, sticky="w", pady=6, padx=(10, 0))
        for val, label in [("path", "服务器路径"), ("url", "HTTP URL"), ("markdown", "Markdown")]:
            ttk.Radiobutton(fmt_frame, text=label, variable=self.fmt_var, value=val).pack(
                side="left", padx=(0, 12)
            )
        row += 1

        # 文件命名
        add_label("文件命名:", row)
        self.name_var = tk.StringVar(value=cfg.get("file_naming", "datetime"))
        name_frame = ttk.Frame(main)
        name_frame.grid(row=row, column=1, sticky="w", pady=6, padx=(10, 0))
        ttk.Radiobutton(name_frame, text="日期时间", variable=self.name_var, value="datetime").pack(
            side="left", padx=(0, 12)
        )
        ttk.Radiobutton(name_frame, text="随机 UUID", variable=self.name_var, value="uuid").pack(
            side="left"
        )
        row += 1

        # 快捷键
        add_label("快捷键:", row)
        self.hotkey_var = tk.StringVar(value=cfg.get("hotkey", "ctrl+alt+u"))
        ttk.Entry(main, textvariable=self.hotkey_var, width=20).grid(
            row=row, column=1, sticky="w", pady=6, padx=(10, 0)
        )
        row += 1

        # 自动更新
        self.auto_update_var = tk.BooleanVar(value=cfg.get("auto_update", True))
        ttk.Checkbutton(main, text="自动检查更新（每天一次）", variable=self.auto_update_var).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=6
        )
        row += 1

        # 按钮
        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=16)
        ttk.Button(btn_frame, text="保存", command=self._save, width=12).pack(side="left", padx=8)
        ttk.Button(btn_frame, text="取消", command=self.root.destroy, width=12).pack(side="left", padx=8)

        main.columnconfigure(1, weight=1)

    def _save(self):
        self.cfg["server"] = self.server_var.get().strip()
        self.cfg["remote_path"] = self.path_var.get().strip()
        self.cfg["url_prefix"] = self.url_var.get().strip()
        self.cfg["clipboard_format"] = self.fmt_var.get()
        self.cfg["file_naming"] = self.name_var.get()
        self.cfg["hotkey"] = self.hotkey_var.get().strip()
        self.cfg["auto_update"] = self.auto_update_var.get()
        save_config(self.cfg)
        if self.on_save:
            self.on_save(self.cfg)
        self.root.destroy()

    def show(self):
        self.root.mainloop()


# ── 托盘图标 ──────────────────────────────────────────
def create_tray_icon(cfg, on_quit):
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    def make_icon():
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([14, 6, 50, 50], radius=6, fill="#4A90D9", outline="#2E6BA6", width=2)
        draw.polygon([(32, 2), (16, 24), (48, 24)], fill="#4A90D9")
        draw.rectangle([24, 54, 40, 62], fill="#4A90D9", outline="#2E6BA6")
        return img

    def on_upload(icon, item):
        do_upload(cfg)

    def on_settings(icon, item):
        def open_settings():
            d = SettingsDialog(cfg)
            d.show()
            cfg.update(load_config())

        threading.Thread(target=open_settings, daemon=True).start()

    def on_open(icon, item):
        os.startfile(str(CONFIG_PATH))

    def on_check_update(icon, item):
        def check():
            info = check_for_update(silent=False)
            if info:
                d = UpdateDialog(
                    info,
                    on_update=lambda: do_update(info, on_quit),
                    on_skip=lambda: None,
                )
                d.show()
            else:
                show_notification("Clip Upload", f"已是最新版本 v{__version__}")

        threading.Thread(target=check, daemon=True).start()

    icon = pystray.Icon(
        "clip-upload",
        make_icon(),
        f"Clip Upload v{__version__}",
        menu=pystray.Menu(
            pystray.MenuItem("上传剪贴板图片", on_upload),
            pystray.MenuItem("设置...", on_settings),
            pystray.MenuItem("检查更新...", on_check_update),
            pystray.MenuItem("打开配置文件", on_open),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda icon, item: (icon.stop(), on_quit())),
        ),
    )
    return icon


# ── 主入口 ────────────────────────────────────────────
def main():
    cfg = load_config()
    hotkey = cfg.get("hotkey", "ctrl+alt+u")
    quit_event = lambda: os._exit(0)

    print(f"[Clip Upload] v{__version__} | 服务器: {cfg['server']} | 路径: {cfg['remote_path']}")
    print(f"[Clip Upload] 快捷键: {hotkey}")

    # 注册全局快捷键
    try:
        import keyboard
        keyboard.add_hotkey(hotkey, lambda: do_upload(cfg), suppress=False)
        print("[Clip Upload] 快捷键已注册")
    except Exception as e:
        print(f"[Clip Upload] 快捷键注册失败: {e}")

    # 启动后静默检查更新
    threading.Thread(target=lambda: auto_update_check(cfg, quit_event), daemon=True).start()

    # 启动托盘
    icon = create_tray_icon(cfg, on_quit=quit_event)
    if icon:
        icon.run()
    else:
        print("[Clip Upload] 托盘不可用，保持后台运行")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
