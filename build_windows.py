"""Build the Windows application with: python build_windows.py"""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import argparse

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', default=str(ROOT / 'release'))
    parser.add_argument('--portable', action='store_true', help='Also create a portable folder and ZIP')
    args = parser.parse_args()
    output = Path(args.output_dir).resolve()
    # Keep build caches local; application runtime does not need this directory.
    env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(ROOT / '.build-cache'))
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
        '--name', 'ADKM85', '--icon', str(ROOT / 'assets' / 'adkm85.ico'),
        '--add-data', f'{ROOT / "texts.txt"}:.',
        '--add-data', f'{ROOT / "assets" / "adkm85.ico"}:assets',
        '--specpath', str(ROOT / 'build'), '--workpath', str(ROOT / 'build' / 'work'),
        '--distpath', str(output), str(ROOT / 'launcher.pyw'),
    ], cwd=ROOT, env=env, check=True)
    print('Ready:', output / 'ADKM85.exe')
    if args.portable:
        portable = output / 'ADKM85-portable'
        portable.mkdir(exist_ok=True)
        shutil.copy2(output / 'ADKM85.exe', portable / 'ADKM85.exe')
        shutil.copy2(ROOT / 'texts.txt', portable / 'texts.txt')
        shutil.copy2(ROOT / 'README.md', portable / 'README.md')
        shutil.make_archive(str(output / 'ADKM85-portable'), 'zip', portable)
        print('Archive:', output / 'ADKM85-portable.zip')


if __name__ == '__main__':
    main()
