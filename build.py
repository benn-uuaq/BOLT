import os
import sys
import subprocess
import platform  # 윈도우 OS 확인용 모듈 추가 완료

# ==============================================================================
# 1. 자동 생성할 SPEC 파일 내용 (소스코드 노출 방지 및 리소스 포함 로직)
# ==============================================================================
SPEC_CONTENT = """# -*- mode: python ; coding: utf-8 -*-
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
    hiddenimports=['serial', 'pyserial', 'PyQt5'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
"""

def main():
    spec_filename = "BOLT_SYSTEM.spec"
    
    print("🚀 [Step 1] 빌드 설정 파일(Spec) 자동 생성 중...")
    try:
        # 항상 최신 내용으로 spec 파일을 덮어씁니다.
        with open(spec_filename, "w", encoding="utf-8") as f:
            f.write(SPEC_CONTENT)
        print(f"✅ '{spec_filename}' 생성 완료.")
    except Exception as e:
        print(f"❌ Spec 파일 생성 실패: {e}")
        return

    print("\n📦 [Step 2] PyInstaller 패키징 진행 중... (시간이 조금 걸립니다)")
    try:
        # python -m PyInstaller를 사용하여 환경 변수 오류 원천 차단
        subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", spec_filename], 
            check=True
        )
    except subprocess.CalledProcessError as e:
        print(f"❌ 빌드 중 오류가 발생했습니다: {e}")
        return

    print("\n🔒 [Step 3] _internal 폴더 숨김 처리 중...")
    target_dir = os.path.abspath(os.path.join("dist", "BOLT_SYSTEM", "_internal"))
    
    if os.path.exists(target_dir):
        if platform.system() == "Windows":
            try:
                subprocess.run(["attrib", "+h", target_dir], shell=True, check=True)
                print(f"✅ '_internal' 폴더 숨김 처리 완료.")
            except subprocess.CalledProcessError as e:
                print(f"⚠️ 경고: 폴더 숨김 처리 중 오류 발생: {e}")
    else:
        print(f"⚠️ 경고: '{target_dir}' 폴더를 찾을 수 없습니다.")

    print("\n🎉 모든 빌드 및 보안 작업이 완벽하게 완료되었습니다!")
    print("👉 'dist/BOLT_SYSTEM' 폴더로 이동하여 실행 파일(.exe)을 확인하세요.")

if __name__ == "__main__":
    main()