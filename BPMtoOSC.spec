# -*- mode: python ; coding: utf-8 -*-
# Build with:  pyinstaller --noconfirm BPMtoOSC.spec
import os
extra_binaries = [('sckaudio', '.')] if os.path.exists('sckaudio') else []

a = Analysis(
    ['main_gui.py'],
    pathex=[],
    binaries=extra_binaries,
    datas=[('pics', 'pics')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='BPMtoOSC',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='BPMtoOSC',
)
app = BUNDLE(
    coll,
    name='BPMtoOSC RXv2 Mossad Spyware.app',
    icon='AppIcon.icns',
    bundle_identifier='com.bpmtoosc.app',
    info_plist={
        'CFBundleName': 'BPMtoOSC RXv2 Mossad Spyware',
        'CFBundleDisplayName': 'BPMtoOSC RXv2 Mossad Spyware',
        'CFBundleShortVersionString': '2.0',
        'NSMicrophoneUsageDescription': 'BPMtoOSC listens to an audio input to detect the BPM.',
        'NSAudioCaptureUsageDescription': 'BPMtoOSC listens to the computer sound to detect the BPM.',
        'NSHighResolutionCapable': True,
        'NSRequiresAquaSystemAppearance': True,
    },
)
