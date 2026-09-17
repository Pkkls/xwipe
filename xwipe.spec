# PyInstaller spec — XWipe, executable Windows autonome, un seul fichier.
# Build : python -m PyInstaller xwipe.spec --noconfirm
# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # N'exclure QUE des paquets tiers surement absents du chemin d'execution.
    # Surtout pas de modules stdlib : urllib.request depend de email et
    # http.client, les exclure casse l'import au demarrage.
    excludes=["numpy", "pandas", "PIL", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="XWipe",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # application fenetree, pas de console noire
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version="version_info.txt",
)
