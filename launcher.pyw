"""Windowed entry point: also usable directly when Python is installed."""
from pathlib import Path
import os
import sys
import traceback


def main():
    try:
        from adkm85 import main as run
        run()
    except Exception as exc:
        detail = traceback.format_exc()
        log_path = None
        try:
            directory = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'ADKM85'
            directory.mkdir(parents=True, exist_ok=True)
            log_path = directory / 'startup-error.log'
            log_path.write_text(detail, encoding='utf-8')
        except OSError:
            pass
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk._default_root
            own_root = root is None
            if own_root:
                root = tk.Tk()
                root.withdraw()
            if isinstance(exc, ModuleNotFoundError) and not getattr(sys, 'frozen', False):
                message = (f'В выбранном Python не установлен модуль {exc.name}.\n\n'
                           'Открой готовый ADKM85.exe: в нём уже есть все зависимости.\n'
                           'Для запуска исходников установи пакеты из requirements.txt.')
            else:
                message = f'Не удалось запустить АДКМ-85.\n\n{exc}'
            if log_path:
                message += f'\n\nПодробности сохранены:\n{log_path}'
            messagebox.showerror('АДКМ-85 — ошибка запуска', message, parent=root)
            if own_root:
                root.destroy()
        except Exception:
            # Last resort when even Tk is unavailable; no console is needed on Windows.
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f'Не удалось запустить АДКМ-85.\n{exc}', 'АДКМ-85', 0x10)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
