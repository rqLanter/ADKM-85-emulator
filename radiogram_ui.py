"""Radiogram editor and player; legacy exercise modes remain independent."""
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from dataclasses import asdict
from pathlib import Path
import queue
import threading
import time
import math
from bisect import bisect_right
from radiogram import Settings, LEVELS, build_plan, render_plan
from learning import compare_answer

MODES = {'Стандартный Морзе':'standard','Классический':'classic','Обычный':'ordinary','С разделителями':'separators','Со служебными сигналами':'service','Полная радиограмма':'full'}
GROUPS = {'Без группировки':'none','Фиксированный размер':'fixed','Разные размеры':'custom','Ручная':'manual'}
VIEWS = {'Весь текст':'all','Только номер группы':'group','Скрыть до проверки':'hidden'}

class RadiogramDialog:
    def __init__(self, app, alphabet, player):
        self.app, self.alphabet, self.player = app, alphabet, player
        self.window = tk.Toplevel(app.root)
        self.window.title('Учебная радиограмма')
        self.window.geometry('1000x760')
        self.window.minsize(900,740)
        self.window.transient(app.root)
        self.window.grab_set()
        self.events=queue.Queue(); self.stop_event=threading.Event(); self.worker=None
        self.started=None; self.plan=None; self.marks=[]; self.vars={}; self.loading=True
        frame=ttk.Frame(self.window,padding=14); frame.pack(fill='both',expand=True)
        top=ttk.Frame(frame); top.pack(fill='x')
        self.level=tk.StringVar(value='Пользовательский')
        levels=ttk.Combobox(top,textvariable=self.level,values=(*LEVELS,'Пользовательский'),state='readonly',width=20)
        levels.pack(side='left'); levels.bind('<<ComboboxSelected>>',self.apply_level)
        ttk.Button(top,text='Применить к приложению',command=self.apply_to_app).pack(side='left',padx=8)
        ttk.Button(top,text='Сохранить пресет',command=self.save).pack(side='right')
        ttk.Button(top,text='Загрузить пресет',command=self.load).pack(side='right',padx=6)
        tabs=ttk.Notebook(frame); tabs.pack(fill='both',expand=True,pady=10)
        pages=[]
        for name in ('Текст и группы','Паузы','Начало и конец'):
            page=ttk.Frame(tabs,padding=12); tabs.add(page,text=name); pages.append(page)
        main,pauses,service=pages
        source=ttk.Frame(main); source.pack(fill='x')
        self.row=tk.StringVar(value=str(app.numbers[0]))
        ttk.Combobox(source,textvariable=self.row,values=app.numbers,state='readonly',width=6).pack(side='left')
        ttk.Button(source,text='Взять учебную строку',command=self.take_row).pack(side='left',padx=8)
        self.field(main,'mode','Передача', 'Полная радиограмма',tuple(MODES))
        self.field(main,'grouping.mode','Группировка','Фиксированный размер',tuple(GROUPS))
        self.field(main,'grouping.size','Размер группы (1–20)',5)
        self.field(main,'grouping.sizes','Разные размеры, через запятую','')
        self.field(main,'visibility','Эталон','Скрыть до проверки',tuple(VIEWS))
        ttk.Label(main,text='Свой текст: пробелы и | задают группы в ручном режиме; перенос — новую строку.').pack(anchor='w')
        self.text=tk.Text(main,height=2,width=30,font=('Consolas',13)); self.text.pack(fill='x')
        self.text.insert('1.0',app.rows[app.numbers[0]])
        self.text.bind('<<Modified>>',self.text_changed)
        ttk.Button(main,text='Построить группы / обновить паузы',command=self.group_editor).pack(anchor='w',pady=6)
        self.group_area=ttk.Frame(main); self.group_area.pack(fill='both',expand=True)
        self.group_canvas=tk.Canvas(self.group_area,height=60,highlightthickness=0,bg=app.BG)
        scroll=ttk.Scrollbar(self.group_area,command=self.group_canvas.yview)
        self.group_canvas.configure(yscrollcommand=scroll.set)
        self.group_canvas.pack(side='left',fill='both',expand=True); scroll.pack(side='right',fill='y')
        self.group_frame=ttk.Frame(self.group_canvas); self.group_canvas.create_window(0,0,window=self.group_frame,anchor='nw')
        self.boundaries=[]
        for key,label,value in [('speed_wpm','Скорость WPM',20),('tone_hz','Тон Гц',700),('spacing.element_units','Между элементами (1–3 точки)',1),('spacing.letter_pause_ms','Между буквами, мс',350),('spacing.group_pause_ms','Между группами, мс',900),('spacing.line_pause_ms','Между строками, мс',1500),('spacing.repeat_pause_ms','Между повторами, мс',1000),('repeats','Количество повторов (1–20)',1)]:
            self.field(pauses,key,label,value)
        self.field(pauses,'experimental','Экспериментальный ритм',False)
        ttk.Label(pauses,text='Внимание: внутренние интервалы ≠ 1 нарушают стандартный ритм Морзе.').pack(anchor='w',pady=8)
        ttk.Button(pauses,text='Восстановить стандартные паузы',command=self.standard).pack(anchor='w')
        self.field(service,'name','Имя пресета','Моя радиограмма')
        for key,label,value in [('start_enabled','Передавать начальный сигнал',True),('start_text','Начальный сигнал','ЖЖЖ'),('pause_after_start_ms','Пауза после начала, мс',1500),('end_enabled','Передавать конечный сигнал',True),('end_text','Конечный сигнал','К'),('pause_before_end_ms','Пауза перед концом, мс',1000),('check_service','Проверять служебные сигналы',False),('service_speed_wpm','Скорость служебных (пусто = основная)',''),('service_tone_hz','Тон служебных (пусто = основной)','')]:
            self.field(service,'transmission.'+key,label,value)
        bottom=ttk.Frame(frame); bottom.pack(fill='x')
        self.listen=ttk.Button(bottom,text='Слушать',command=self.start); self.listen.pack(side='left')
        ttk.Button(bottom,text='Стоп',command=self.stop_event.set).pack(side='left',padx=6)
        ttk.Button(bottom,text='Прослушать пример',command=lambda:self.start(True)).pack(side='left')
        self.status=ttk.Label(frame,text='Готово · эталон скрыт'); self.status.pack(anchor='w',pady=5)
        self.progress=ttk.Progressbar(frame,maximum=100); self.progress.pack(fill='x')
        self.preview=ttk.Label(frame,text='',wraplength=930); self.preview.pack(anchor='w',pady=5)
        ttk.Label(frame,text='Ответ: пробелы и | не учитываются; повторы записываются подряд.').pack(anchor='w')
        self.answer=tk.Text(frame,height=1,font=('Consolas',13)); self.answer.pack(fill='x')
        ttk.Button(frame,text='Проверить последнюю передачу',command=self.check).pack(anchor='w',pady=5)
        self.loading=False
        initial=Settings.from_dict(asdict(app.radio_settings))
        if not initial.text: initial.text=app.get_text()
        self.put(initial)
        self.vars['speed_wpm'].set(app.speed.get())
        self.vars['tone_hz'].set(app.tone.get())
        self.vars['transmission.start_enabled'].set(app.start_signal.get())
        self.vars['transmission.end_enabled'].set(app.end_signal.get())
        self.group_editor()
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        self.window.bind('<Escape>',lambda _:self.stop_event.set())
        self.timer=self.window.after(15,self.poll)

    def apply_to_app(self):
        try:
            settings=self.settings()
            build_plan(settings,self.alphabet)
            self.app.apply_radio_settings(settings)
            self.status.configure(text='Применено к приложению. Для таблицы/экзамена — одна передача; эталон без ЖЖЖ/К.')
        except ValueError as exc:
            messagebox.showwarning('Настройки',str(exc),parent=self.window)

    def text_changed(self,_=None):
        if self.text.edit_modified():
            self.changed()
            self.text.edit_modified(False)

    def field(self,parent,key,label,value,choices=None):
        row=ttk.Frame(parent); row.pack(fill='x',pady=2)
        ttk.Label(row,text=label,width=43).pack(side='left')
        var=tk.BooleanVar(value=value) if type(value) is bool else tk.StringVar(value=value)
        self.vars[key]=var
        if type(value) is bool: widget=ttk.Checkbutton(row,variable=var)
        elif choices: widget=ttk.Combobox(row,textvariable=var,values=choices,state='readonly',width=25)
        else: widget=ttk.Entry(row,textvariable=var,width=28)
        widget.pack(side='left')
        var.trace_add('write',self.changed)

    def changed(self,*_):
        if not self.loading: self.level.set('Пользовательский')

    def settings(self, include_boundaries=True):
        data=asdict(Settings())
        data['text']=self.text.get('1.0','end').strip(); data['difficulty']=self.level.get()
        for key,var in self.vars.items():
            value=var.get()
            if key=='mode': value=MODES[value]
            elif key=='grouping.mode': value=GROUPS[value]
            elif key=='visibility': value=VIEWS[value]
            elif key=='grouping.sizes': value=[int(n.strip()) for n in value.split(',') if n.strip()]
            elif key in ('name','transmission.start_text','transmission.end_text'): pass
            elif isinstance(var,tk.BooleanVar): pass
            elif key.startswith('transmission.service_'): value=float(value) if value.strip() else None
            else: value=float(value) if key=='spacing.element_units' else int(value)
            if '.' in key:
                parent,child=key.split('.'); data[parent][child]=value
            else: data[key]=value
        if data['transmission']['start_enabled'] or data['transmission']['end_enabled']:
            data['mode']={'classic':'standard','ordinary':'service','separators':'full'}.get(data['mode'],data['mode'])
        data['separators_enabled']=data['mode'] in ('full','separators')
        if include_boundaries:
            data['spacing']['custom_group_pauses_ms']=[int(var.get()) if var.get().strip() else None for var in self.boundaries]
        return Settings.from_dict(data)

    def take_row(self):
        self.text.delete('1.0','end'); self.text.insert('1.0',self.app.rows[int(self.row.get())]); self.group_editor()

    def group_editor(self):
        try: plan=build_plan(self.settings(False),self.alphabet)
        except (ValueError,KeyError) as exc:
            messagebox.showwarning('Проверь параметры',str(exc),parent=self.window); return
        old=[v.get() for v in self.boundaries]; self.boundaries=[]
        for child in self.group_frame.winfo_children(): child.destroy()
        for i,group in enumerate(plan.groups):
            row=ttk.Frame(self.group_frame); row.pack(anchor='w')
            ttk.Label(row,text=f'Группа {group.number} · {len(group.text)} букв',width=30).pack(side='left')
            if i<len(plan.groups)-1:
                var=tk.StringVar(value=old[i] if i<len(old) else '')
                self.boundaries.append(var); var.trace_add('write',self.changed)
                ttk.Entry(row,textvariable=var,width=8).pack(side='left')
                ttk.Label(row,text='мс после группы (пусто = общая)').pack(side='left')
        self.group_frame.update_idletasks(); self.group_canvas.configure(scrollregion=self.group_canvas.bbox('all'))
        self.preview.configure(text=plan.display if self.vars['visibility'].get()=='Весь текст' else 'Эталон скрыт')

    def put(self,settings):
        self.loading=True; data=asdict(settings)
        for key,var in self.vars.items():
            value=data[key] if '.' not in key else data[key.split('.')[0]][key.split('.')[1]]
            if key=='mode': value=next(k for k,v in MODES.items() if v==value)
            elif key=='grouping.mode': value=next(k for k,v in GROUPS.items() if v==value)
            elif key=='visibility': value=next(k for k,v in VIEWS.items() if v==value)
            elif key=='grouping.sizes': value=', '.join(map(str,value))
            elif value is None: value=''
            var.set(value)
        self.text.delete('1.0','end'); self.text.insert('1.0',settings.text)
        self.level.set(settings.difficulty); self.boundaries=[]; self.group_editor()
        for var,value in zip(self.boundaries,settings.spacing['custom_group_pauses_ms']): var.set('' if value is None else value)
        self.text.edit_modified(False)
        self.loading=False

    def apply_level(self,_=None):
        name=self.level.get()
        if name not in LEVELS: return
        speed,letter,group=LEVELS[name]
        self.loading=True
        for key,value in [('speed_wpm',speed),('spacing.letter_pause_ms',letter),('spacing.group_pause_ms',group)]: self.vars[key].set(value)
        self.loading=False

    def standard(self):
        try:
            dot=1200/int(self.vars['speed_wpm'].get())
            for key,value in [('spacing.element_units',1),('spacing.letter_pause_ms',max(100,math.ceil(3*dot))),('spacing.group_pause_ms',math.ceil(7*dot)),('spacing.line_pause_ms',math.ceil(7*dot)),('experimental',False)]: self.vars[key].set(value)
            for var in self.boundaries: var.set('')
        except ValueError: messagebox.showwarning('Скорость','Введи скорость 5–40 WPM',parent=self.window)

    def save(self):
        try: settings=self.settings(); build_plan(settings,self.alphabet)
        except ValueError as exc:
            messagebox.showwarning('Параметры',str(exc),parent=self.window); return
        path=filedialog.asksaveasfilename(parent=self.window,defaultextension='.json',filetypes=[('Пресет JSON','*.json')])
        if path:
            try: settings.save(path)
            except OSError as exc: messagebox.showerror('Сохранение',str(exc),parent=self.window)

    def load(self):
        path=filedialog.askopenfilename(parent=self.window,filetypes=[('Пресет JSON','*.json')])
        if path:
            try:
                settings=Settings.load(path); build_plan(settings,self.alphabet); self.put(settings)
            except (OSError,ValueError,TypeError) as exc: messagebox.showerror('Загрузка',str(exc),parent=self.window)

    def start(self,example=False):
        if self.worker: return
        try:
            settings=self.settings()
            if example: settings.text='АБВГДЕЖЗИЙ'; settings.grouping={'mode':'fixed','size':5,'sizes':[]}; settings.spacing['custom_group_pauses_ms']=[]
            plan=build_plan(settings,self.alphabet)
            if len(plan.expected)>2000: raise ValueError('Для проверки допускается не более 2000 букв с учётом повторов.')
        except ValueError as exc:
            messagebox.showwarning('Передача',str(exc),parent=self.window); return
        self.plan=plan; self.visibility=settings.visibility; self.started=None; self.completed=False
        self.stop_event.clear(); self.answer.delete('1.0','end'); self.listen.configure(state='disabled')
        self.preview.configure(text=plan.display if self.visibility=='all' else 'Эталон скрыт')
        self.status.configure(text='Подготовка передачи…'); self.progress['value']=0
        def work():
            error=None
            try:
                audio,marks=render_plan(plan,self.stop_event)
                if audio is not None and not self.stop_event.is_set(): self.player(audio,marks,self.stop_event,self.events)
            except Exception as exc: error=str(exc)
            finally: self.events.put(('done',error))
        self.worker=threading.Thread(target=work,daemon=True); self.worker.start()

    def poll(self):
        while not self.events.empty():
            kind,value=self.events.get_nowait()
            if kind=='playing':
                self.started,self.duration,self.marks=value; self.starts=[m[0] for m in self.marks]
            elif kind=='done':
                self.worker.join(); self.worker=None; self.started=None; self.listen.configure(state='normal')
                self.completed=not value and not self.stop_event.is_set()
                self.status.configure(text=value or ('ПЕРЕДАЧА ЗАВЕРШЕНА' if self.completed else 'ОСТАНОВЛЕНО'))
                if self.completed: self.progress['value']=100
        if self.started is not None and not self.stop_event.is_set():
            elapsed=max(0,time.monotonic()-self.started); frame=int(elapsed*22050)
            index=bisect_right(self.starts,frame)-1
            if index>=0:
                event=self.marks[index][2]
                self.status.configure(text=f'{event.stage} · повтор {event.repeat}')
                if self.visibility=='group': self.preview.configure(text=f'Группа № {event.group}' if event.group else 'Служебный этап')
            self.progress['value']=min(100,100*elapsed/self.duration)
        self.timer=self.window.after(15,self.poll)

    def check(self):
        if self.worker or not self.plan or not getattr(self,'completed',False): return
        typed=''.join(self.answer.get('1.0','end').upper().replace('|','').split())
        if len(typed)>4000: self.status.configure(text='Ответ слишком длинный'); return
        errors,_=compare_answer(self.plan.expected,typed)
        self.preview.configure(text='Эталон: '+self.plan.expected)
        self.status.configure(text=f'Ошибок: {errors} · букв в эталоне: {len(self.plan.expected)}')

    def close(self):
        self.stop_event.set(); self.window.after_cancel(self.timer); self.window.destroy()
