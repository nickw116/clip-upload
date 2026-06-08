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
    assert mod.__version__ == "1.10.5", f"version mismatch: {mod.__version__}"

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


    def test_browse_folder_returns_path():
        """Select a real folder and verify the path is returned"""
        target_dir = os.environ.get("TEMP", "C:\\Windows\\Temp")
        selected_path = [None]

        def select_folder(dlg):
            """Navigate to target folder in the browse dialog tree and click OK"""
            try:
                # The folder browse dialog has a tree view
                # Try to find the edit/toolbar and type a path
                # New-style dialog (BIF_NEWDIALOGSTYLE) has a text field
                # Try clicking OK directly (desktop folder is selected by default)
                ok_btn = dlg.child_window(title="OK", control_type="Button")
                ok_btn.click()
            except Exception:
                try:
                    dlg.OK.click()
                except Exception:
                    dlg.close()

        def browse():
            return mod._win32_browse_folder("SelectFolderTest")

        r = _test_dialog_with_automation(browse, "SelectFolderTest", select_folder)

        # Should return a non-None, non-empty string (desktop or some folder)
        assert r is not None, "browse returned None - user selected folder but got no path"
        assert isinstance(r, str), f"expected str, got {type(r)}: {r}"
        assert len(r) > 0, "browse returned empty string"
        assert "\\" in r or "/" in r, f"not a valid path: {r}"

    check("browse folder dialog returns real path", test_browse_folder_returns_path)


    def test_browse_file_dialog_returns_path():
        """Open file dialog, select a real file, verify path is returned"""
        test_file = os.path.join(os.environ.get("TEMP", "C:\\Windows\\Temp"), "clip_test_file.txt")
        with open(test_file, "w") as f:
            f.write("test")

        def select_file(dlg):
            try:
                # Type the file path in the filename field
                combo = dlg.child_window(class_name="Edit")
                combo.set_text(test_file)
                time.sleep(0.3)
                ok_btn = dlg.child_window(title="&Open", control_type="Button")
                ok_btn.click()
            except Exception:
                try:
                    dlg.child_window(title="Open").click()
                except Exception:
                    dlg.close()

        def browse():
            return mod._win32_open_file_dialog("SelectFileTest", "All\0*.*\0")

        r = _test_dialog_with_automation(browse, "SelectFileTest", select_file)

        assert r is not None, "file dialog returned None"
        assert isinstance(r, str), f"expected str, got {type(r)}: {r}"
        assert "clip_test_file.txt" in r, f"expected file path, got: {r}"

        # cleanup
        try:
            os.unlink(test_file)
        except Exception:
            pass

    check("browse file dialog returns real path", test_browse_file_dialog_returns_path)


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
            fields_found["hotkey"] = hasattr(d, 'hotkey_var')

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

        opened = [False]
        def show():
            try:
                mod._show_welcome(cfg, merged)
                opened[0] = True
            except Exception as e:
                opened[0] = f"ERROR: {e}"

        t = threading.Thread(target=show, daemon=True)
        t.start()
        time.sleep(2)

        try:
            app = pywinauto.Application(backend="win32").connect(
                title_re="Clip Upload v1.10", timeout=5)
            dlg = app.top_window()
            # Click the hide button
            try:
                btn = dlg.child_window(title_re=".*")
                for child in dlg.descendants():
                    if hasattr(child, 'window_text') and 'background' in str(child.window_text()).lower():
                        child.click()
                        break
                else:
                    dlg.close()
            except Exception:
                dlg.close()
        except Exception:
            pass

        t.join(timeout=5)
        assert opened[0] is True, f"welcome failed: {opened[0]}"

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
