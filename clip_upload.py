#!/usr/bin/env python3
"""
剪贴板图片上传工具 (Windows)
后台运行，按 Ctrl+Alt+U 上传当前剪贴板图片到服务器，路径自动写回剪贴板。
托盘图标，右键退出 / 打开设置 / 检查更新。
"""

import io
import json
import logging
import msvcrt
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from tkinter import ttk
import tkinter as tk

__version__ = "1.3.0"
REPO_API = "https://api.github.com/repos/nickw116/clip-upload/releases/latest"

# ── 日志 ──────────────────────────────────────────────
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "clip-upload"
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_PATH = CONFIG_DIR / "clip_upload.log"
LOCK_PATH = CONFIG_DIR / "clip_upload.lock"

CONFIG_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("clip_upload")
log.setLevel(logging.DEBUG)
_fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_fh)


# ── 单实例锁 ─────────────────────────────────────────
class SingleInstance:
    def __init__(self):
        self.lockfile = None

    def acquire(self):
        try:
            self.lockfile = open(LOCK_PATH, "w")
            msvcrt.locking(self.lockfile.fileno(), msvcrt.LK_NBLCK, 1)
            self.lockfile.write(str(os.getpid()))
            self.lockfile.flush()
            return True
        except (IOError, OSError):
            if self.lockfile:
                self.lockfile.close()
            return False

    def release(self):
        try:
            if self.lockfile:
                msvcrt.locking(self.lockfile.fileno(), msvcrt.LK_UNLCK, 1)
                self.lockfile.close()
        except Exception:
            pass


# ── 配置 ──────────────────────────────────────────────
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent

DEFAULT_CONFIG = {
    "server": "",
    "port": 22,
    "username": "",
    "password": "",
    "ssh_key": "",
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
    except Exception as e:
        log.warning("get_clipboard_image failed: %s", e)
        return None


def set_clipboard_text(text):
    try:
        import win32clipboard, win32con
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        win32clipboard.CloseClipboard()
        return
    except Exception as e:
        log.debug("win32clipboard failed: %s", e)
    try:
        import base64
        safe = text.replace("'", "''").replace("\n", " ")
        ps_cmd = f"Set-Clipboard -Value '{safe}'"
        encoded = base64.b64encode(ps_cmd.encode("utf-16-le")).decode()
        subprocess.run(
            ["powershell", "-EncodedCommand", encoded],
            capture_output=True, timeout=5,
        )
    except Exception as e:
        log.error("set_clipboard_text failed: %s", e)


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
    server = cfg.get("server", "").strip()
    remote_path = cfg["remote_path"].rstrip("/")
    dest = f"{remote_path}/{filename}"

    if not server or server in ("localhost", "127.0.0.1", "local"):
        os.makedirs(remote_path.replace("/", os.sep), exist_ok=True)
        shutil.copy2(local_path, dest.replace("/", os.sep))
        return dest

    import paramiko

    port = int(cfg.get("port", 22))
    username = cfg.get("username", "").strip()
    password = cfg.get("password", "")
    ssh_key = cfg.get("ssh_key", "").strip()

    transport = None
    try:
        transport = paramiko.Transport((server, port))

        if ssh_key and os.path.isfile(ssh_key):
            key = None
            for loader in [paramiko.Ed25519Key.from_private_key_file,
                           paramiko.RSAKey.from_private_key_file,
                           paramiko.ECDSAKey.from_private_key_file]:
                try:
                    key = loader(ssh_key)
                    break
                except (paramiko.SSHException, ValueError):
                    continue
            if not key:
                raise RuntimeError(f"无法加载密钥文件: {ssh_key}")
            transport.connect(username=username, pkey=key)
        else:
            transport.connect(username=username, password=password)

        sftp = paramiko.SFTPClient.from_transport(transport)

        # 确保远程目录存在
        dirs_to_create = []
        d = remote_path
        while d and d != "/":
            dirs_to_create.append(d)
            d = "/".join(d.split("/")[:-1])
        dirs_to_create.reverse()

        for d in dirs_to_create:
            try:
                sftp.stat(d)
            except IOError:
                try:
                    sftp.mkdir(d)
                except IOError as e:
                    log.debug("mkdir %s: %s", d, e)

        sftp.put(local_path, dest)
        log.info("SFTP uploaded to %s:%s", server, dest)
        return dest
    finally:
        if transport:
            transport.close()


# ── 通知 (纯 tkinter, 不依赖 pkg_resources) ──────────
def show_notification(title, message):
    # 用 tkinter 弹窗替代 win10toast，避免 pkg_resources 依赖
    def _show():
        try:
            root = tk.Tk()
            root.withdraw()
            root.after(3000, root.destroy)
            try:
                from tkinter import messagebox
                # 不用 messagebox, 用一个无边框小窗口
                pass
            except Exception:
                pass
            root.mainloop()
        except Exception:
            pass

    # 尝试系统托盘气泡通知
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxTimeoutW(
            0, message, title, 0x40, 0, 3000
        )
        return
    except Exception:
        pass

    # 最终 fallback: PowerShell 通知
    try:
        import base64
        safe_title = str(title).replace("'", "''").replace("\n", " ")[:100]
        safe_msg = str(message).replace("'", "''").replace("\n", " ")[:200]
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
            capture_output=True, timeout=8,
        )
    except Exception as e:
        log.debug("notification failed: %s", e)


