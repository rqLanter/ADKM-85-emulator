"""Validated radiogram settings and structured transmission plans."""
from dataclasses import dataclass, field, asdict
import json
import math
import re
from pathlib import Path

MODES = ('classic', 'standard', 'ordinary', 'separators', 'service', 'full')
LEVELS = {'Очень легко': (10, 900, 2000), 'Легко': (15, 600, 1400),
          'Нормально': (20, 350, 900), 'Сложно': (25, 180, 420), 'Очень сложно': (30, 120, 280)}


@dataclass
class Settings:
    name: str = 'Моя радиограмма'
    text: str = ''
    mode: str = 'full'
    speed_wpm: int = 20
    tone_hz: int = 700
    difficulty: str = 'Пользовательский'
    grouping: dict = field(default_factory=lambda: {'mode': 'fixed', 'size': 5, 'sizes': []})
    transmission: dict = field(default_factory=lambda: {'start_enabled': True, 'start_text': 'ЖЖЖ',
        'end_enabled': True, 'end_text': 'К', 'pause_after_start_ms': 1500, 'pause_before_end_ms': 1000,
        'check_service': False, 'service_speed_wpm': None, 'service_tone_hz': None})
    spacing: dict = field(default_factory=lambda: {'element_units': 1, 'letter_pause_ms': 350,
        'group_pause_ms': 900, 'line_pause_ms': 1500, 'repeat_pause_ms': 1000, 'custom_group_pauses_ms': []})
    separators_enabled: bool = True
    experimental: bool = False
    repeats: int = 1
    visibility: str = 'hidden'

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError('Настройки должны быть объектом JSON.')
        base = asdict(cls())
        for key, value in data.items():
            if key not in base:
                raise ValueError(f'Неизвестный параметр: {key}')
            if isinstance(base[key], dict):
                if not isinstance(value, dict) or set(value) - set(base[key]):
                    raise ValueError(f'Неверные параметры раздела {key}')
                base[key].update(value)
            else:
                base[key] = value
        if 'mode' not in data:
            base['mode'] = 'full' if base['separators_enabled'] else 'service'
        result = cls(**base)
        result.validate()
        return result

    def validate(self):
        def number(value, low, high, name):
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'{name}: допустимо от {low} до {high}')
        if self.mode not in MODES or self.visibility not in ('all', 'group', 'hidden'):
            raise ValueError('Неизвестный режим передачи или отображения.')
        if not all(isinstance(v, str) for v in (self.name, self.text, self.difficulty)):
            raise ValueError('Имя, текст и уровень должны быть строками.')
        number(self.speed_wpm, 5, 40, 'Скорость')
        number(self.tone_hz, 300, 1200, 'Тон')
        if type(self.repeats) is not int or not 1 <= self.repeats <= 20:
            raise ValueError('Число повторов: от 1 до 20.')
        if type(self.experimental) is not bool or type(self.separators_enabled) is not bool:
            raise ValueError('Неверное значение переключателя.')
        for key in ('start_enabled', 'end_enabled', 'check_service'):
            if type(self.transmission[key]) is not bool:
                raise ValueError(f'{key}: ожидается да/нет.')
        for key in ('start_text', 'end_text'):
            if not isinstance(self.transmission[key], str):
                raise ValueError('Служебный сигнал должен быть текстом.')
        for key in ('pause_after_start_ms', 'pause_before_end_ms'):
            number(self.transmission[key], 0, 10000, key)
        for key, low, high in [('service_speed_wpm', 5, 40), ('service_tone_hz', 300, 1200)]:
            if self.transmission[key] is not None:
                number(self.transmission[key], low, high, key)
        for key, low, high in [('element_units', 1, 3), ('letter_pause_ms', 100, 3000),
                              ('group_pause_ms', 100, 10000), ('line_pause_ms', 100, 15000), ('repeat_pause_ms', 0, 15000)]:
            number(self.spacing[key], low, high, key)
        pauses = self.spacing['custom_group_pauses_ms']
        if not isinstance(pauses, list):
            raise ValueError('Индивидуальные паузы должны быть списком.')
        for value in pauses:
            if value is not None:
                number(value, 100, 10000, 'Индивидуальная пауза')
        g = self.grouping
        if g['mode'] not in ('none', 'fixed', 'custom', 'manual'):
            raise ValueError('Неизвестная группировка.')
        if type(g['size']) is not int or not 1 <= g['size'] <= 20:
            raise ValueError('Размер группы: от 1 до 20.')
        if not isinstance(g['sizes'], list) or any(type(n) is not int or not 1 <= n <= 20 for n in g['sizes']):
            raise ValueError('Размеры групп должны быть целыми числами от 1 до 20.')
        if g['mode'] == 'custom' and not g['sizes']:
            raise ValueError('Задай размеры групп.')
        if self.mode not in ('classic', 'standard') and not self.experimental:
            dot_ms = 1200 / self.speed_wpm
            if self.spacing['element_units'] != 1:
                raise ValueError('Нестандартный ритм внутри букв требует экспериментального режима.')
            for key, minimum in [('letter_pause_ms', 3 * dot_ms), ('group_pause_ms', 7 * dot_ms), ('line_pause_ms', 7 * dot_ms)]:
                if self.spacing[key] + 1e-8 < minimum:
                    raise ValueError(f'{key}: для {self.speed_wpm} WPM нужно не менее {minimum:g} мс.')
            if any(p is not None and p < 3 * dot_ms for p in pauses):
                raise ValueError('Индивидуальная пауза короче стандартной межбуквенной. Включи экспериментальный режим.')
        return self

    def with_level(self, name):
        speed, letter, group = LEVELS[name]
        data = asdict(self)
        data.update(speed_wpm=speed, difficulty=name)
        data['spacing'].update(letter_pause_ms=letter, group_pause_ms=group)
        return Settings.from_dict(data)

    def save(self, path):
        self.validate()
        Path(path).write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding='utf-8')

    @classmethod
    def load(cls, path):
        return cls.from_dict(json.loads(Path(path).read_text(encoding='utf-8-sig')))


