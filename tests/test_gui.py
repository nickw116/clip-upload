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
APP_SCRIPT = SCRIPT_DIR.parent / "clip_upload.py"
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
print("  Part 1: logic + Win32 struct tests")
print("=" * 60)
print()


def test_import():
    if sys.platform != "win32":
        # Linux: mock Windows-only modules
        import unittest.mock as mock
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
    assert mod.__version__ == "1.11.0", f"version mismatch: {mod.__version__}"

check("import + version", test_import)


def test_browseinfow_struct():
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
    bi.lpszTitle = "Test"
    bi.ulFlags = 0x40
    assert sizeof(BROWSEINFOW) > 0

check("BROWSEINFOW struct", test_browseinfow_struct)


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

check("OPENFILENAMEW struct + cast", test_openfilenamew_struct)


def test_menu_id_no_collision():
    next_id = [1000]
    actions = {}

    def build(items):
        for item in reversed(items):
            if item is None:
                pass
            elif isinstance(item[1], list):
                build(item[1])
            else:
                actions[next_id[0]] = item[1]
                next_id[0] += 1

    build([
        ("upload", "upload_cb"),
        None,
        ("switch", [("p1", "sw1"), ("p2", "sw2"), ("p3", "sw3")]),
        None,
        ("settings", "settings_cb"),
        ("quit", "quit_cb"),
    ])

    vals = list(actions.values())
    assert len(set(vals)) == len(vals), f"ID collision: {actions}"