# ── 核心上传动作 ──────────────────────────────────────
def do_upload(cfg):
    try:
        # 检查配置
        server = cfg.get("server", "").strip()
        if not server:
            show_notification("Clip Upload", "请先配置服务器信息：右键托盘图标 → 设置")
            return

        username = cfg.get("username", "").strip()
        password = cfg.get("password", "")
        ssh_key = cfg.get("ssh_key", "").strip()
        if not username:
            show_notification("Clip Upload", "请先配置用户名：右键托盘图标 → 设置")
            return
        if not password and not ssh_key:
            show_notification("Clip Upload", "请先配置密码或 SSH 密钥：右键托盘图标 → 设置")
            return

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
            log.error("upload failed: %s\n%s", e, traceback.format_exc())
            show_notification("Clip Upload", f"上传失败: {e}")
            return
        finally:
            os.unlink(temp_path)

        content = build_clipboard_content(cfg, filename)
        set_clipboard_text(content)
        show_notification("Clip Upload", content)
        log.info("uploaded: %s", content)
    except Exception as e:
        log.error("do_upload error: %s\n%s", e, traceback.format_exc())


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
chcp 65001 >/dev/null
echo 正在更新 ClipUpload...
:retry
del "{exe_path}"
if exist "{exe_path}" (
    timeout /t 1 /nobreak >/dev/null
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
        log.error("do_update failed: %s", e)


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

        w, h = 520, 560
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        main = ttk.Frame(self.root, padding=20)
        main.pack(fill="both", expand=True)

        row = 0

        def add_label(text, r):
            ttk.Label(main, text=text).grid(row=r, column=0, sticky="w", pady=5)

        # 版本标题
        header = ttk.Frame(main)
        header.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(header, text="Clip Upload", font=("", 14, "bold")).pack(side="left")
        ttk.Label(header, text=f"  v{__version__}", foreground="gray").pack(side="left")
        row += 1

        # 服务器地址
        add_label("服务器地址:", row)
        self.server_var = tk.StringVar(value=cfg.get("server", ""))
        ttk.Entry(main, textvariable=self.server_var, width=38).grid(
            row=row, column=1, sticky="ew", pady=5, padx=(10, 0)
        )
        row += 1

        # 端口
        add_label("端口:", row)
        self.port_var = tk.StringVar(value=str(cfg.get("port", 22)))
        ttk.Entry(main, textvariable=self.port_var, width=10).grid(
            row=row, column=1, sticky="w", pady=5, padx=(10, 0)
        )
        row += 1

        # 用户名
        add_label("用户名:", row)
        self.username_var = tk.StringVar(value=cfg.get("username", ""))
        ttk.Entry(main, textvariable=self.username_var, width=38).grid(
            row=row, column=1, sticky="ew", pady=5, padx=(10, 0)
        )
        row += 1

        # 密码
        add_label("密码:", row)
        pwd_frame = ttk.Frame(main)
        pwd_frame.grid(row=row, column=1, sticky="ew", pady=5, padx=(10, 0))
        self.password_var = tk.StringVar(value=cfg.get("password", ""))
        self.pwd_entry = ttk.Entry(pwd_frame, textvariable=self.password_var, width=30, show="*")
        self.pwd_entry.pack(side="left", fill="x", expand=True)
        self.show_pwd_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(pwd_frame, text="显示", variable=self.show_pwd_var,
                        command=self._toggle_pwd).pack(side="left", padx=(6, 0))
        row += 1

        # SSH 密钥
        add_label("SSH 密钥:", row)
        key_frame = ttk.Frame(main)
        key_frame.grid(row=row, column=1, sticky="ew", pady=5, padx=(10, 0))
        self.key_var = tk.StringVar(value=cfg.get("ssh_key", ""))
        ttk.Entry(key_frame, textvariable=self.key_var, width=28).pack(side="left", fill="x", expand=True)
        ttk.Button(key_frame, text="浏览", command=self._browse_key, width=6).pack(side="left", padx=(4, 0))
        row += 1

        # 分隔线
        ttk.Separator(main, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=8
        )
        row += 1

        # 远程路径
        add_label("远程路径:", row)
        self.path_var = tk.StringVar(value=cfg.get("remote_path", ""))
        ttk.Entry(main, textvariable=self.path_var, width=38).grid(
            row=row, column=1, sticky="ew", pady=5, padx=(10, 0)
        )
        row += 1

        # URL 前缀
        add_label("URL 前缀:", row)
        self.url_var = tk.StringVar(value=cfg.get("url_prefix", ""))
        ttk.Entry(main, textvariable=self.url_var, width=38).grid(
            row=row, column=1, sticky="ew", pady=5, padx=(10, 0)
        )
        row += 1

        # 粘贴板格式
        add_label("粘贴板格式:", row)
        self.fmt_var = tk.StringVar(value=cfg.get("clipboard_format", "path"))
        fmt_frame = ttk.Frame(main)
        fmt_frame.grid(row=row, column=1, sticky="w", pady=5, padx=(10, 0))
        for val, label in [("path", "服务器路径"), ("url", "HTTP URL"), ("markdown", "Markdown")]:
            ttk.Radiobutton(fmt_frame, text=label, variable=self.fmt_var, value=val).pack(
                side="left", padx=(0, 12)
            )
        row += 1

        # 文件命名
        add_label("文件命名:", row)
        self.name_var = tk.StringVar(value=cfg.get("file_naming", "datetime"))
        name_frame = ttk.Frame(main)
        name_frame.grid(row=row, column=1, sticky="w", pady=5, padx=(10, 0))
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
            row=row, column=1, sticky="w", pady=5, padx=(10, 0)
        )
        row += 1

        # 自动更新
        self.auto_update_var = tk.BooleanVar(value=cfg.get("auto_update", True))
        ttk.Checkbutton(main, text="自动检查更新（每天一次）", variable=self.auto_update_var).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=5
        )
        row += 1

        # 按钮
        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=12)
        ttk.Button(btn_frame, text="保存", command=self._save, width=12).pack(side="left", padx=8)
        ttk.Button(btn_frame, text="取消", command=self.root.destroy, width=12).pack(side="left", padx=8)

        main.columnconfigure(1, weight=1)

    def _toggle_pwd(self):
        self.pwd_entry.config(show="" if self.show_pwd_var.get() else "*")

    def _browse_key(self):
        path = tk.filedialog.askopenfilename(
            title="选择 SSH 私钥文件",
            filetypes=[("所有文件", "*.*"), ("PEM", "*.pem"), ("PPK", "*.ppk")],
        )
        if path:
            self.key_var.set(path)

    def _save(self):
        self.cfg["server"] = self.server_var.get().strip()
        try:
            self.cfg["port"] = int(self.port_var.get().strip() or 22)
        except ValueError:
            self.cfg["port"] = 22
        self.cfg["username"] = self.username_var.get().strip()
        self.cfg["password"] = self.password_var.get()
        self.cfg["ssh_key"] = self.key_var.get().strip()
        self.cfg["remote_path"] = self.path_var.get().strip()
        self.cfg["url_prefix"] = self.url_var.get().strip()
        self.cfg["clipboard_format"] = self.fmt_var.get()
        self.cfg["file_naming"] = self.name_var.get()
        self.cfg["hotkey"] = self.hotkey_var.get().strip()
        self.cfg["auto_update"] = self.auto_update_var.get()
        save_config(self.cfg)
        log.info("config saved: server=%s user=%s path=%s",
                 self.cfg["server"], self.cfg["username"], self.cfg["remote_path"])
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
    except ImportError as e:
        log.error("pystray import failed: %s", e)
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

    def on_open_log(icon, item):
        os.startfile(str(LOG_PATH))

    icon = pystray.Icon(
        "clip-upload",
        make_icon(),
        f"Clip Upload v{__version__}",
        menu=pystray.Menu(
            pystray.MenuItem("上传剪贴板图片", on_upload),
            pystray.MenuItem("设置...", on_settings),
            pystray.MenuItem("检查更新...", on_check_update),
            pystray.MenuItem("打开配置文件", on_open),
            pystray.MenuItem("打开日志", on_open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda icon, item: (icon.stop(), on_quit())),
        ),
    )
    return icon


