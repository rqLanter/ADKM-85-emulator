import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
import threading
import queue
import random
import time
import sys
import uuid
from dataclasses import asdict
from radiogram import Settings, build_plan, render_plan
from learning import ProgressStore, score_result, compare_answer
from bisect import bisect_right
import numpy as np
import sounddevice as sd

MORSE = {
    'А': '.-', 'Б': '-...', 'В': '.--', 'Г': '--.', 'Д': '-..', 'Е': '.',
    'Ж': '...-', 'З': '--..', 'И': '..', 'Й': '.---', 'К': '-.-', 'Л': '.-..',
    'М': '--', 'Н': '-.', 'О': '---', 'П': '.--.', 'Р': '.-.', 'С': '...',
    'Т': '-', 'У': '..-', 'Ф': '..-.', 'Х': '....', 'Ц': '-.-.', 'Ч': '---.',
    'Ш': '----', 'Щ': '--.-', 'Ъ': '--.--', 'Ы': '-.--', 'Ь': '-..-',
    'Э': '..-..', 'Ю': '..--', 'Я': '.-.-', 'Ё': '.',
}
def load_rows():
    
    path = Path(__file__).with_name('texts.txt')
    if getattr(sys, 'frozen', False):
        external = Path(sys.executable).with_name('texts.txt')
        if external.exists():
            path = external
    result = {}
    for line_number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        try:
            number, content = line.split(';', 1)
            number = int(number.strip())
            content = ''.join(content.upper().split())
            if number < 1 or number in result or len(content) != 20 or any(c not in MORSE for c in content):
                raise ValueError('ожидается уникальный номер и 20 русских букв')
        except ValueError as exc:
            raise ValueError(f'texts.txt, строка {line_number}: {exc}') from exc
        result[number] = content
    if not result:
        raise ValueError('В texts.txt нет упражнений')
    return dict(sorted(result.items()))


def grouped(text):
    return ' '.join(text[i:i + 5] for i in range(0, len(text), 5))


def generate_training(alphabet, length, rng=None):
    letters = ''.join(dict.fromkeys(''.join(alphabet.upper().split())))
    if not letters or any(letter not in MORSE for letter in letters):
        raise ValueError('Введи русские буквы, например АОКМ. Пробелы разрешены.')
    if length not in (1, 5, 10, 20):
        raise ValueError('Выбери длину: 1, 5, 10 или 20 букв.')
    return ''.join((rng or random).choices(letters, k=length))


