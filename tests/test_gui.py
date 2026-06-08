"""
Windows GUI 自动化测试 - 在 GitHub Actions Windows runner 上运行
Part 1: 纯逻辑 + Win32 ctypes 结构体 (Linux/Windows 都能跑)
Part 2: 真实 GUI 测试 (仅 Windows, 用 pywinauto)
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import threading
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
APP_SCRIPT = SCRIPT_DIR.parent / "clip_upload.py"  # 项目根目录
TEST_DIR = Path(tempfile.mkdtemp(prefix="clip_upload_gui_test_"))

PASS = 0
FAIL = 0
ERRORS = []


def check(name, fn):
    global PASS, FAIL
    print(f"  {name} ... ", end="", flush=True)
    try:
        fn()
        print("PASS")
        PASS += 1
    except Exception as e:
        print(f"FAIL: {e}")
        ERRORS.append(f"{name}: {e}")
        import traceback; traceback.print_exc()
        FAIL += 1


# ══════════════════════════════════════════════════════
#  Part 1: 逻辑 + Win32 结构体测试 (跨平台)
# ══════════════════════════════════════════════════════
print("=" * 60)
print("  Part 1: 逻辑 + Win32 结构体测试")
print("=" * 60)
print()


def test_import():
    # Mock Windows-only modules for Linux
    import unittest.mock as mock
    if sys.platform != "win32":
        sys.modules.setdefault("msvcrt", mock.MagicMock())
        if not hasattr(ctypes, "windll"):
            ctypes.windll = mock.MagicMock()
            ctypes.wintypes = mock.MagicMock()
            ctypes.windll.user32 = mock.MagicMock()
            ctypes.windll.kernel32 = mock.MagicMock()
            ctypes.windll.shell32 = mock.MagicMock()
            ctypes.windll.ole32 = mock.MagicMock()
            ctypes.windll.comdlg32 = mock.MagicMock()
        if not hasattr(os, "startfile"):
            os.startfile = lambda p: None

    import importlib.util
    spec = importlib.util.spec_from_file_location("clip_upload", str(APP_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["clip_upload"] = mod
    spec.loader.exec_module(mod)
    assert hasattr(mod, 'FolderWatcherManager')
    assert hasattr(mod, '_win32_browse_folder')
    assert hasattr(mod, '_win32_open_file_dialog')

check("模块导入", test_import)


def test_browseinfow_struct():
    """验证 BROWSEINFOW 结构体可以创建并设置字段"""
    from ctypes import c_wchar_p, c_int, c_void_p, Structure, sizeof

    class BROWSEINFOW(Structure):
        _fields_ = [
            ("hwndOwner", c_void_p),
            ("pidlRoot", c_void_p),
            ("pszDisplayName", ctypes.c_wchar * 260),
            ("lpszTitle", c_wchar_p),
            ("ulFlags", c_int),
            ("lpfn", c_void_p),
            ("lParam", c_void_p),
            ("iImage", c_int),
        ]

    bi = BROWSEINFOW()
    # 验证结构体大小合理 (64-bit: 8+8+520+8+4+8+8+4 = ~568, 含 padding)
    size = sizeof(BROWSEINFOW)
    assert size > 0, "struct size should be positive"
    # 验证可以设置字段
    bi.lpszTitle = "Test"
    bi.ulFlags = 0x40

check("BROWSEINFOW 结构体", test_browseinfow_struct)


def test_openfilenamew_struct():
    from ctypes import c_wchar_p, c_int, c_void_p, Structure, sizeof, cast, create_unicode_buffer

    class OPENFILENAMEW(Structure):
        _fields_ = [
            ("lStructSize", c_int),
            ("hwndOwner", c_void_p),
            ("hInstance", c_void_p),
            ("lpstrFilter", c_wchar_p),
            ("lpstrCustomFilter", c_wchar_p),
            ("nMaxCustFilter", c_int),
            ("nFilterIndex", c_int),
            ("lpstrFile", c_wchar_p),
            ("nMaxFile", c_int),
            ("lpstrFileTitle", c_wchar_p),
            ("nMaxFileTitle", c_int),
            ("lpstrInitialDir", c_wchar_p),
            ("lpstrTitle", c_wchar_p),
            ("Flags", c_int),
            ("nFileOffset", ctypes.c_ushort),
            ("nFileExtension", ctypes.c_ushort),
            ("lpstrDefExt", c_wchar_p),
            ("lCustData", c_void_p),
            ("lpfnHook", c_void_p),
            ("lpTemplateName", c_wchar_p),
        ]

    buf = create_unicode_buffer(260)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = sizeof(OPENFILENAMEW)
    ofn.lpstrFile = cast(buf, c_wchar_p)
    ofn.nMaxFile = 260
    buf.value = "C:\\test\\file.pem"
    assert buf.value == "C:\\test\\file.pem"

check("OPENFILENAMEW 结构体 + cast", test_openfilenamew_struct)


def test_menu_id_no_collision():
    next_id = [1000]
    actions = {}

    def build(items):
        for item in reversed(items):
            if item is None:
                pass
            elif isinstance(item[1], list):
                text, sub_items = item
                build(sub_items)
            else:
                text, cb = item
                mid = next_id[0]
                actions[mid] = cb
                next_id[0] += 1

    build([
        ("上传截图", "upload"),
        None,
        ("切换服务器", [("p1", "sw1"), ("p2", "sw2"), ("p3", "sw3")]),
        None,
        ("设置...", "settings"),
        ("检查更新...", "update"),
        ("打开配置", "open_cfg"),
        ("打开日志", "open_log"),
        None,
        ("退出", "quit"),
    ])

    cbs = list(actions.values())
    assert len(set(cbs)) == len(cbs), f"ID collision! {actions}"
    quit_ids = [i for i, c in actions.items() if c == "quit"]
    assert len(quit_ids) == 1
    for sid in [i for i, c in actions.items() if "sw" in str(c)]:
        assert actions[sid] != "quit"

check("菜单 ID 无碰撞", test_menu_id_no_collision)


def test_folder_watch_e2e():
    mod = sys.modules.get("clip_upload")
    if mod is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("clip_upload", str(APP_SCRIPT))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["clip_upload"] = mod
        spec.loader.exec_module(mod)

    watch_dir = TEST_DIR / "watch"
    watch_dir.mkdir()
    remote_dir = TEST_DIR / "remote"

    profile_cfg = {
        "server": "localhost", "port": 22, "username": "test",
        "password": "test", "ssh_key": "",
        "remote_path": str(remote_dir), "url_prefix": "",
        "clipboard_format": "path", "watch_folder": str(watch_dir),
    }
    global_cfg = {"file_naming": "datetime", "image_format": "png"}

    uploaded = []
    orig_upload = mod.upload_file

    def mock_upload(local_path, cfg, filename):
        remote_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, str(remote_dir / filename))
        uploaded.append(filename)

    mod.upload_file = mock_upload
    try:
        mgr = mod.FolderWatcherManager({
            "active_profile": "test",
            "profiles": {"test": profile_cfg},
            "global": global_cfg,
        })
        mgr.start()
        time.sleep(1)

        (watch_dir / "screenshot.png").write_bytes(b"\x89PNG" + b"\x00" * 200)
        for _ in range(30):
            if uploaded:
                break
            time.sleep(0.5)
        mgr.stop()

        assert len(uploaded) == 1
        assert (watch_dir / "uploaded").exists()
        assert not (watch_dir / "screenshot.png").exists()
    finally:
        mod.upload_file = orig_upload

check("文件夹监控 + 自动上传", test_folder_watch_e2e)


# ══════════════════════════════════════════════════════
#  Part 2: Windows GUI 测试
# ══════════════════════════════════════════════════════
IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    print()
    print("=" * 60)
    print("  Part 2: Windows GUI 测试")
    print("=" * 60)
    print()

    def test_browse_folder_dialog():
        """验证 _win32_browse_folder 弹出对话框不崩溃"""
        mod = sys.modules["clip_upload"]
        result = [None]

        def browse():
            try:
                result[0] = mod._win32_browse_folder("选择监控文件夹")
            except Exception as e:
                result[0] = f"ERROR: {e}"

        t = threading.Thread(target=browse, daemon=True)
        t.start()
        time.sleep(3)

        # 找到对话框并关闭
        try:
            import pywinauto
            app = pywinauto.Application(backend="win32").connect(
                title_re="选择监控文件夹", timeout=5)
            dlg = app.top_window()
            dlg.close()
        except Exception:
            pass

        t.join(timeout=5)
        assert not (isinstance(result[0], str) and result[0].startswith("ERROR")), result[0]

    check("浏览文件夹对话框弹出", test_browse_folder_dialog)

    def test_browse_file_dialog():
        """验证 _win32_open_file_dialog 弹出对话框不崩溃"""
        mod = sys.modules["clip_upload"]
        result = [None]

        def browse():
            try:
                result[0] = mod._win32_open_file_dialog(
                    "选择文件", "所有文件\0*.*\0")
            except Exception as e:
                result[0] = f"ERROR: {e}"

        t = threading.Thread(target=browse, daemon=True)
        t.start()
        time.sleep(3)

        try:
            import pywinauto
            app = pywinauto.Application(backend="win32").connect(
                title_re="选择文件", timeout=5)
            dlg = app.top_window()
            dlg.close()
        except Exception:
            pass

        t.join(timeout=5)
        assert not (isinstance(result[0], str) and result[0].startswith("ERROR")), result[0]

    check("浏览文件对话框弹出", test_browse_file_dialog)

    def test_settings_dialog():
        """验证设置对话框可以打开并显示所有字段"""
        mod = sys.modules["clip_upload"]

        test_cfg = {
            "active_profile": "default",
            "profiles": {"default": dict(mod.PROFILE_FIELDS,
                          server="test.com", username="u", password="p")},
            "global": dict(mod.GLOBAL_FIELDS),
        }

        opened = [False]
        def open_dlg():
            try:
                d = mod.SettingsDialog(test_cfg, on_save=lambda c: None)
                opened[0] = True
                time.sleep(1)
                d.root.destroy()
            except Exception as e:
                opened[0] = f"ERROR: {e}"

        t = threading.Thread(target=open_dlg, daemon=True)
        t.start()
        t.join(timeout=15)
        assert opened[0] is True, f"dialog failed: {opened[0]}"

    check("设置对话框打开", test_settings_dialog)

    def test_welcome_dialog():
        """验证欢迎对话框可以正常显示"""
        mod = sys.modules["clip_upload"]

        cfg = {
            "active_profile": "default",
            "profiles": {"default": dict(mod.PROFILE_FIELDS)},
            "global": dict(mod.GLOBAL_FIELDS),
        }
        merged = mod.get_merged_config(cfg)

        opened = [False]
        def show():
            try:
                mod._show_welcome(cfg, merged)
                opened[0] = True
            except Exception as e:
                opened[0] = f"ERROR: {e}"

        t = threading.Thread(target=show, daemon=True)
        t.start()
        time.sleep(3)

        # 找到欢迎窗口并关闭
        try:
            import pywinauto
            app = pywinauto.Application(backend="win32").connect(
                title_re="Clip Upload v", timeout=5)
            dlg = app.top_window()
            # 点击隐藏到后台按钮
            try:
                dlg["隐藏到后台"].click()
            except Exception:
                dlg.close()
        except Exception:
            pass

        t.join(timeout=5)
        assert opened[0] is True, f"welcome failed: {opened[0]}"

    check("欢迎对话框", test_welcome_dialog)


# ══════════════════════════════════════════════════════
#  清理 + 结果
# ══════════════════════════════════════════════════════
shutil.rmtree(TEST_DIR, ignore_errors=True)

print()
total = PASS + FAIL
print(f"结果: {PASS} passed, {FAIL} failed, {total} total")
if ERRORS:
    print("失败项:")
    for e in ERRORS:
        print(f"  - {e}")

sys.exit(1 if FAIL else 0)