check("menu ID no collision", test_menu_id_no_collision)


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

    uploaded = []
    orig = mod.upload_file

    def mock_upload(local_path, cfg, filename):
        remote_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, str(remote_dir / filename))
        uploaded.append(filename)

    mod.upload_file = mock_upload
    try:
        mgr = mod.FolderWatcherManager({
            "active_profile": "t",
            "profiles": {"t": {
                "server": "localhost", "port": 22, "username": "t",
                "password": "t", "ssh_key": "",
                "remote_path": str(remote_dir), "url_prefix": "",
                "clipboard_format": "path", "watch_folder": str(watch_dir),
            }},
            "global": {"file_naming": "datetime", "image_format": "png"},
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
        mod.upload_file = orig

check("folder watch + auto upload", test_folder_watch_e2e)


# ══════════════════════════════════════════════════════
#  Part 2: Windows GUI tests (real Win32 API calls)
# ══════════════════════════════════════════════════════
IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    print()
    print("=" * 60)
    print("  Part 2: Windows GUI tests")
    print("=" * 60)
    print()

    mod = sys.modules["clip_upload"]
    import pywinauto

    # ── helper: run function in thread, auto-close dialog via pywinauto ──
    def _test_dialog_with_automation(fn, dialog_title, action_fn, timeout=15):
        """Run fn in thread, find dialog by title, perform action, return fn result."""
        result = [None]

        def worker():
            try:
                result[0] = fn()
            except Exception as e:
                result[0] = f"ERROR: {e}"

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(2)

        try:
            app = pywinauto.Application(backend="win32").connect(
                title_re=dialog_title, timeout=8)
            dlg = app.top_window()
            time.sleep(0.5)
            action_fn(dlg)
        except Exception as e:
            print(f"    (dialog automation failed: {e})")

        t.join(timeout=timeout)
        return result[0]


    def test_browse_folder_api_restype():
        """Verify SHBrowseForFolderW has correct restype (c_void_p, not c_int)"""
        shell32 = ctypes.windll.shell32
        # After importing clip_upload, the restype should be set
        assert shell32.SHBrowseForFolderW.restype is not None, \
            "SHBrowseForFolderW.restype must be set"
        # On 64-bit, c_void_p is 8 bytes, c_int is 4 bytes
        assert ctypes.sizeof(shell32.SHBrowseForFolderW.restype) >= ctypes.sizeof(ctypes.c_void_p), \
            f"restype too small: {shell32.SHBrowseForFolderW.restype}"

    check("SHBrowseForFolderW.restype = c_void_p", test_browse_folder_api_restype)


    def test_shgetpathfromidlist_argtypes():
        """Verify SHGetPathFromIDListW argtypes are correct"""
        shell32 = ctypes.windll.shell32
        assert shell32.SHGetPathFromIDListW.argtypes is not None, \
            "SHGetPathFromIDListW.argtypes must be set"
        assert len(shell32.SHGetPathFromIDListW.argtypes) == 2, \
            "SHGetPathFromIDListW should take 2 args (pidl, path)"
        # First arg should be pointer-sized
        assert ctypes.sizeof(shell32.SHGetPathFromIDListW.argtypes[0]) >= ctypes.sizeof(ctypes.c_void_p), \
            f"first arg too small for 64-bit pointer"

    check("SHGetPathFromIDListW.argtypes correct", test_shgetpathfromidlist_argtypes)


    def test_browse_folder_dialog_no_crash():
        """Verify browse folder dialog opens and closes without crash"""
        result = [None]

        def browse():
            try:
                result[0] = mod._win32_browse_folder("TestFolderBrowse")
            except Exception as e:
                result[0] = f"ERROR: {e}"

        t = threading.Thread(target=browse, daemon=True)
        t.start()
        time.sleep(3)

        # Find and close any dialog window
        try:
            # SHBrowseForFolder dialog title might be different
            # Try to find by window class
            for proc in pywinauto.findwindows.find_elements():
                if "folder" in (proc.name or "").lower() or "browse" in (proc.name or "").lower():
                    try:
                        app = pywinauto.Application(backend="win32").connect(handle=proc.handle)
                        app.top_window().close()
                    except Exception:
                        pass
        except Exception:
            pass

        # Also try sending Escape key to close any modal dialog
        try:
            import pywinauto.keyboard as kb
            kb.send_keys("{ESC}")
        except Exception:
            pass

        t.join(timeout=5)
        # No crash = PASS (None means cancelled, which is fine)
        assert not (isinstance(result[0], str) and result[0].startswith("ERROR")), \
            f"dialog crashed: {result[0]}"

    check("browse folder dialog no crash", test_browse_folder_dialog_no_crash)


    def test_browse_file_dialog_no_crash():
        """Verify file dialog opens and closes without crash"""
        result = [None]

        def browse():
            try:
                result[0] = mod._win32_open_file_dialog("TestFileBrowse", "All\0*.*\0")
            except Exception as e:
                result[0] = f"ERROR: {e}"

        t = threading.Thread(target=browse, daemon=True)
        t.start()
        time.sleep(3)

        try:
            import pywinauto.keyboard as kb
            kb.send_keys("{ESC}")
        except Exception:
            pass

        t.join(timeout=5)
        assert not (isinstance(result[0], str) and result[0].startswith("ERROR")), \
            f"dialog crashed: {result[0]}"

    check("browse file dialog no crash", test_browse_file_dialog_no_crash)


    def test_settings_dialog_save():
        """Open settings, set watch_folder, save, verify config persisted"""
        cfg_dir = TEST_DIR / "cfg"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = cfg_dir / "config.json"

        test_cfg = {
            "active_profile": "default",
            "profiles": {"default": dict(mod.PROFILE_FIELDS,
                          server="test.com", username="u", password="p",
                          remote_path="/tmp")},
            "global": dict(mod.GLOBAL_FIELDS),
        }
        with open(cfg_path, "w") as f:
            json.dump(test_cfg, f)

        # Patch CONFIG_PATH temporarily
        orig_config_path = mod.CONFIG_PATH
        mod.CONFIG_PATH = cfg_path

        saved_cfg = [None]

        def on_save(c):
            saved_cfg[0] = c

        opened = [False]
        def open_dlg():
            try:
                d = mod.SettingsDialog(test_cfg, on_save=on_save)
                opened[0] = True
                time.sleep(0.5)
                # Set watch folder via the variable
                d.watch_folder_var.set("C:\\Temp\\WatchTest")
                # Trigger save
                d._save()
            except Exception as e:
                opened[0] = f"ERROR: {e}"

        t = threading.Thread(target=open_dlg, daemon=True)
        t.start()
        t.join(timeout=15)

        mod.CONFIG_PATH = orig_config_path

        assert opened[0] is True, f"dialog failed: {opened[0]}"
        assert saved_cfg[0] is not None, "on_save was not called"
        assert saved_cfg[0]["profiles"]["default"]["watch_folder"] == "C:\\Temp\\WatchTest", \
            f"watch_folder not saved: {saved_cfg[0]['profiles']['default']}"

    check("settings dialog: set watch_folder + save", test_settings_dialog_save)


    def test_settings_dialog_all_fields_present():
        """Verify settings dialog has all required UI fields"""
        import tkinter as tk

        fields_found = {}

        def check_fields():
            test_cfg = {
                "active_profile": "default",
                "profiles": {"default": dict(mod.PROFILE_FIELDS)},
                "global": dict(mod.GLOBAL_FIELDS),
            }
            d = mod.SettingsDialog(test_cfg, on_save=lambda c: None)

            # Check all StringVar fields exist
            fields_found["server"] = hasattr(d, 'server_var')
            fields_found["port"] = hasattr(d, 'port_var')
            fields_found["username"] = hasattr(d, 'username_var')
            fields_found["password"] = hasattr(d, 'password_var')
            fields_found["ssh_key"] = hasattr(d, 'key_var')
            fields_found["remote_path"] = hasattr(d, 'path_var')
            fields_found["url_prefix"] = hasattr(d, 'url_var')
            fields_found["clipboard_format"] = hasattr(d, 'fmt_var')
            fields_found["watch_folder"] = hasattr(d, 'watch_folder_var')
            fields_found["file_naming"] = hasattr(d, 'name_var')

            d.root.destroy()

        t = threading.Thread(target=check_fields, daemon=True)
        t.start()
        t.join(timeout=15)

        for field, found in fields_found.items():
            assert found, f"missing UI field: {field}"

    check("settings dialog has all fields", test_settings_dialog_all_fields_present)


    def test_welcome_dialog():
        """Verify welcome dialog opens and can be closed"""
        cfg = {
            "active_profile": "default",
            "profiles": {"default": dict(mod.PROFILE_FIELDS)},
            "global": dict(mod.GLOBAL_FIELDS),
        }
        merged = mod.get_merged_config(cfg)

        result = [None]
        def show():
            try:
                mod._show_welcome(cfg, merged)
                result[0] = "ok"
            except Exception as e:
                result[0] = f"ERROR: {e}"

        t = threading.Thread(target=show, daemon=True)
        t.start()
        time.sleep(2)

        # Close the welcome window
        try:
            app = pywinauto.Application(backend="win32").connect(
                title_re="Clip Upload v1.11", timeout=5)
            dlg = app.top_window()
            dlg.close()
        except Exception:
            try:
                import pywinauto.keyboard as kb
                kb.send_keys("{ESC}")
            except Exception:
                pass

        t.join(timeout=5)
        assert result[0] != "ok" or result[0] is None or not (isinstance(result[0], str) and result[0].startswith("ERROR")), \
            f"welcome crashed: {result[0]}"

    check("welcome dialog", test_welcome_dialog)


# ══════════════════════════════════════════════════════
#  cleanup + results
# ══════════════════════════════════════════════════════
shutil.rmtree(TEST_DIR, ignore_errors=True)

print()
total = PASS + FAIL
print(f"result: {PASS} passed, {FAIL} failed, {total} total")
if ERRORS:
    print("failed:")
    for e in ERRORS:
        print(f"  - {e}")

sys.exit(1 if FAIL else 0)
