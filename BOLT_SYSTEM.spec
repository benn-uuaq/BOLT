# -*- mode: python ; coding: utf-8 -*-
import os

def get_data_files(src_dir, dest_dir):
    files = []
    if not os.path.exists(src_dir):
        return files
        
    for root, dirs, filenames in os.walk(src_dir):
        for filename in filenames:
            # 소스코드(.py, .pyc, .ui) 및 캐시 폴더는 배포에서 제외 (보안)
            if not filename.endswith('.py') and not filename.endswith('.pyc') and not filename.endswith('.ui') and '__pycache__' not in root:
                full_path = os.path.join(root, filename)
                relative_path = os.path.relpath(root, src_dir)
                if relative_path == '.':
                    target_dir = dest_dir
                else:
                    target_dir = os.path.join(dest_dir, relative_path)
                
                files.append((full_path, target_dir))
    return files

block_cipher = None

my_datas = []

# 기본 루트 파일들 안전하게 추가
if os.path.exists('logo.ico'):
    my_datas.append(('logo.ico', '.'))
if os.path.exists('back_points.png'):
    my_datas.append(('back_points.png', '.'))
if os.path.exists('head_points.png'):
    my_datas.append(('head_points.png', '.'))
if os.path.exists('logo.png'):
    my_datas.append(('logo.png', '.'))
if os.path.exists('requirements.txt'):
    my_datas.append(('requirements.txt', '.'))

# DB 폴더의 데이터 파일들만 추가 (.py 제외)
my_datas += get_data_files('DB', 'DB')

a = Analysis(
    ['bolt_main.py'],  # ★ 프로젝트의 실제 메인 실행 파일 이름
    pathex=[],
    binaries=[],
    datas=my_datas,
    hiddenimports=['serial', 'PyQt5'],   # 'pyserial'은 패키지명(모듈명은 serial)이라 제거
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 이 PC에 설치된 다른 Qt 바인딩/대형 패키지가 딸려 들어오는 것 차단
    # (PyQt5 + PySide6 동시 수집 시 PyInstaller가 빌드를 중단함)
    excludes=[
        'PySide6', 'PySide2', 'PyQt6', 'shiboken6', 'shiboken2',
        'torch', 'torchvision', 'torchaudio', 'tensorflow', 'tensorboard',
        'IPython', 'jedi', 'parso', 'zmq', 'nbformat', 'jupyter', 'jupyter_client', 'ipykernel', 'notebook',
        'tkinter', '_tkinter',
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
    name='BOLT_SYSTEM',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='logo.ico' if os.path.exists('logo.ico') else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='BOLT_SYSTEM',
)
