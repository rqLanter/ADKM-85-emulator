import unittest, threading, tempfile
from pathlib import Path
from dataclasses import asdict
import numpy as np
from radiogram import Settings, build_plan, render_plan, LEVELS
from adkm85 import MORSE, make_audio

class RadiogramTests(unittest.TestCase):
    def config(self,**kwargs): return Settings.from_dict({'text':'АБВГДЕЖЗИЙКЛМНОПРСТУ',**kwargs})
    def test_service_and_exact_pauses(self):
        plan=build_plan(self.config(),MORSE)
        letters=[e.letter for e in plan.events if e.kind=='tone' and e.symbol_index==0]
        self.assertEqual(''.join(letters),'ЖЖЖАБВГДЕЖЗИЙКЛМНОПРСТУК')
        self.assertEqual([e.seconds for e in plan.events if e.boundary=='start'],[1.5])
        self.assertEqual([e.seconds for e in plan.events if e.boundary=='end'],[1])
        self.assertEqual(len(plan.expected),20)
    def test_four_modes(self):
        for mode,services,separators in [('ordinary',False,False),('separators',False,True),('service',True,False),('full',True,True)]:
            plan=build_plan(self.config(mode=mode),MORSE)
            self.assertEqual(any(e.boundary=='start' for e in plan.events),services)
            self.assertEqual(any(e.boundary=='group' for e in plan.events),separators)
    def test_group_sizes_and_remainder(self):
        for size in (3,5,10):
            plan=build_plan(self.config(grouping={'size':size}),MORSE)
            self.assertEqual(''.join(g.text for g in plan.groups),self.config().text)
            self.assertEqual([g.number for g in plan.groups],list(range(1,len(plan.groups)+1)))
        plan=build_plan(self.config(grouping={'mode':'custom','sizes':[3,5,2]}),MORSE)
        self.assertEqual([len(g.text) for g in plan.groups],[3,5,2,10])
        with self.assertRaises(ValueError): build_plan(self.config(grouping={'mode':'custom','sizes':[20,1]}),MORSE)
    def test_manual_long_text_and_lines(self):
        text='АБ | ВГ ДЕ\nЖЗ | ИЙ'*3
        plan=build_plan(self.config(text=text,grouping={'mode':'manual'}),MORSE)
        self.assertGreater(len(plan.expected),20)
        self.assertTrue(any(e.boundary=='line' for e in plan.events))
        self.assertFalse(any(e.letter=='|' for e in plan.events))
    def test_priority_no_double_pauses(self):
        plan=build_plan(self.config(spacing={'custom_group_pauses_ms':[500,1000,1500]}),MORSE)
        self.assertEqual([e.seconds for e in plan.events if e.boundary=='custom'],[.5,1,1.5])
        self.assertFalse(any(e.boundary=='group' for e in plan.events))
        # Silence between individual elements is separated by a tone; no silence stacking.
        self.assertFalse(any(a.kind==b.kind=='silence' for a,b in zip(plan.events,plan.events[1:])))
    def test_classic_matches_original(self):
        s=self.config(mode='classic')
        audio,_=render_plan(build_plan(s,MORSE),threading.Event())
        old=make_audio(s.text,s.speed_wpm,s.tone_hz,0,threading.Event())
        np.testing.assert_array_equal(audio,old)
    def test_levels_and_experimental(self):
        for name,(speed,letter,group) in LEVELS.items():
            s=self.config().with_level(name)
            self.assertEqual((s.speed_wpm,s.spacing['letter_pause_ms'],s.spacing['group_pause_ms']),(speed,letter,group))
        with self.assertRaises(ValueError): self.config(spacing={'element_units':2})
        self.config(experimental=True,spacing={'element_units':2})
        with self.assertRaises(ValueError): self.config(speed_wpm=5)
    def test_repeats_and_check_services(self):
        s=self.config(repeats=2,transmission={'check_service':True})
        plan=build_plan(s,MORSE)
        self.assertEqual(plan.expected,('ЖЖЖ'+s.text+'К')*2)
        self.assertEqual(len([e for e in plan.events if e.boundary=='repeat']),1)
    def test_json_and_invalid_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'preset.json'; s=self.config(); s.save(path)
            self.assertEqual(asdict(s),asdict(Settings.load(path)))
        for value in ({'repeats':True},{'speed_wpm':float('nan')},{'spacing':{'group_pause_ms':1}},{'unknown':1}):
            with self.subTest(value=value),self.assertRaises(ValueError): self.config(**value)
    def test_cancellation_at_every_event(self):
        plan=build_plan(self.config(),MORSE)
        class Stop:
            def __init__(self,n): self.n=n
            def is_set(self): self.n-=1; return self.n<0
        for i in range(len(plan.events)):
            audio,marks=render_plan(plan,Stop(i))
            self.assertIsNone(audio); self.assertEqual(marks,[])

if __name__=='__main__': unittest.main()