# ── 主入口 ────────────────────────────────────────────
def main():
    log.info("=" * 50)
    log.info("ClipUpload v%s starting", __version__)
    log.info("exe: %s", sys.executable)
    log.info("config: %s", CONFIG_PATH)

    # 单实例检查
    instance = SingleInstance()
    if not instance.acquire():
        log.warning("another instance already running, exiting")
        show_notification("Clip Upload", "已在运行中，请查看系统托盘")
        return

    cfg = load_config()
    hotkey = cfg.get("hotkey", "ctrl+alt+u")
    quit_event = lambda: (instance.release(), os._exit(0))

    log.info("server=%s port=%s user=%s path=%s",
             cfg.get("server"), cfg.get("port"), cfg.get("username"), cfg.get("remote_path"))

    try:
        import keyboard
        keyboard.add_hotkey(hotkey, lambda: do_upload(cfg), suppress=False)
        log.info("hotkey registered: %s", hotkey)
    except Exception as e:
        log.warning("hotkey register failed: %s", e)

    threading.Thread(target=lambda: auto_update_check(cfg, quit_event), daemon=True).start()

    icon = create_tray_icon(cfg, on_quit=quit_event)
    if icon:
        log.info("starting tray icon")
        icon.run()
    else:
        log.warning("tray unavailable, running in background")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.critical("fatal: %s\n%s", e, traceback.format_exc())