def make_audio(text, wpm, tone_hz, noise_percent, stop_event, marks=None):
    sample_rate = 22050
    dot = 1.2 / wpm
    parts = []
    frames = 0

    def silence(units):
        nonlocal frames
        count = max(1, round(sample_rate * dot * units))
        parts.append(np.zeros(count, dtype=np.float32))
        frames += count

    def beep(units):
        nonlocal frames
        count = max(1, round(sample_rate * dot * units))
        t = np.arange(count, dtype=np.float32) / sample_rate
        wave = np.sin(2 * np.pi * tone_hz * t)
        edge = min(round(sample_rate * 0.005), count // 2)
        if edge:
            ramp = np.linspace(0, 1, edge, dtype=np.float32)
            wave[:edge] *= ramp
            wave[-edge:] *= ramp[::-1]
        parts.append((0.5 * wave).astype(np.float32))
        frames += count

    words = text.upper().split(' ')
    for wi, word in enumerate(words):
        for ci, letter in enumerate(word):
            if stop_event.is_set():
                return None
            code = MORSE.get(letter)
            if not code:
                continue
            for si, symbol in enumerate(code):
                start = frames
                beep(1 if symbol == '.' else 3)
                if marks is not None:
                    marks.append((start, frames, letter, si))
                if si < len(code) - 1:
                    silence(1)
            if ci < len(word) - 1:
                silence(3)
        if wi < len(words) - 1:
            silence(7)

    if not parts:
        return None
    audio = np.concatenate(parts)
    if noise_percent > 0:
        rng = np.random.default_rng()
        audio += rng.normal(0, 0.25 * noise_percent / 100, len(audio)).astype(np.float32)
    return np.clip(audio, -1, 1)



def stream_audio(text, wpm, tone, noise, stop_event, events):
    error = None
    try:
        marks = []
        audio = make_audio(text, wpm, tone, noise, stop_event, marks)
        if audio is not None and not stop_event.is_set():
            play_samples(audio, marks, stop_event, events)
    except Exception as exc:
        error = str(exc)
    finally:
        events.put(('done', error))


def play_samples(audio, marks, stop_event, events):
    cursor = 0
    finished = threading.Event()

    def callback(outdata, frames, clock, status):
        nonlocal cursor
        outdata.fill(0)
        if stop_event.is_set():
            raise sd.CallbackAbort
        count = min(frames, len(audio) - cursor)
        outdata[:count, 0] = audio[cursor:cursor + count]
        if cursor == 0:
            # Anchor the display to the device's scheduled output time.
            started = time.monotonic() + clock.outputBufferDacTime - clock.currentTime
            events.put(('playing', (started, len(audio) / 22050, marks)))
        cursor += count
        if cursor == len(audio):
            raise sd.CallbackStop

    with sd.OutputStream(samplerate=22050, channels=1, dtype='float32',
                         callback=callback, finished_callback=finished.set) as stream:
        while not finished.wait(0.02):
            if stop_event.is_set():
                stream.abort()
                break


class App:
    BG = '#101923'
    CARD = '#1a2735'
    INK = '#edf3f8'
    MUTED = '#9eb0c4'
    ACCENT = '#5ee0bd'

    def __init__(self, root, store=None):
        self.root = root
        root.title('АДКМ-85 • Приём на слух')
        icon = Path(__file__).with_name('assets') / 'adkm85.ico'
        if icon.exists():
            root.iconbitmap(str(icon))
        root.geometry('1080x780')
        root.minsize(960, 740)
        root.configure(bg=self.BG)
        self.store = store if store is not None else ProgressStore()
        self.attempt_id = uuid.uuid4().hex
        self.playback_attempt = None
        self.listened = False
        self.hints_used = False
        self.attempt_saved = False
        self.exam_state = 'idle'
        self.exam_number = None
        self.exam_text = ''
        self.practice_kind = 'training'
        self.rows = load_rows()
        self.numbers = list(self.rows)
        self.stop_event = threading.Event()
        self.events = queue.Queue()
        self.worker = None
        self.closed = False
        self.playing_since = None
        self.duration = 0
        self.marks = []
        self.mark_starts = []
        self.hint_state = None
        self.training_text = generate_training(''.join(MORSE), 20)
        self.mode = 'table'
        self.radio_settings = Settings()
        self.transmission_kind = tk.StringVar(value='Стандартный Морзе')
        self.start_signal = tk.BooleanVar(value=False)
        self.end_signal = tk.BooleanVar(value=False)
        self.playback_expected = None
        self.alphabet = tk.StringVar(value=''.join(MORSE))
        self.training_length = tk.StringVar(value='20')
        self.show_morse = tk.BooleanVar(value=False)
        self.row_number = tk.StringVar(value=str(self.numbers[0]))
        self.speed = tk.IntVar(value=20)
        self.tone = tk.IntVar(value=700)
        self.noise = tk.IntVar(value=0)
        self.show_text = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='Готов к приёму')
        self.counter = tk.StringVar(value='0 / 20 букв')
        self.progress = tk.DoubleVar(value=0)
        self.configure_styles()
        self.build_ui()
        self.row_number.trace_add('write', self.select_exercise)
        self.show_morse.trace_add('write', self.hint_changed)
        self.show_text.trace_add('write', self.hint_changed)
        self.answer.bind('<<Modified>>', self.answer_changed)
        self.answer.bind('<Control-Return>', lambda _: (self.check(), 'break')[1])
        root.bind('<F5>', lambda _: self.start())
        root.bind('<Escape>', lambda _: self.stop())
        root.bind('<Control-Return>', lambda _: self.check())
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.update_preview()
        self.draw_morse()
        self.poll_id = root.after(15, self.poll_audio)

    def configure_styles(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.', font=('Segoe UI', 11), background=self.BG, foreground=self.INK)
        style.configure('TFrame', background=self.BG)
        style.configure('Card.TFrame', background=self.CARD)
        style.configure('TLabel', background=self.BG, foreground=self.INK)
        style.configure('Card.TLabel', background=self.CARD)
        style.configure('Muted.TLabel', background=self.BG, foreground=self.MUTED)
        style.configure('Hint.TLabel', background=self.CARD, foreground=self.MUTED, font=('Segoe UI', 10))
        style.configure('Title.TLabel', font=('Segoe UI', 28, 'bold'))
        style.configure('Heading.TLabel', background=self.CARD, font=('Segoe UI', 14, 'bold'))
        style.configure('TButton', background='#2b3d50', foreground=self.INK, borderwidth=0, padding=(13, 9))
        style.map('TButton', background=[('active', '#39516a'), ('disabled', '#23313f')],
                  foreground=[('disabled', '#73859a')])
        style.configure('Accent.TButton', background=self.ACCENT, foreground='#102a26', font=('Segoe UI', 11, 'bold'))
        style.map('Accent.TButton', background=[('active', '#88efd3'), ('disabled', '#315b54')],
                  foreground=[('disabled', '#9eb0c4')])
        style.configure('TNotebook', background=self.CARD, borderwidth=0)
        style.configure('TNotebook.Tab', background='#2b3d50', foreground=self.INK, padding=(7, 7))
        style.map('TNotebook.Tab', background=[('selected', '#315b54')], foreground=[('selected', self.ACCENT)])
        style.configure('TEntry', fieldbackground='#101e2c', foreground=self.INK, insertcolor=self.ACCENT)
        style.configure('TCheckbutton', background=self.CARD, foreground=self.INK, padding=4)
        style.map('TCheckbutton', background=[('active', self.CARD)])
        style.configure('TCombobox', fieldbackground='#101e2c', background='#2b3d50',
                        foreground=self.INK, arrowcolor=self.ACCENT, padding=7)
        style.map('TCombobox', fieldbackground=[('readonly', '#101e2c')], foreground=[('readonly', self.INK)])
        self.root.option_add('*TCombobox*Listbox.background', self.CARD)
        self.root.option_add('*TCombobox*Listbox.foreground', self.INK)
        self.root.option_add('*TCombobox*Listbox.selectBackground', '#315b54')
        style.configure('Treeview', background='#101e2c', fieldbackground='#101e2c', foreground=self.INK, rowheight=29)
        style.map('Treeview', background=[('selected', '#315b54')], foreground=[('selected', self.INK)])
        style.configure('Treeview.Heading', background='#2b3d50', foreground=self.INK, padding=6)
        style.configure('TProgressbar', troughcolor='#101e2c', background=self.ACCENT,
                        borderwidth=0, thickness=5)

    def label(self, parent, text, style='Card.TLabel', **kwargs):
        widget = ttk.Label(parent, text=text, style=style, **kwargs)
        return widget

    def build_ui(self):
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill='both', expand=True)
        header = ttk.Frame(outer)
        header.pack(fill='x', pady=(0, 10))
        self.label(header, 'АДКМ-85', 'Title.TLabel').pack(side='left')
        self.label(header, 'ПРИЁМ МОРЗЕ', 'Muted.TLabel').pack(side='left', padx=20, pady=(12, 0))
        self.radio_button = ttk.Button(header, text='Радиограмма', command=self.open_radiogram)
        self.radio_button.pack(side='right', padx=(8, 0))
        self.alphabet_button = ttk.Button(header, text='Азбука', command=self.open_alphabet)
        self.alphabet_button.pack(side='right', padx=(8, 0))
        ttk.Button(header, text='Мои ошибки', command=self.open_errors).pack(side='right')

        content = ttk.Frame(outer)
        content.pack(fill='both', expand=True)
        content.columnconfigure(1, weight=1)
        content.rowconfigure(0, weight=1)
        left = ttk.Frame(content, style='Card.TFrame', padding=20, width=284)
        left.grid(row=0, column=0, sticky='ns', padx=(0, 18))
        left.grid_propagate(False)
        left.columnconfigure(0, weight=1)
        self.mode_tabs = ttk.Notebook(left, height=220)
        self.mode_tabs.grid(row=0, column=0, sticky='ew', pady=(0, 14))
        table = ttk.Frame(self.mode_tabs, style='Card.TFrame', padding=(0, 6))
        table.columnconfigure(0, weight=1)
        training = ttk.Frame(self.mode_tabs, style='Card.TFrame', padding=(0, 6))
        self.mode_tabs.add(table, text='Таблица')
        self.mode_tabs.add(training, text='Тренировка')
        exam = ttk.Frame(self.mode_tabs, style='Card.TFrame', padding=(0, 6))
        self.mode_tabs.add(exam, text='Экзамен')
        self.label(exam, '20 букв · одно прослушивание', 'Hint.TLabel').pack(anchor='w')
        self.label(exam, 'Оригинал и подсказки скрыты.\nСтоп завершает попытку без оценки.\n\nОценки: 5 ≥ 95%, 4 ≥ 85%,\n3 ≥ 70%, иначе 2.', 'Hint.TLabel', wraplength=230).pack(anchor='w', pady=8)
        ttk.Button(exam, text='Новый экзамен', command=self.new_exam, style='Accent.TButton').pack(fill='x', pady=6)
        self.selector = ttk.Combobox(table, textvariable=self.row_number, values=self.numbers,
                                     state='readonly', font=('Segoe UI', 20, 'bold'), width=8, height=12)
        self.selector.grid(row=2, column=0, sticky='ew')
        navigation = ttk.Frame(table, style='Card.TFrame')
        navigation.grid(row=3, column=0, sticky='ew', pady=10)
        navigation.columnconfigure((0, 1), weight=1)
        ttk.Button(navigation, text='← Назад', command=lambda: self.navigate(-1)).grid(row=0, column=0, sticky='ew', padx=(0, 4))
        ttk.Button(navigation, text='Далее →', command=lambda: self.navigate(1)).grid(row=0, column=1, sticky='ew', padx=(4, 0))
        ttk.Button(table, text='Случайное упражнение', command=self.random_exercise).grid(row=4, column=0, sticky='ew')
        ttk.Button(table, text=f'Все {len(self.rows)} номеров', command=self.open_library).grid(row=5, column=0, sticky='ew', pady=(8, 0))
        self.label(training, 'Буквы · например АОКМ', 'Hint.TLabel').pack(anchor='w', pady=(0, 7))
        self.alphabet_entry = ttk.Entry(training, textvariable=self.alphabet, width=20, font=('Consolas', 12))
        self.alphabet_entry.pack(fill='x')
        self.alphabet_entry.bind('<Return>', lambda _: self.new_training())
        length_row = ttk.Frame(training, style='Card.TFrame')
        length_row.pack(fill='x', pady=10)
        self.label(length_row, 'Букв в задании', 'Hint.TLabel').pack(side='left')
        ttk.Combobox(length_row, values=(1, 5, 10, 20), textvariable=self.training_length,
                     state='readonly', width=4).pack(side='right')
        ttk.Button(training, text='Новый набор', style='Accent.TButton', command=self.new_training).pack(fill='x')
        self.training_notice = self.label(training, '«Повторить» звучит тем же набором.\n«Новый набор» создаёт другой.',
                                          'Hint.TLabel', wraplength=234)
        self.training_notice.pack(anchor='w', pady=(10, 0))
        self.mode_tabs.bind('<<NotebookTabChanged>>', self.change_mode)
        self.label(left, 'Звучание', 'Heading.TLabel').grid(row=1, column=0, sticky='w', pady=(0, 8))
        for row, (name, variable, low, high, step) in enumerate((
            ('Скорость · слов/мин', self.speed, 5, 40, 1),
            ('Тон · Гц', self.tone, 300, 1200, 10),
            ('Помехи · %', self.noise, 0, 100, 1),
        ), 2):
            block = ttk.Frame(left, style='Card.TFrame')
            block.grid(row=row, column=0, sticky='ew', pady=6)
            heading = ttk.Frame(block, style='Card.TFrame')
            heading.pack(fill='x')
            self.label(heading, name, 'Hint.TLabel').pack(side='left')
            ttk.Label(heading, textvariable=variable, style='Card.TLabel').pack(side='right')
            tk.Scale(block, from_=low, to=high, resolution=step, variable=variable, orient='horizontal',
                     bg=self.CARD, fg=self.INK, troughcolor='#101e2c', activebackground=self.ACCENT,
                     highlightthickness=0, bd=1, sliderrelief='raised', sliderlength=18,
                     font=('Segoe UI', 10), showvalue=False, length=234).pack(fill='x')
        self.label(left, 'Настройки применяются\nпри следующем прослушивании.', 'Hint.TLabel').grid(row=5, column=0, sticky='w', pady=(12, 0))

        right = ttk.Frame(content)
        right.grid(row=0, column=1, sticky='nsew')
        listen = ttk.Frame(right, style='Card.TFrame', padding=14)
        listen.pack(fill='x', pady=(0, 16))
        title = ttk.Frame(listen, style='Card.TFrame')
        title.pack(fill='x')
        self.exercise_title = self.label(title, '', 'Heading.TLabel')
        self.exercise_title.pack(side='left')
        self.length_label = self.label(title, '', 'Hint.TLabel')
        self.length_label.pack(side='right')
        self.preview = self.label(listen, '', font=('Consolas', 17), wraplength=600)
        self.preview.pack(fill='x', pady=(6, 8))
        hints = ttk.Frame(listen, style='Card.TFrame')
        hints.pack(fill='x')
        self.morse_toggle = ttk.Checkbutton(hints, text='Подсказка Морзе', variable=self.show_morse, command=self.draw_morse)
        self.morse_toggle.pack(side='left', padx=(0, 12))
        self.original_toggle = ttk.Checkbutton(hints, text='Показать оригинал', variable=self.show_text, command=self.update_preview)
        self.original_toggle.pack(side='left')
        transmission_row = ttk.Frame(listen, style='Card.TFrame')
        transmission_row.pack(fill='x', pady=(4, 0))
        ttk.Combobox(transmission_row, textvariable=self.transmission_kind, values=('Стандартный Морзе', 'Радиограмма'), state='readonly', width=22).pack(side='left')
        self.start_toggle = ttk.Checkbutton(transmission_row, text='ЖЖЖ в начале', variable=self.start_signal)
        self.start_toggle.pack(side='left')
        self.end_toggle = ttk.Checkbutton(transmission_row, text='К в конце', variable=self.end_signal)
        self.end_toggle.pack(side='left')
        self.morse_canvas = tk.Canvas(listen, height=46, bg='#101e2c', highlightthickness=0)
        self.morse_canvas.pack(fill='x', pady=(5, 0))
        buttons = ttk.Frame(listen, style='Card.TFrame')
        buttons.pack(fill='x', pady=(10, 8))
        self.start_button = ttk.Button(buttons, text='▶  Слушать / повторить', style='Accent.TButton', command=self.start)
        self.start_button.pack(side='left', padx=(0, 10))
        ttk.Button(buttons, text='■  Стоп', command=self.stop).pack(side='left')
        self.status_label = ttk.Label(listen, textvariable=self.status, style='Hint.TLabel')
        self.status_label.pack(anchor='w', pady=(0, 8))
        ttk.Progressbar(listen, variable=self.progress, maximum=100).pack(fill='x')

        practice = ttk.Frame(right, style='Card.TFrame', padding=14)
        practice.pack(fill='both', expand=True)
        entry_title = ttk.Frame(practice, style='Card.TFrame')
        entry_title.pack(fill='x')
        self.label(entry_title, 'Твой ответ', 'Heading.TLabel').pack(side='left')
        ttk.Label(entry_title, textvariable=self.counter, style='Hint.TLabel').pack(side='right')
        self.answer = tk.Text(practice, height=1, wrap='char', font=('Consolas', 20),
                              bg='#101e2c', fg=self.INK, insertbackground=self.ACCENT,
                              selectbackground='#315b54', relief='flat', padx=14, pady=10,
                              highlightthickness=1, highlightbackground='#30465d', highlightcolor=self.ACCENT,
                              undo=True, width=20)
        self.answer.pack(fill='x', pady=(7, 0))
        actions = ttk.Frame(practice, style='Card.TFrame')
        actions.pack(fill='x', pady=8)
        self.check_button = ttk.Button(actions, text='Проверить ответ', style='Accent.TButton', command=self.check)
        self.check_button.pack(side='left', padx=(0, 10))
        self.clear_button = ttk.Button(actions, text='Очистить', command=self.clear_answer)
        self.clear_button.pack(side='left')
        self.result = self.label(practice, 'После проверки здесь появится результат.', 'Hint.TLabel')
        self.result.pack(anchor='w')
        self.comparison = tk.Text(practice, height=2, width=20, font=('Consolas', 13),
                                  bg=self.CARD, fg=self.INK, relief='flat', bd=0, state='disabled',
                                  wrap='none', takefocus=False)
        self.comparison.pack(fill='x', pady=(7, 0))
        scroll = ttk.Scrollbar(practice, orient='horizontal', command=self.comparison.xview)
        scroll.pack(fill='x', pady=(2, 4))
        self.comparison.configure(xscrollcommand=scroll.set)
        for tag, color in [('ok', self.ACCENT), ('wrong', '#ff8c98'), ('missing', '#ffcc80'), ('extra', '#ff8c98')]:
            self.comparison.tag_configure(tag, foreground=color)
        self.legend = self.label(practice, '', 'Hint.TLabel')
        self.legend.pack(anchor='w')
        self.label(outer, 'Ответ — русскими буквами · F5 — слушать · Esc — стоп · Ctrl + Enter — проверить', 'Muted.TLabel').pack(anchor='w', pady=(10, 0))

    def get_text(self):
        if self.mode == 'exam':
            return self.exam_text
        return self.training_text if self.mode == 'training' else self.rows[int(self.row_number.get())]

    def change_mode(self, _=None):
        new_mode = ('table', 'training', 'exam')[self.mode_tabs.index(self.mode_tabs.select())]
        if new_mode != self.mode:
            self.stop()
            self.mode = new_mode
            self.show_morse.set(new_mode == 'training')
            if new_mode == 'exam':
                self.new_exam()
            else:
                self.select_exercise()
        self.sync_controls()

    def hint_changed(self, *_):
        if self.show_morse.get() or self.show_text.get():
            self.hints_used = True

    def sync_controls(self):
        busy = self.worker is not None
        service_state = 'disabled' if busy or (self.mode == 'exam' and self.exam_state != 'idle') else 'normal'
        self.start_toggle.configure(state=service_state)
        self.end_toggle.configure(state=service_state)
        exam = self.mode == 'exam'
        sealed = exam and self.exam_state != 'submitted'
        self.morse_toggle.configure(state='disabled' if exam else 'normal')
        self.original_toggle.configure(state='disabled' if sealed else 'normal')
        self.alphabet_button.configure(state='disabled' if busy or sealed else 'normal')
        self.radio_button.configure(state='disabled' if busy or sealed else 'normal')
        self.start_button.configure(state='disabled' if busy or (exam and self.exam_state != 'idle') else 'normal')
        self.check_button.configure(state='disabled' if busy or (exam and self.exam_state != 'answer') else 'normal',
                                    text='Сдать экзамен' if exam else 'Проверить ответ')
        self.clear_button.configure(state='disabled' if exam and self.exam_state == 'submitted' else 'normal')
        self.answer.configure(state='disabled' if exam and self.exam_state == 'submitted' else 'normal')

    def new_exam(self):
        self.stop()
        self.exam_number = random.choice(self.numbers)
        self.exam_text = self.rows[self.exam_number]
        self.exam_state = 'idle'
        self.select_exercise()
        self.status.set('Экзамен готов · одно прослушивание')
        self.sync_controls()

    def save_attempt(self, typed='', errors=0, aligned=(), status='completed'):
        if self.attempt_saved:
            return
        eligible = self.listened and not self.hints_used and status == 'completed'
        try:
            self.store.record(self.attempt_id, 'exam' if self.mode == 'exam' else self.practice_kind if self.mode == 'training' else 'table',
                              self.get_text(), typed, aligned, errors, eligible, status,
                              self.exam_number if self.mode == 'exam' else int(self.row_number.get()) if self.mode == 'table' else None)
        except OSError as exc:
            messagebox.showwarning('История не сохранена', str(exc), parent=self.root)
        else:
            self.attempt_saved = True

    def apply_radio_settings(self, settings):
        self.radio_settings = Settings.from_dict(asdict(settings))
        self.speed.set(round(settings.speed_wpm))
        self.tone.set(round(settings.tone_hz))
        self.start_signal.set(settings.transmission['start_enabled'])
        self.end_signal.set(settings.transmission['end_enabled'])
        self.transmission_kind.set('Стандартный Морзе' if settings.mode in ('classic', 'standard') else 'Радиограмма')
        self.status.set('Настройки радиограммы применены к следующей передаче')

    def main_plan(self):
        data = asdict(self.radio_settings)
        data.update(text=self.get_text(), speed_wpm=self.speed.get(), tone_hz=self.tone.get(), repeats=1, visibility='hidden')
        data['transmission'].update(start_enabled=self.start_signal.get(), start_text='ЖЖЖ', end_enabled=self.end_signal.get(), end_text='К', check_service=False)
        if self.transmission_kind.get() == 'Стандартный Морзе':
            data.update(mode='standard', grouping={'mode':'none','size':5,'sizes':[]})
            data['spacing']['custom_group_pauses_ms'] = []
        else:
            data['mode'] = 'full' if data['separators_enabled'] else 'service'
        return build_plan(Settings.from_dict(data), MORSE)

    def open_radiogram(self):
        if self.worker or (self.mode == 'exam' and self.exam_state not in ('submitted', 'incomplete')):
            return
        from radiogram_ui import RadiogramDialog
        RadiogramDialog(self, MORSE, play_samples)

    def open_alphabet(self):
        if self.worker or (self.mode == 'exam' and self.exam_state not in ('submitted', 'incomplete')):
            return
        AlphabetDialog(self)

    def open_errors(self):
        dialog = tk.Toplevel(self.root)
        dialog.title('Мои ошибки и результаты')
        dialog.configure(bg=self.BG)
        dialog.geometry('720x600')
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill='both', expand=True)
        self.label(frame, 'Работа над ошибками', 'Title.TLabel').pack(anchor='w')
        summary = ttk.Label(frame, style='Muted.TLabel')
        summary.pack(anchor='w', pady=8)
        tabs = ttk.Notebook(frame)
        tabs.pack(fill='both', expand=True)
        errors_page = ttk.Frame(tabs, padding=8)
        history_page = ttk.Frame(tabs, padding=8)
        tabs.add(errors_page, text='Путаницы и пропуски')
        tabs.add(history_page, text='Последние попытки')
        tree = ttk.Treeview(errors_page, columns=('expected', 'received', 'count'), show='headings', height=10)
        for column, title in [('expected', 'Передано'), ('received', 'Принято'), ('count', 'Случаев')]:
            tree.heading(column, text=title)
            tree.column(column, width=150, anchor='center')
        scroll = ttk.Scrollbar(errors_page, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        history_list = ttk.Frame(history_page)
        history_list.pack(fill='both', expand=True)
        history = ttk.Treeview(history_list, selectmode='extended', columns=('date', 'kind', 'result'), show='headings')
        for column, title, width in [('date', 'Время (UTC)', 160), ('kind', 'Режим', 140), ('result', 'Результат', 260)]:
            history.heading(column, text=title)
            history.column(column, width=width)
        hs = ttk.Scrollbar(history_list, command=history.yview)
        history.configure(yscrollcommand=hs.set)
        history.pack(side='left', fill='both', expand=True)
        hs.pack(side='right', fill='y')
        names = {'exam': 'Экзамен', 'table': 'Таблица', 'training': 'Тренировка', 'mistakes': 'По ошибкам'}
        actions = ttk.Frame(history_page)
        actions.pack(fill='x', pady=(8, 0))
        note_label = ttk.Label(frame, style='Muted.TLabel', wraplength=650)
        note_label.pack(anchor='w', pady=10)
        row_ids = {}

        def refresh():
            summary.configure(text=f"Зачтено попыток без подсказок: {self.store.data['attempts']}")
            tree.delete(*tree.get_children())
            history.delete(*history.get_children())
            row_ids.clear()
            for expected, received, count in self.store.confusions():
                tree.insert('', 'end', values=(expected if expected != '·' else 'Лишняя буква',
                                               received if received != '·' else 'Пропуск', count))
            for row in reversed(self.store.data['history']):
                result = 'Не завершён' if row.get('status') != 'completed' else f"{row.get('score')}% · ошибок {row.get('errors')}" + (f" · оценка {row.get('grade')}" if row.get('kind') == 'exam' else '')
                if row.get('status') == 'completed' and not row.get('eligible'):
                    result += ' · с подсказкой'
                item = history.insert('', 'end', values=(row.get('at', '')[:19].replace('T', ' '), names.get(row.get('kind'), ''), result))
                row_ids[item] = row.get('id')
            note = self.store.error or ('Сначала заверши приём без подсказок: ошибки появятся здесь.' if not self.store.confusions() else 'В упражнения войдут до 10 букв из самых частых путаниц и пропусков.')
            note_label.configure(text=note)
            train_button.configure(state='normal' if self.store.problem_letters(MORSE) else 'disabled')
            delete_button.configure(state='disabled')
            clear_button.configure(state='normal' if not self.store.error and (self.store.data['history'] or self.store.data['attempts'] or self.store.data['confusions']) else 'disabled')

        def delete_selected():
            ids = [row_ids[item] for item in history.selection() if row_ids.get(item)]
            if not ids:
                return
            if not messagebox.askyesno('Удалить попытки?', f'Удалить выбранные попытки ({len(ids)})?\nИх вклад в статистику ошибок тоже будет удалён.\nОтменить это действие нельзя.', parent=dialog):
                return
            try:
                self.store.delete_attempts(ids)
            except OSError as exc:
                messagebox.showerror('Не удалось удалить попытки', str(exc), parent=dialog)
                return
            refresh()

        def clear_all():
            if not messagebox.askyesno('Очистить всю историю?', 'Удалить все попытки и полностью сбросить статистику ошибок?\nОтменить это действие нельзя.', parent=dialog):
                return
            try:
                self.store.clear_history()
            except OSError as exc:
                messagebox.showerror('Не удалось очистить историю', str(exc), parent=dialog)
                return
            refresh()

        delete_button = ttk.Button(actions, text='Удалить выбранные', command=delete_selected, state='disabled')
        delete_button.pack(side='left')
        clear_button = ttk.Button(actions, text='Очистить всю историю', command=clear_all)
        clear_button.pack(side='right')
        history.bind('<<TreeviewSelect>>', lambda _: delete_button.configure(state='normal' if history.selection() and not self.store.error else 'disabled'))
        history.bind('<Delete>', lambda _: delete_selected())
        def train():
            letters = self.store.problem_letters(MORSE)
            if not letters:
                return
            dialog.destroy()
            self.mode_tabs.select(1)
            self.change_mode()
            self.alphabet.set(letters)
            self.training_length.set('20')
            self.new_training()
            self.practice_kind = 'mistakes'
            self.show_morse.set(False)
            self.hints_used = False
            self.training_notice.configure(text=f'По ошибкам: {letters}. Подсказка выключена.')
        train_button = ttk.Button(frame, text='Тренировать проблемные буквы', style='Accent.TButton', command=train)
        train_button.pack(anchor='w')
        refresh()
        dialog.bind('<Escape>', lambda _: dialog.destroy())
        dialog.grab_set()

    def new_training(self):
        try:
            text = generate_training(self.alphabet.get(), int(self.training_length.get()))
        except ValueError as exc:
            self.training_notice.configure(text=str(exc), foreground='#ffcc80')
            return
        self.practice_kind = 'training'
        self.training_text = text
        self.training_notice.configure(text='Новый набор готов. Нажми «Слушать».', foreground=self.MUTED)
        self.select_exercise()

    def draw_morse(self, mark=None, active=False):
        self.hint_state = (mark, active)
        self.morse_canvas.delete('all')
        if not self.show_morse.get():
            self.morse_canvas.create_text(12, 23, anchor='w', text='Подсказка выключена · приём только на слух',
                                          fill=self.MUTED, font=('Segoe UI', 10))
        elif mark is None:
            self.morse_canvas.create_text(12, 23, anchor='w', text='Здесь будут звучащая буква и её код · —',
                                          fill=self.MUTED, font=('Segoe UI', 10))
        else:
            _, _, letter, symbol_index = mark
            self.morse_canvas.create_text(14, 23, anchor='w', text=letter,
                                          fill=self.INK, font=('Consolas', 23, 'bold'))
            for index, symbol in enumerate(MORSE[letter]):
                x = 84 + index * 42
                if index == symbol_index and active:
                    self.morse_canvas.create_rectangle(x - 17, 5, x + 17, 41, fill='#315b54', outline='')
                self.morse_canvas.create_text(x, 23, text='•' if symbol == '.' else '—',
                                              fill=self.ACCENT if index == symbol_index and active else self.MUTED,
                                              font=('Consolas', 25, 'bold'))


    def update_preview(self):
        self.exercise_title.configure(text='Экзамен' if self.mode == 'exam' else 'Свободная тренировка' if self.mode == 'training' else f'Упражнение № {self.row_number.get()}')
        self.length_label.configure(text=f'{len(self.get_text())} букв')
        self.preview.configure(text=grouped(self.get_text()) if self.show_text.get() and (self.mode != 'exam' or self.exam_state == 'submitted') else grouped('•' * len(self.get_text())),
                               foreground=self.INK if self.show_text.get() else self.MUTED)

    def select_exercise(self, *_):
        self.stop()
        self.attempt_id = uuid.uuid4().hex
        self.attempt_saved = False
        self.listened = False
        self.show_text.set(False)
        self.hints_used = self.show_morse.get()
        self.clear_answer()
        self.update_preview()
        self.progress.set(0)
        self.status.set('Готов к приёму')
        self.sync_controls()

    def navigate(self, offset):
        index = self.numbers.index(int(self.row_number.get()))
        self.row_number.set(str(self.numbers[(index + offset) % len(self.numbers)]))

    def random_exercise(self):
        choices = [n for n in self.numbers if str(n) != self.row_number.get()]
        if choices:
            self.row_number.set(str(random.choice(choices)))

    def open_library(self):
        dialog = tk.Toplevel(self.root)
        dialog.title('Выбрать упражнение')
        dialog.configure(bg=self.BG)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=20)
        frame.pack()
        self.label(frame, 'Все упражнения', 'Title.TLabel').grid(row=0, column=0, columnspan=10, sticky='w', pady=(0, 8))
        def choose(number):
            self.row_number.set(str(number))
            dialog.destroy()
        for index, number in enumerate(self.numbers):
            ttk.Button(frame, text=str(number), width=4,
                       style='Accent.TButton' if str(number) == self.row_number.get() else 'TButton',
                       command=lambda n=number: choose(n)).grid(row=index // 10 + 1, column=index % 10, padx=3, pady=3)
        dialog.bind('<Escape>', lambda _: dialog.destroy())
        dialog.grab_set()

    def clear_result(self):
        self.result.configure(text='После проверки здесь появится результат.', foreground=self.MUTED) #тест 8
        self.comparison.configure(state='normal')
        self.comparison.delete('1.0', 'end')
        self.comparison.configure(state='disabled')
        self.legend.configure(text='')

    def clear_answer(self):
        if self.mode == 'exam' and self.exam_state == 'submitted':
            return
        self.answer.configure(state='normal')
        self.answer.delete('1.0', 'end')
        self.clear_result()
        self.counter.set(f'0 / {len(self.get_text())} букв')

    def answer_changed(self, _=None):
        if self.answer.edit_modified():
            typed = ''.join(self.answer.get('1.0', 'end').upper().split())
            self.counter.set(f'{len(typed)} / {len(self.get_text())} букв')
            if self.mode != 'exam' or self.exam_state != 'submitted':
                self.clear_result()
            self.answer.edit_modified(False)

    def start(self):
        if self.worker is not None or (self.mode == 'exam' and self.exam_state != 'idle'):
            return
        try:
            plan = self.main_plan()
        except ValueError as exc:
            messagebox.showwarning('Параметры передачи', str(exc), parent=self.root)
            return
        self.playback_attempt = self.attempt_id
        if self.mode == 'exam':
            self.exam_state = 'playing'
        self.hints_used |= self.show_morse.get() or self.show_text.get()
        self.stop_event.clear()
        self.progress.set(0)
        self.playing_since = None
        self.draw_morse()
        self.start_button.configure(state='disabled')
        self.check_button.configure(state='disabled')
        self.status.set('Подготовка сигнала…')
        args = (self.get_text(), self.speed.get(), self.tone.get(), self.noise.get(), plan)
        self.worker = threading.Thread(target=self.play, args=args, daemon=True)
        self.worker.start()
        self.sync_controls()
        self.answer.focus_set()

    def play(self, text, wpm, tone, noise, plan=None):
        if plan is None:
            stream_audio(text, wpm, tone, noise, self.stop_event, self.events)
            return
        error = None
        try:
            audio, events = render_plan(plan, self.stop_event)
            if audio is not None and not self.stop_event.is_set():
                if noise:
                    audio += np.random.default_rng().normal(0, .25 * noise / 100, len(audio)).astype(np.float32)
                    np.clip(audio, -1, 1, out=audio)
                marks = [(start, end, event.letter, event.symbol_index) for start, end, event in events if event.kind == 'tone']
                play_samples(audio, marks, self.stop_event, self.events)
        except Exception as exc:
            error = str(exc)
        finally:
            self.events.put(('done', error))

    def poll_audio(self):
        while not self.events.empty():
            event, value = self.events.get_nowait()
            if event == 'playing' and not self.stop_event.is_set():
                self.playing_since, self.duration, self.marks = value
                self.mark_starts = [mark[0] for mark in self.marks]
                self.status.set('Передача · слушай и записывай')
            elif event == 'done':
                # ждать
                if self.worker:
                    self.worker.join()
                self.worker = None
                self.playing_since = None
                self.draw_morse()
                current = self.playback_attempt == self.attempt_id
                if not current:
                    self.sync_controls()
                    continue
                if value:
                    if self.mode == 'exam':
                        self.exam_state = 'incomplete'
                        self.save_attempt(status='incomplete')
                    self.status.set('Не удалось воспроизвести звук')
                    messagebox.showerror('Ошибка звука', value, parent=self.root)
                elif self.stop_event.is_set():
                    self.status.set('Экзамен не завершён · начни новый' if self.mode == 'exam' else 'Остановлено · можно повторить')
                else:
                    self.progress.set(100)
                    self.listened = True
                    if self.mode == 'exam':
                        self.exam_state = 'answer'
                    self.status.set('Передача завершена · сдай экзамен' if self.mode == 'exam' else 'Передача завершена · проверь ответ')
                self.sync_controls()
        if self.playing_since is not None:
            elapsed = time.monotonic() - self.playing_since
            self.progress.set(max(0, min(100, elapsed / self.duration * 100)))
            frame = int(elapsed * 22050)
            index = bisect_right(self.mark_starts, frame) - 1
            mark = self.marks[index] if index >= 0 else None
            state = (mark, mark is not None and frame < mark[1])
            if state != self.hint_state:
                self.draw_morse(*state)
        if not self.closed:
            self.poll_id = self.root.after(15, self.poll_audio)

    def stop(self):
        if self.mode == 'exam' and self.exam_state in ('playing', 'answer'):
            self.exam_state = 'incomplete'
            self.save_attempt(status='incomplete')
        self.stop_event.set()
        self.playing_since = None
        self.draw_morse()
        if self.mode == 'exam' and self.exam_state == 'incomplete':
            self.status.set('Экзамен не завершён · начни новый')
        elif self.worker:
            self.status.set('Остановлено · можно повторить')
        self.sync_controls()

    def check(self):
        if self.mode == 'exam' and self.exam_state != 'answer':
            return
        if self.worker is not None:
            return
        typed = ''.join(self.answer.get('1.0', 'end').upper().split())
        if not typed and self.mode != 'exam':
            self.result.configure(text='Сначала введи услышанные буквы.', foreground='#ffcc80')
            self.answer.focus_set()
            return
        if len(typed) > 100:
            self.result.configure(text=f'Ответ слишком длинный: ожидается {len(self.get_text())} букв.', foreground='#ffcc80')
            return
        original = self.get_text()
        errors, aligned = compare_answer(original, typed)
        correct = sum(tag == 'ok' for _, _, tag in aligned)
        if self.listened:
            self.save_attempt(typed, errors, aligned)
        if self.mode == 'exam':
            self.exam_state = 'submitted'
        self.result.configure(text=f'Верно: {correct} из {len(original)}  ·  Ошибок: {errors}',
                              foreground=self.ACCENT if errors == 0 else '#ffcc80')
        self.comparison.configure(state='normal')
        self.comparison.delete('1.0', 'end')
        # Keep both aligned rows visible; long answers have a horizontal scrollbar.
        for label, side in [('Было:  ', 0), ('Ответ: ', 1)]:
            self.comparison.insert('end', label)
            for index, pair in enumerate(aligned):
                self.comparison.insert('end', pair[side], pair[2])
                if (index + 1) % 5 == 0:
                    self.comparison.insert('end', ' ')
            if side == 0:
                self.comparison.insert('end', '\n')
        self.comparison.configure(state='disabled')
        self.legend.configure(text='Зелёный — верно · красный — ошибка · точка — пропуск / лишняя буква')
        if self.mode == 'exam':
            score, grade = score_result(len(original), errors)
            self.result.configure(text=f'Оценка: {grade} · {score}% · ошибок: {errors} · текст № {self.exam_number}')
        self.show_text.set(True)
        self.update_preview()
        self.sync_controls()

    def close(self):
        self.closed = True
        self.stop()
        self.root.after_cancel(self.poll_id)
        self.root.destroy()


class AlphabetDialog:
    def __init__(self, app):
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title('Азбука Морзе со звуком')
        self.window.configure(bg=app.BG)
        self.window.resizable(False, False)
        self.window.transient(app.root)
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.pending = None
        self.letter = 'А'
        self.started = None
        self.marks = []
        self.closed = False
        frame = ttk.Frame(self.window, padding=20)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Азбука со звуком', style='Title.TLabel').pack(anchor='w')
        ttk.Label(frame, text='Нажми букву. Скорость и тон — из основных настроек.', style='Muted.TLabel').pack(anchor='w', pady=8)
        self.canvas = tk.Canvas(frame, width=490, height=68, bg='#101e2c', highlightthickness=0)
        self.canvas.pack(fill='x', pady=(0, 12))
        keys = ttk.Frame(frame)
        keys.pack()
        for i, letter in enumerate(MORSE):
            ttk.Button(keys, text=letter, width=5, command=lambda char=letter: self.choose(char)).grid(row=i // 6, column=i % 6, padx=4, pady=4)
        ttk.Button(frame, text='Повторить букву', command=lambda: self.choose(self.letter)).pack(fill='x', pady=(12, 0))
        self.note = ttk.Label(frame, text='Выбери букву, чтобы услышать её сигнал.', style='Muted.TLabel')
        self.note.pack(anchor='w', pady=(8, 0))
        self.render()
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.window.bind('<Escape>', lambda _: self.close())
        self.window.grab_set()
        self.poll_id = self.window.after(15, self.poll)

    def render(self, symbol=None):
        self.canvas.delete('all')
        self.canvas.create_text(24, 34, anchor='w', text=self.letter, font=('Consolas', 32, 'bold'), fill=self.app.INK)
        for i, value in enumerate(MORSE[self.letter]):
            x = 110 + 48 * i
            if i == symbol:
                self.canvas.create_rectangle(x - 20, 10, x + 20, 58, fill='#315b54', outline='')
            self.canvas.create_text(x, 34, text='•' if value == '.' else '—', font=('Consolas', 29, 'bold'),
                                    fill=self.app.ACCENT if i == symbol else self.app.MUTED)

    def choose(self, letter):
        if self.worker is not None:
            self.pending = letter
            self.stop_event.set()
            return
        self.letter = letter
        self.pending = None
        self.stop_event.clear()
        self.started = None
        self.render()
        self.note.configure(text=f'{letter} · {MORSE[letter]}')
        self.worker = threading.Thread(target=stream_audio,
                                       args=(letter, self.app.speed.get(), self.app.tone.get(), 0, self.stop_event, self.events), daemon=True)
        self.worker.start()

    def poll(self):
        while not self.events.empty():
            event, value = self.events.get_nowait()
            if event == 'playing' and not self.stop_event.is_set():
                self.started, _, self.marks = value
            elif event == 'done':
                self.worker.join()
                self.worker = None
                self.started = None
                self.render()
                if value:
                    self.note.configure(text=f'Ошибка звука: {value}')
                if self.pending:
                    self.choose(self.pending)
        if self.started is not None:
            frame = int((time.monotonic() - self.started) * 22050)
            symbol = next((index for start, end, _, index in self.marks if start <= frame < end), None)
            self.render(symbol)
        if not self.closed:
            self.poll_id = self.window.after(15, self.poll)

    def close(self):
        self.closed = True
        self.stop_event.set()
        self.window.after_cancel(self.poll_id)
        self.window.destroy()


def main():
    root = tk.Tk()
    try:
        App(root)
    except (OSError, ValueError) as exc:
        root.withdraw()
        messagebox.showerror('Не удалось открыть базу упражнений', str(exc), parent=root) #надо провести тесты криво
        root.destroy()
    else:
        root.mainloop()


if __name__ == '__main__':
    main()
