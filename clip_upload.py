#!/usr/bin/env python3
"""
剪贴板图片上传工具 (Windows)
后台运行，按 Ctrl+Alt+U 上传当前剪贴板图片到服务器，路径自动写回剪贴板。
托盘图标，右键退出 / 切换服务器 / 打开设置 / 检查更新。
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
from tkinter import ttk, simpledialog, messagebox
import tkinter as tk

__version__ = "1.4.0"
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

# 每个 profile 的字段
PROFILE_FIELDS = {
    "server": "",
    "port": 22,
    "username": "",
    "password": "",
    "ssh_key": "",
    "remote_path": "/var/www/images",
    "url_prefix": "",
    "clipboard_format": "path",
}

# 全局设置字段
GLOBAL_FIELDS = {
    "file_naming": "datetime",
    "image_format": "png",
    "hotkey": "ctrl+alt+u",
    "auto_update": True,
    "last_check": "",
}

DEFAULT_CONFIG = {
    "active_profile": "default",
    "profiles": {
        "default": dict(PROFILE_FIELDS),
    },
    "global": dict(GLOBAL_FIELDS),
}


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        # 向前兼容: 从旧格式迁移
        if "profiles" not in cfg:
            old = {k: v for k, v in cfg.items() if k not in GLOBAL_FIELDS}
            glob = {k: v for k, v in cfg.items() if k in GLOBAL_FIELDS}
            cfg = {
                "active_profile": "default",
                "profiles": {"default": {**PROFILE_FIELDS, **old}},
                "global": {**GLOBAL_FIELDS, **glob},
            }
            save_config(cfg)
            log.info("migrated old config to profile format")
        # 补齐缺失字段
        for name, prof in cfg.get("profiles", {}).items():
            for k, v in PROFILE_FIELDS.items():
                prof.setdefault(k, v)
        for k, v in GLOBAL_FIELDS.items():
            cfg.setdefault("global", {})
            cfg["global"].setdefault(k, v)
        return cfg
    save_config(DEFAULT_CONFIG)
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def get_active_profile(cfg):
    """返回当前激活的 profile 字典"""
    name = cfg.get("active_profile", "default")
    return cfg["profiles"].get(name, dict(PROFILE_FIELDS))


def get_merged_config(cfg):
    """返回 profile + global 合并后的配置，用于上传"""
    prof = get_active_profile(cfg)
    merged = {**prof, **cfg.get("global", {})}
    merged["_profile_name"] = cfg.get("active_profile", "default")
    return merged


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


# ── 通知 ──────────────────────────────────────────────
def show_notification(title, message):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxTimeoutW(
            0, message, title, 0x40, 0, 3000
        )
        return
    except Exception:
        pass
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
        merged = get_merged_config(cfg)

        server = merged.get("server", "").strip()
        if not server:
            show_notification("Clip Upload", "请先配置服务器：右键托盘图标 → 设置")
            return
        username = merged.get("username", "").strip()
        if not username:
            show_notification("Clip Upload", "请先配置用户名：右键托盘图标 → 设置")
            return
        password = merged.get("password", "")
        ssh_key = merged.get("ssh_key", "").strip()
        if not password and not ssh_key:
            show_notification("Clip Upload", "请先配置密码或 SSH 密钥：右键托盘图标 → 设置")
            return

        image_data = get_clipboard_image()
        if not image_data:
            show_notification("Clip Upload", "剪贴板中没有图片，请先截图")
            return

        filename = generate_filename(merged)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(image_data)
            temp_path = f.name

        try:
            upload_file(temp_path, merged, filename)
        except Exception as e:
            log.error("upload failed: %s\n%s", e, traceback.format_exc())
            show_notification("Clip Upload", f"上传失败: {e}")
            return
        finally:
            os.unlink(temp_path)

        content = build_clipboard_content(merged, filename)
        set_clipboard_text(content)
        profile_name = merged.get("_profile_name", "")
        label = f"[{profile_name}] " if profile_name else ""
        show_notification("Clip Upload", f"{label}{content}")
        log.info("uploaded [%s]: %s", profile_name, content)
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
        if parse_version(tag) <= parse_version(__version__):
            if not silent:
                show_notification("Clip Upload", f"已是最新版本 v{__version__}")
            return None
        for a in data.get("assets", []):
            if a["name"] == "ClipUpload.exe":
                return {"version": tag, "url": a["browser_download_url"], "notes": data.get("body", "")}
        return None
    except Exception as e:
        if not silent:
            show_notification("Clip Upload", f"检查更新失败: {e}")
        return None


def do_update(info, on_quit):
    try:
        import urllib.request
        exe_path = Path(sys.executable)
        new_exe = CONFIG_DIR / "ClipUpload_new.exe"
        updater_bat = CONFIG_DIR / "updater.bat"

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
        with open(updater_bat, "w", encoding="utf-8") as f:
            f.write(bat)

        subprocess.Popen(
            ["cmd", "/c", str(updater_bat)],
            creationflags=subprocess.CREATE_NO_WINDOW,
            close_fds=True,
        )
        on_quit()
    except Exception as e:
        show_notification("Clip Upload", f"更新失败: {e}")
        log.error("do_update failed: %s", e)


def auto_update_check(cfg, on_quit):
    glob = cfg.get("global", {})
    if not glob.get("auto_update", True):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if glob.get("last_check") == today:
        return
    glob["last_check"] = today
    save_config(cfg)
    info = check_for_update(silent=True)
    if info:
        d = UpdateDialog(info, on_update=lambda: do_update(info, on_quit), on_skip=lambda: None)
        d.show()


# ── 更新确认对话框 ────────────────────────────────────
class UpdateDialog:
    def __init__(self, info, on_update, on_skip):
        self.root = tk.Tk()
        self.root.title("Clip Upload 更新")
        self.root.resizable(False, False)
        self.root.configure(bg="#f5f5f5")
        w, h = 500, 280
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.on_update = on_update
        self.on_skip = on_skip

        main = ttk.Frame(self.root, padding=24)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text=f"发现新版本: {info['version']}", font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(main, text=f"当前版本: v{__version__}").pack(anchor="w", pady=(4, 12))
        if info.get("notes"):
            ttk.Label(main, text="更新内容:").pack(anchor="w")
            txt = tk.Text(main, height=4, width=52, wrap="word")
            txt.insert("1.0", info["notes"][:300])
            txt.config(state="disabled")
            txt.pack(fill="x", pady=6)

        btn = ttk.Frame(main)
        btn.pack(pady=16)
        ttk.Button(btn, text="立即更新", command=self._do_update, width=14).pack(side="left", padx=12)
        ttk.Button(btn, text="跳过", command=self._do_skip, width=14).pack(side="left", padx=12)

    def _do_update(self):
        self.root.destroy()
        self.on_update()

    def _do_skip(self):
        self.root.destroy()
        self.on_skip()

    def show(self):
        self.root.mainloop()


# ── 设置窗口 (多 Profile) ────────────────────────────
class SettingsDialog:
    def __init__(self, cfg, on_save=None):
        self.cfg = cfg
        self.on_save = on_save
        self.profiles = json.loads(json.dumps(cfg.get("profiles", {})))
        self.active = cfg.get("active_profile", "default")

        self.root = tk.Tk()
        self.root.title("Clip Upload 设置")
        self.root.resizable(False, False)
        self.root.configure(bg="#f5f5f5")

        w, h = 540, 600
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        main = ttk.Frame(self.root, padding=15)
        main.pack(fill="both", expand=True)

        # ── 标题 ──
        header = ttk.Frame(main)
        header.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        ttk.Label(header, text="Clip Upload", font=("", 14, "bold")).pack(side="left")
        ttk.Label(header, text=f"  v{__version__}", foreground="gray").pack(side="left")

        # ── Profile 选择器 ──
        sel_frame = ttk.LabelFrame(main, text="服务器配置", padding=8)
        sel_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 8))

        ttk.Label(sel_frame, text="当前:").grid(row=0, column=0, sticky="w")
        self.profile_var = tk.StringVar(value=self.active)
        self.profile_combo = ttk.Combobox(
            sel_frame, textvariable=self.profile_var,
            values=list(self.profiles.keys()), state="readonly", width=20
        )
        self.profile_combo.grid(row=0, column=1, padx=6)
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_switch)

        ttk.Button(sel_frame, text="+ 新建", command=self._add_profile, width=6).grid(
            row=0, column=2, padx=2
        )
        ttk.Button(sel_frame, text="重命名", command=self._rename_profile, width=6).grid(
            row=0, column=3, padx=2
        )
        ttk.Button(sel_frame, text="删除", command=self._delete_profile, width=6).grid(
            row=0, column=4, padx=2
        )

        # ── 服务器字段 ──
        fields_frame = ttk.LabelFrame(main, text="连接设置", padding=8)
        fields_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=4)

        row = 0

        def add_field(label, var_name, width=35, **kw):
            nonlocal row
            ttk.Label(fields_frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
            var = tk.StringVar()
            setattr(self, var_name, var)
            entry = ttk.Entry(fields_frame, textvariable=var, width=width, **kw)
            entry.grid(row=row, column=1, sticky="ew", pady=3, padx=(8, 0))
            row += 1
            return entry

        add_field("服务器地址:", "server_var")
        add_field("端口:", "port_var", width=8)

        ttk.Label(fields_frame, text="用户名:").grid(row=row, column=0, sticky="w", pady=3)
        self.username_var = tk.StringVar()
        ttk.Entry(fields_frame, textvariable=self.username_var, width=35).grid(
            row=row, column=1, sticky="ew", pady=3, padx=(8, 0)
        )
        row += 1

        # 密码 (带显示/隐藏)
        ttk.Label(fields_frame, text="密码:").grid(row=row, column=0, sticky="w", pady=3)
        pwd_frame = ttk.Frame(fields_frame)
        pwd_frame.grid(row=row, column=1, sticky="ew", pady=3, padx=(8, 0))
        self.password_var = tk.StringVar()
        self.pwd_entry = ttk.Entry(pwd_frame, textvariable=self.password_var, width=28, show="*")
        self.pwd_entry.pack(side="left", fill="x", expand=True)
        self.show_pwd_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(pwd_frame, text="显示", variable=self.show_pwd_var,
                        command=self._toggle_pwd).pack(side="left", padx=(4, 0))
        row += 1

        # SSH 密钥
        ttk.Label(fields_frame, text="SSH 密钥:").grid(row=row, column=0, sticky="w", pady=3)
        key_frame = ttk.Frame(fields_frame)
        key_frame.grid(row=row, column=1, sticky="ew", pady=3, padx=(8, 0))
        self.key_var = tk.StringVar()
        ttk.Entry(key_frame, textvariable=self.key_var, width=26).pack(side="left", fill="x", expand=True)
        ttk.Button(key_frame, text="浏览", command=self._browse_key, width=5).pack(side="left", padx=(4, 0))
        row += 1

        ttk.Separator(fields_frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=6
        )
        row += 1

        add_field("远程路径:", "path_var")
        add_field("URL 前缀:", "url_var")

        # 粘贴板格式
        ttk.Label(fields_frame, text="粘贴板格式:").grid(row=row, column=0, sticky="w", pady=3)
        self.fmt_var = tk.StringVar()
        fmt_frame = ttk.Frame(fields_frame)
        fmt_frame.grid(row=row, column=1, sticky="w", pady=3, padx=(8, 0))
        for val, label in [("path", "路径"), ("url", "URL"), ("markdown", "MD")]:
            ttk.Radiobutton(fmt_frame, text=label, variable=self.fmt_var, value=val).pack(
                side="left", padx=(0, 10)
            )
        row += 1

        fields_frame.columnconfigure(1, weight=1)

        # ── 全局设置 ──
        glob = cfg.get("global", {})
        glob_frame = ttk.LabelFrame(main, text="通用设置", padding=8)
        glob_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=4)

        ttk.Label(glob_frame, text="文件命名:").grid(row=0, column=0, sticky="w", pady=3)
        self.name_var = tk.StringVar(value=glob.get("file_naming", "datetime"))
        nf = ttk.Frame(glob_frame)
        nf.grid(row=0, column=1, sticky="w", pady=3, padx=(8, 0))
        ttk.Radiobutton(nf, text="日期时间", variable=self.name_var, value="datetime").pack(side="left", padx=(0, 12))
        ttk.Radiobutton(nf, text="随机 UUID", variable=self.name_var, value="uuid").pack(side="left")

        ttk.Label(glob_frame, text="快捷键:").grid(row=1, column=0, sticky="w", pady=3)
        self.hotkey_var = tk.StringVar(value=glob.get("hotkey", "ctrl+alt+u"))
        ttk.Entry(glob_frame, textvariable=self.hotkey_var, width=18).grid(
            row=1, column=1, sticky="w", pady=3, padx=(8, 0)
        )

        self.auto_update_var = tk.BooleanVar(value=glob.get("auto_update", True))
        ttk.Checkbutton(glob_frame, text="自动检查更新", variable=self.auto_update_var).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=3
        )

        # ── 按钮 ──
        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=4, column=0, columnspan=3, pady=10)
        ttk.Button(btn_frame, text="保存", command=self._save, width=12).pack(side="left", padx=8)
        ttk.Button(btn_frame, text="取消", command=self.root.destroy, width=12).pack(side="left", padx=8)

        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        main.columnconfigure(2, weight=1)

        # 加载当前 profile 数据
        self._load_profile_to_ui(self.active)

    def _load_profile_to_ui(self, name):
        prof = self.profiles.get(name, dict(PROFILE_FIELDS))
        self.server_var.set(prof.get("server", ""))
        self.port_var.set(str(prof.get("port", 22)))
        self.username_var.set(prof.get("username", ""))
        self.password_var.set(prof.get("password", ""))
        self.key_var.set(prof.get("ssh_key", ""))
        self.path_var.set(prof.get("remote_path", ""))
        self.url_var.set(prof.get("url_prefix", ""))
        self.fmt_var.set(prof.get("clipboard_format", "path"))

    def _save_current_profile_from_ui(self):
        name = self.profile_var.get()
        if name not in self.profiles:
            return
        self.profiles[name].update({
            "server": self.server_var.get().strip(),
            "port": int(self.port_var.get().strip() or 22),
            "username": self.username_var.get().strip(),
            "password": self.password_var.get(),
            "ssh_key": self.key_var.get().strip(),
            "remote_path": self.path_var.get().strip(),
            "url_prefix": self.url_var.get().strip(),
            "clipboard_format": self.fmt_var.get(),
        })

    def _on_profile_switch(self, event=None):
        self._save_current_profile_from_ui()
        new_name = self.profile_var.get()
        self.active = new_name
        self._load_profile_to_ui(new_name)

    def _add_profile(self):
        name = tk.simpledialog.askstring("新建配置", "配置名称:", parent=self.root)
        if name and name.strip():
            name = name.strip()
            if name in self.profiles:
                show_notification("Clip Upload", f"配置 '{name}' 已存在")
                return
            self._save_current_profile_from_ui()
            self.profiles[name] = dict(PROFILE_FIELDS)
            self.profile_combo["values"] = list(self.profiles.keys())
            self.profile_var.set(name)
            self.active = name
            self._load_profile_to_ui(name)

    def _rename_profile(self):
        old_name = self.profile_var.get()
        name = tk.simpledialog.askstring("重命名", "新名称:", initialvalue=old_name, parent=self.root)
        if name and name.strip() and name.strip() != old_name:
            name = name.strip()
            self._save_current_profile_from_ui()
            self.profiles[name] = self.profiles.pop(old_name)
            if self.active == old_name:
                self.active = name
            self.profile_combo["values"] = list(self.profiles.keys())
            self.profile_var.set(name)

    def _delete_profile(self):
        name = self.profile_var.get()
        if len(self.profiles) <= 1:
            show_notification("Clip Upload", "至少保留一个配置")
            return
        if tk.messagebox.askyesno("删除配置", f"确定删除 '{name}'?", parent=self.root):
            del self.profiles[name]
            first = list(self.profiles.keys())[0]
            self.active = first
            self.profile_combo["values"] = list(self.profiles.keys())
            self.profile_var.set(first)
            self._load_profile_to_ui(first)

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
        self._save_current_profile_from_ui()
        self.cfg["active_profile"] = self.active
        self.cfg["profiles"] = self.profiles
        self.cfg["global"] = {
            "file_naming": self.name_var.get(),
            "image_format": "png",
            "hotkey": self.hotkey_var.get().strip(),
            "auto_update": self.auto_update_var.get(),
            "last_check": self.cfg.get("global", {}).get("last_check", ""),
        }
        save_config(self.cfg)
        log.info("config saved: active=%s profiles=%s", self.active, list(self.profiles.keys()))
        if self.on_save:
            self.on_save(self.cfg)
        self.root.destroy()

    def show(self):
        self.root.mainloop()


# ── 托盘图标 (动态菜单) ──────────────────────────────
class TrayApp:
    def __init__(self, cfg, on_quit):
        self.cfg = cfg
        self.on_quit = on_quit
        self.icon = None
        self._menu_cache = None

    def _make_icon_image(self):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([14, 6, 50, 50], radius=6, fill="#4A90D9", outline="#2E6BA6", width=2)
        draw.polygon([(32, 2), (16, 24), (48, 24)], fill="#4A90D9")
        draw.rectangle([24, 54, 40, 62], fill="#4A90D9", outline="#2E6BA6")
        return img

    def _build_menu(self):
        import pystray
        active = self.cfg.get("active_profile", "default")
        profiles = self.cfg.get("profiles", {})
        merged = get_merged_config(self.cfg)
        server_label = merged.get("server", "") or "未配置"
        active_text = f"[{active}] {server_label}"

        items = [
            pystray.MenuItem(f"上传  (当前: {active_text})", lambda i, item: do_upload(self.cfg)),
            pystray.Menu.SEPARATOR,
        ]

        # 服务器列表
        if profiles:
            profile_items = []
            for name, prof in profiles.items():
                svr = prof.get("server", "") or "未配置"
                label = f"{name}  ({svr})"
                profile_items.append(
                    pystray.MenuItem(
                        label,
                        lambda i, item, n=name: self._switch_profile(n),
                        checked=lambda i, item, n=name: n == active,
                        radio=True,
                    )
                )
            items.append(pystray.Menu("切换服务器", *profile_items))
            items.append(pystray.Menu.SEPARATOR)

        items.extend([
            pystray.MenuItem("设置...", lambda i, item: self._open_settings()),
            pystray.MenuItem("检查更新...", lambda i, item: self._check_update()),
            pystray.MenuItem("打开配置文件", lambda i, item: os.startfile(str(CONFIG_PATH))),
            pystray.MenuItem("打开日志", lambda i, item: os.startfile(str(LOG_PATH))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda i, item: (self.icon.stop(), self.on_quit())),
        ])

        return pystray.Menu(*items)

    def _switch_profile(self, name):
        self.cfg["active_profile"] = name
        save_config(self.cfg)
        merged = get_merged_config(self.cfg)
        log.info("switched to profile: %s (%s)", name, merged.get("server", ""))
        show_notification("Clip Upload", f"已切换到: {name}")
        self._refresh_icon()

    def _refresh_icon(self):
        if self.icon:
            self.icon.menu = self._build_menu()
            active = self.cfg.get("active_profile", "default")
            merged = get_merged_config(self.cfg)
            svr = merged.get("server", "") or "未配置"
            self.icon.title = f"Clip Upload v{__version__} - [{active}] {svr}"

    def _open_settings(self):
        def open_dialog():
            d = SettingsDialog(self.cfg, on_save=lambda c: (self.cfg.update(c), self._refresh_icon()))
            d.show()
        threading.Thread(target=open_dialog, daemon=True).start()

    def _check_update(self):
        def check():
            info = check_for_update(silent=False)
            if info:
                d = UpdateDialog(info, on_update=lambda: do_update(info, self.on_quit), on_skip=lambda: None)
                d.show()
            else:
                show_notification("Clip Upload", f"已是最新版本 v{__version__}")
        threading.Thread(target=check, daemon=True).start()

    def run(self):
        try:
            import pystray
        except ImportError as e:
            log.error("pystray import failed: %s", e)
            return False

        try:
            self.icon = pystray.Icon("clip-upload", self._make_icon_image(), "Clip Upload", self._build_menu())
            self._refresh_icon()
            log.info("starting tray icon")
            self.icon.run()
            return True
        except Exception as e:
            log.error("tray icon failed: %s\n%s", e, traceback.format_exc())
            return False


def _show_fallback_window(cfg, quit_event):
    """托盘不可用时的 fallback 窗口"""
    root = tk.Tk()
    root.title("Clip Upload")
    root.geometry("320x160")
    root.resizable(False, False)
    root.configure(bg="#f5f5f5")

    active = cfg.get("active_profile", "default")
    merged = get_merged_config(cfg)
    svr = merged.get("server", "") or "未配置"

    ttk.Label(root, text=f"Clip Upload v{__version__}", font=("", 12, "bold")).pack(pady=(16, 4))
    ttk.Label(root, text=f"当前服务器: [{active}] {svr}").pack()
    ttk.Label(root, text="快捷键: Ctrl+Alt+U 上传截图").pack(pady=4)
    ttk.Button(root, text="设置", command=lambda: threading.Thread(
        target=lambda: SettingsDialog(cfg, on_save=lambda c: None).show(), daemon=True
    ).start(), width=10).pack(pady=8)
    ttk.Button(root, text="退出", command=lambda: (root.destroy(), quit_event()), width=10).pack()

    root.protocol("WM_DELETE_WINDOW", lambda: (root.destroy(), quit_event()))
    root.mainloop()


# ── 主入口 ────────────────────────────────────────────
def main():
    log.info("=" * 50)
    log.info("ClipUpload v%s starting", __version__)
    log.info("exe: %s", sys.executable)

    instance = SingleInstance()
    if not instance.acquire():
        log.warning("another instance already running, exiting")
        show_notification("Clip Upload", "已在运行中，请查看系统托盘")
        return

    cfg = load_config()
    quit_event = lambda: (instance.release(), os._exit(0))

    active = cfg.get("active_profile", "default")
    merged = get_merged_config(cfg)
    log.info("active profile: %s | server=%s user=%s path=%s",
             active, merged.get("server"), merged.get("username"), merged.get("remote_path"))

    hotkey = cfg.get("global", {}).get("hotkey", "ctrl+alt+u")
    try:
        import keyboard
        keyboard.add_hotkey(hotkey, lambda: do_upload(cfg), suppress=False)
        log.info("hotkey registered: %s", hotkey)
    except Exception as e:
        log.warning("hotkey register failed: %s", e)

    threading.Thread(target=lambda: auto_update_check(cfg, quit_event), daemon=True).start()

    tray = TrayApp(cfg, on_quit=quit_event)
    if not tray.run():
        log.warning("tray unavailable, showing fallback window")
        _show_fallback_window(cfg, quit_event)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.critical("fatal: %s\n%s", e, traceback.format_exc())
