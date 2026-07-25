# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — сборка RealtimeTranslator в onedir (ЭТАП 2 установщика).

Собирает: .venv\Scripts\pyinstaller translator.spec --noconfirm

Важно:
- nvidia-пакеты (cublas/cudnn/cuda_nvrtc, ~2-3 ГБ) НЕ включаем — их докачает
  установщик на ЭТАПЕ 3 в %APPDATA%\RealtimeTranslator\cuda\<pkg>\bin
  (см. paths.py и pipeline.py). ctranslate2 несёт свою маленькую cudnn64_9.dll
  сам — это его штатная часть, её исключать не нужно и незачем.
- .env НЕ включаем ни в каком виде — ключи вводятся через KeysDialog при
  первом запуске (пишутся в %APPDATA%\RealtimeTranslator\.env).
"""

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('assets', 'assets'),
        ('knowledge_base', 'knowledge_base'),
    ],
    hiddenimports=[
        'faster_whisper',
        'pyaudiowpatch',
        'av',
        'onnxruntime',
        'tokenizers',
        'huggingface_hub',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # nvidia-пакеты (полновесные CUDA-библиотеки) — не тащим в сборку;
    # докачиваются установщиком отдельно
    excludes=[
        'nvidia',
        'nvidia.cublas',
        'nvidia.cudnn',
        'nvidia.cuda_nvrtc',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='RealtimeTranslator',
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
    icon='assets\\icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='RealtimeTranslator',
)
