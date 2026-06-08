# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec - 单文件 exe 打包"""

a = Analysis(
    ['clip_upload.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'PIL._tkinter_finder',
        'keyboard',
        'pystray',
        'win10toast',
        'win32clipboard',
        'win32con',
        'watchdog',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'numpy', 'scipy', 'tkinter.test'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ClipUpload',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=None,
    onefile=True,
)