@dataclass(frozen=True)
class Group:
    text: str
    line: int
    number: int


@dataclass(frozen=True)
class Event:
    kind: str
    seconds: float
    stage: str
    group: int = 0
    letter: str = ''
    symbol_index: int = -1
    tone_hz: float = 700
    boundary: str = ''
    repeat: int = 1


@dataclass
class Plan:
    events: list
    groups: list
    expected: str
    display: str
    warnings: list


def parse_groups(settings, alphabet):
    settings.validate()
    groups = []
    for line_no, line in enumerate(settings.text.upper().splitlines(), 1):
        if not line.strip():
            continue
        letters = ''.join(line.replace('|', '').split())
        if any(c not in alphabet for c in letters):
            raise ValueError('Текст содержит неизвестные символы. Разрешены русские буквы, пробелы, | и переносы строк.')
        if not letters:
            continue
        g = settings.grouping
        if g['mode'] == 'manual':
            parts = [part for part in re.split(r'[\s|]+', line.strip()) if part]
        elif g['mode'] == 'none':
            parts = [letters]
        else:
            sizes = g['sizes'] if g['mode'] == 'custom' else [g['size']] * math.ceil(len(letters) / g['size'])
            if g['mode'] == 'custom' and sum(sizes) > len(letters):
                raise ValueError(f'Сумма размеров групп ({sum(sizes)}) больше длины строки {line_no} ({len(letters)}). Текст не обрезан.')
            parts = []
            offset = 0
            for size in sizes:
                parts.append(letters[offset:offset + size])
                offset += size
            if offset < len(letters):
                parts.append(letters[offset:])
        first_number = len(groups) + 1
        groups.extend(Group(part, line_no, first_number + i) for i, part in enumerate(parts))
    if not groups:
        raise ValueError('Введи непустой основной текст.')
    return groups


def build_plan(settings, alphabet):
    groups = parse_groups(settings, alphabet)
    events = []
    classic = settings.mode in ('classic', 'standard')
    separators = settings.mode in ('separators', 'full') and settings.separators_enabled
    services = settings.mode in ('standard', 'service', 'full')
    tx, spacing = settings.transmission, settings.spacing
    start = ''.join(tx['start_text'].upper().split()) if services and tx['start_enabled'] else ''
    end = ''.join(tx['end_text'].upper().split()) if services and tx['end_enabled'] else ''
    if any(c not in alphabet for c in start + end):
        raise ValueError('Служебные сигналы содержат неизвестные буквы.')
    custom = spacing['custom_group_pauses_ms']
    if len(custom) > max(0, len(groups) - 1):
        raise ValueError('Индивидуальных пауз больше, чем границ групп.')
    warnings = ['Экспериментальный режим: возможен нестандартный ритм Морзе.'] if settings.experimental else []
    def pause(ms, boundary, repeat):
        if ms > 0:
            events.append(Event('silence', ms / 1000, 'ПАУЗА', boundary=boundary, repeat=repeat))
    def letters(text, stage, group, repeat, service=False):
        speed = (tx['service_speed_wpm'] or settings.speed_wpm) if service else settings.speed_wpm
        tone = (tx['service_tone_hz'] or settings.tone_hz) if service else settings.tone_hz
        dot = 1.2 / speed
        for ci, char in enumerate(text):
            for si, symbol in enumerate(alphabet[char]):
                events.append(Event('tone', dot * (1 if symbol == '.' else 3), stage, group, char, si, tone, repeat=repeat))
                if si < len(alphabet[char]) - 1:
                    events.append(Event('silence', dot * (1 if classic else spacing['element_units']), 'ПАУЗА', group, repeat=repeat))
            if ci < len(text) - 1:
                pause(dot * 3000 if classic else max(dot * 3000, spacing['letter_pause_ms']) if service and not settings.experimental else spacing['letter_pause_ms'], 'letter', repeat)
    for repeat in range(1, settings.repeats + 1):
        if repeat > 1:
            pause(spacing['repeat_pause_ms'], 'repeat', repeat)
        if start:
            letters(start, 'ПЕРЕДАЧА НАЧАЛЬНОГО СИГНАЛА', 0, repeat, True)
            pause(tx['pause_after_start_ms'], 'start', repeat)
        for i, group in enumerate(groups):
            if i:
                previous = groups[i - 1]
                if classic:
                    ms, boundary = 3600 / settings.speed_wpm, 'letter'
                elif i - 1 < len(custom) and custom[i - 1] is not None:
                    ms, boundary = custom[i - 1], 'custom'
                elif previous.line != group.line:
                    ms, boundary = spacing['line_pause_ms'], 'line'
                elif separators:
                    ms, boundary = spacing['group_pause_ms'], 'group'
                else:
                    ms, boundary = spacing['letter_pause_ms'], 'letter'
                pause(ms, boundary, repeat)
            letters(group.text, f'ПЕРЕДАЧА ГРУППЫ № {group.number}', group.number, repeat)
        if end:
            pause(tx['pause_before_end_ms'], 'end', repeat)
            letters(end, 'ПЕРЕДАЧА КОНЕЧНОГО СИГНАЛА', 0, repeat, True)
    main = ''.join(g.text for g in groups)
    expected = (start + main + end if tx['check_service'] else main) * settings.repeats
    display = (' | ' if separators else ' ').join(g.text for g in groups)
    return Plan(events, groups, expected, display, warnings)


def render_plan(plan, stop_event, sample_rate=22050):
    import numpy as np
    counts = [max(1, round(e.seconds * sample_rate)) for e in plan.events]
    total = sum(counts)
    if total > sample_rate * 1200:
        raise ValueError('Передача длиннее 20 минут. Уменьши текст, повторы или паузы.')
    audio = np.zeros(total, dtype=np.float32)
    marks = []
    offset = 0
    for event, count in zip(plan.events, counts):
        if stop_event.is_set():
            return None, []
        if event.kind == 'tone':
            t = np.arange(count, dtype=np.float32) / sample_rate
            wave = (0.5 * np.sin(2 * np.pi * event.tone_hz * t)).astype(np.float32)
            edge = min(round(sample_rate * 0.005), count // 2)
            if edge:
                ramp = np.linspace(0, 1, edge, dtype=np.float32)
                wave[:edge] *= ramp
                wave[-edge:] *= ramp[::-1]
            audio[offset:offset + count] = wave
        marks.append((offset, offset + count, event))
        offset += count
    return audio, marks
