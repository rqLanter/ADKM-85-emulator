import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import learning
import adkm85


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'progress.json'
        self.store = learning.ProgressStore(self.path)

    def tearDown(self):
        self.folder.cleanup()

    def test_unassisted_confusions_and_reload(self):
        errors, aligned = adkm85.compare_answer('САБ', 'ХА')
        self.store.record('1', 'table', 'САБ', 'ХА', aligned, errors, eligible=True)
        again = learning.ProgressStore(self.path)
        self.assertEqual(again.data['attempts'], 1)
        self.assertIn(('С', 'Х', 1), again.confusions())
        self.assertIn(('Б', '·', 1), again.confusions())
        self.assertEqual(set(again.problem_letters(adkm85.MORSE)), {'С', 'Х', 'Б'})

    def test_no_duplicate_or_assisted_counts(self):
        alignment = [('А', 'Б', 'wrong')]
        self.assertTrue(self.store.record('1', 'training', 'А', 'Б', alignment, 1, eligible=False))
        self.assertFalse(self.store.record('1', 'training', 'А', 'Б', alignment, 1, eligible=True))
        self.assertEqual(self.store.confusions(), [])
        self.assertEqual(len(self.store.data['history']), 1)

    def test_incomplete_exam_has_no_grade(self):
        self.store.record('1', 'exam', 'АБ', status='incomplete', eligible=True)
        row = self.store.data['history'][0]
        self.assertIsNone(row['grade'])
        self.assertIsNone(row['score'])
        self.assertFalse(row['eligible'])
        self.assertEqual(self.store.data['attempts'], 0)

    def test_corrupt_history_is_preserved(self):
        self.path.write_text('{broken', encoding='utf-8')
        store = learning.ProgressStore(self.path)
        self.assertIsNotNone(store.error)
        with self.assertRaises(OSError):
            store.record('1', 'exam', 'А')
        self.assertEqual(self.path.read_text(encoding='utf-8'), '{broken')

    def test_failed_save_does_not_commit_in_memory(self):
        with patch.object(Path, 'replace', side_effect=OSError('denied')):
            with self.assertRaises(OSError):
                self.store.record('1', 'table', 'А')
        self.assertEqual(self.store.data['history'], [])

    def test_delete_subtracts_only_selected_attempts_and_persists(self):
        errors, aligned = adkm85.compare_answer('СА', 'ХА')
        for i in range(3):
            self.store.record(str(i), 'table', 'СА', 'ХА', aligned, errors, True)
        self.store.record('assisted', 'training', 'СА', 'ХА', aligned, errors, False)
        self.assertEqual(self.store.delete_attempts(['0', 'assisted', 'unknown']), 2)
        store = learning.ProgressStore(self.path)
        self.assertEqual(store.data['attempts'], 2)
        self.assertEqual(store.confusions(), [('С', 'Х', 2)])
        self.assertEqual(len(store.data['history']), 2)
        self.assertEqual(store.delete_attempts(['0']), 0)

    def test_delete_keeps_totals_from_archived_attempts(self):
        self.store.data['attempts'] = 10
        self.store.data['confusions'] = {'С|Х': 10}
        self.store.record('visible', 'table', 'С', 'Х', [('С', 'Х', 'wrong')], 1, True)
        self.store.delete_attempts(['visible'])
        self.assertEqual(self.store.data['attempts'], 10)
        self.assertEqual(self.store.confusions(), [('С', 'Х', 10)])
        self.store.clear_history()
        self.assertEqual(learning.ProgressStore(self.path).data,
                         {'version': 1, 'history': [], 'confusions': {}, 'attempts': 0})

    def test_failed_delete_and_clear_leave_data_unchanged(self):
        self.store.record('1', 'table', 'А', 'Б', [('А', 'Б', 'wrong')], 1, True)
        before = json.loads(json.dumps(self.store.data))
        with patch.object(Path, 'replace', side_effect=OSError('denied')):
            for operation in [lambda: self.store.delete_attempts(['1']), self.store.clear_history]:
                with self.assertRaises(OSError):
                    operation()
                self.assertEqual(self.store.data, before)
        self.assertEqual(learning.ProgressStore(self.path).data, before)

    def test_grade_boundaries(self):
        self.assertEqual([learning.score_result(20, n)[1] for n in (0, 1, 2, 3, 4, 6, 7, 30)], [5, 5, 4, 4, 3, 3, 2, 2])


class LearningInterfaceTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.folder = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.warnings = patch.object(adkm85.messagebox, 'showwarning')
        self.warning_mock = self.warnings.start()
        self.app = adkm85.App(self.root, learning.ProgressStore(Path(self.folder.name) / 'progress.json'))
        self.root.update()

    def tearDown(self):
        self.app.close()
        self.warning_mock.assert_not_called()
        self.warnings.stop()
        self.folder.cleanup()

    def exam(self):
        self.app.mode_tabs.select(2)
        self.app.change_mode()
        self.root.update()

    def play_to_end(self):
        with patch.object(adkm85.threading, 'Thread'):
            self.app.start()
        self.app.events.put(('done', None))
        self.root.after_cancel(self.app.poll_id)
        self.app.poll_audio()

    def type_answer(self, text):
        self.app.answer.insert('1.0', text)
        self.root.update()

    def test_exam_sealed_single_play_and_single_submission(self):
        self.exam()
        self.assertEqual(self.app.exam_state, 'idle')
        self.assertIn('disabled', self.app.morse_toggle.state())
        self.assertIn('disabled', self.app.original_toggle.state())
        self.assertNotIn(self.app.exam_text, self.app.preview.cget('text'))
        self.app.check()
        self.assertEqual(self.app.store.data['history'], [])
        self.play_to_end()
        self.assertEqual(self.app.exam_state, 'answer')
        with patch.object(adkm85.threading, 'Thread') as thread:
            self.app.start()
            thread.assert_not_called()
        self.type_answer(self.app.get_text())
        self.app.check()
        self.assertEqual(self.app.exam_state, 'submitted')
        self.assertEqual(self.app.store.data['history'][-1]['grade'], 5)
        self.assertTrue(self.app.store.data['history'][-1]['eligible'])
        self.app.check()
        self.assertEqual(len(self.app.store.data['history']), 1)
        self.app.clear_answer()
        self.assertEqual(self.app.answer.get('1.0', 'end').strip(), self.app.get_text())

    def test_stop_exam_and_switch_do_not_grade(self):
        self.exam()
        self.play_to_end()
        self.app.stop()
        self.app.check()
        self.assertEqual(self.app.exam_state, 'incomplete')
        self.assertEqual(self.app.store.data['history'][-1]['status'], 'incomplete')
        self.app.new_exam()
        self.play_to_end()
        self.app.mode_tabs.select(0)
        self.app.change_mode()
        self.assertEqual(self.app.store.data['history'][-1]['status'], 'incomplete')
        self.assertEqual(len(self.app.store.data['history']), 2)

    def test_hint_exposure_is_latched(self):
        self.app.show_text.set(True)
        self.app.show_text.set(False)
        self.play_to_end()
        self.type_answer(self.app.get_text())
        self.app.check()
        self.assertFalse(self.app.store.data['history'][-1]['eligible'])
        self.app.check()
        self.assertEqual(len(self.app.store.data['history']), 1)

    def test_empty_exam_answer_is_a_completed_grade_two(self):
        self.exam()
        self.play_to_end()
        self.app.check()
        row = self.app.store.data['history'][-1]
        self.assertEqual((row['grade'], row['errors'], row['status']), (2, 20, 'completed'))

    def test_remedial_training_uses_confused_pair(self):
        errors, aligned = adkm85.compare_answer('С', 'Х')
        self.app.store.record('seed', 'table', 'С', 'Х', aligned, errors, True)
        self.app.open_errors()
        dialog = next(w for w in self.root.winfo_children() if isinstance(w, adkm85.tk.Toplevel))
        frame = dialog.winfo_children()[0]
        button = next(w for w in frame.winfo_children() if isinstance(w, adkm85.ttk.Button))
        button.invoke()
        self.root.update()
        self.assertEqual(self.app.mode, 'training')
        self.assertEqual(self.app.practice_kind, 'mistakes')
        self.assertEqual(len(self.app.get_text()), 20)
        self.assertLessEqual(set(self.app.get_text()), set('СХ'))
        self.assertFalse(self.app.show_morse.get())
        self.assertFalse(self.app.hints_used)

    def test_history_deletion_confirmation_and_refresh(self):
        self.app.store.record('seed', 'table', 'С', 'Х', [('С', 'Х', 'wrong')], 1, True)
        self.app.open_errors()
        def descendants(widget):
            return [child for item in widget.winfo_children() for child in [item, *descendants(item)]]
        widgets = descendants(self.root)
        trees = [w for w in widgets if isinstance(w, adkm85.ttk.Treeview)]
        history = next(w for w in trees if 'date' in w['columns'])
        buttons = {w.cget('text'): w for w in widgets if isinstance(w, adkm85.ttk.Button)}
        history.selection_set(history.get_children()[0])
        self.root.update()
        with patch.object(adkm85.messagebox, 'askyesno', return_value=False):
            buttons['Удалить выбранные'].invoke()
        self.assertEqual(len(self.app.store.data['history']), 1)
        with patch.object(adkm85.messagebox, 'askyesno', return_value=True):
            buttons['Удалить выбранные'].invoke()
        self.root.update()
        self.assertEqual(history.get_children(), ())
        self.assertEqual(self.app.store.confusions(), [])
        self.assertIn('disabled', buttons['Удалить выбранные'].state())
        self.assertIn('disabled', buttons['Тренировать проблемные буквы'].state())
        self.app.store.record('again', 'table', 'А')
        # A fresh window should see persisted changes and clear all rows.
        dialog = next(w for w in self.root.winfo_children() if isinstance(w, adkm85.tk.Toplevel))
        dialog.destroy()
        self.app.open_errors()
        buttons = {w.cget('text'): w for w in descendants(self.root) if isinstance(w, adkm85.ttk.Button)}
        with patch.object(adkm85.messagebox, 'askyesno', return_value=True):
            buttons['Очистить всю историю'].invoke()
        self.assertEqual(self.app.store.data['history'], [])
        next(w for w in self.root.winfo_children() if isinstance(w, adkm85.tk.Toplevel)).destroy()

    def test_shared_radio_settings_and_service_switches(self):
        from radiogram import Settings
        settings=Settings.from_dict({'text':'АБВ', 'speed_wpm':15, 'tone_hz':800,
                                    'spacing':{'letter_pause_ms':600,'group_pause_ms':1400}})
        self.app.apply_radio_settings(settings)
        self.assertEqual((self.app.speed.get(),self.app.tone.get()),(15,800))
        plan=self.app.main_plan()
        self.assertEqual(plan.expected,self.app.get_text())
        self.assertEqual(sum(e.symbol_index==0 and e.kind=='tone' for e in plan.events),24)
        self.app.start_signal.set(False);self.app.end_signal.set(False)
        self.assertEqual(sum(e.symbol_index==0 and e.kind=='tone' for e in self.app.main_plan().events),20)
        self.app.mode_tabs.select(1);self.app.change_mode()
        self.assertEqual(self.app.main_plan().expected,self.app.get_text())
        self.assertNotIn('disabled',self.app.start_toggle.state())

    def test_training_optional_service_signals(self):
        self.app.mode_tabs.select(1)
        self.app.change_mode()
        text=self.app.get_text()
        for start,end in ((False,False),(True,False),(False,True),(True,True)):
            with self.subTest(start=start,end=end):
                self.app.start_signal.set(start)
                self.app.end_signal.set(end)
                plan=self.app.main_plan()
                transmitted=''.join(e.letter for e in plan.events if e.kind=='tone' and e.symbol_index==0)
                self.assertEqual(transmitted,('ЖЖЖ' if start else '')+text+('К' if end else ''))
                self.assertEqual(plan.expected,text)
        self.play_to_end()
        self.type_answer(text)
        self.app.check()
        self.assertIn('Ошибок: 0',self.app.result.cget('text'))

    def test_radio_dialog_applies_to_main_window(self):
        from radiogram_ui import RadiogramDialog
        dialog=RadiogramDialog(self.app,adkm85.MORSE,adkm85.play_samples)
        self.root.update()
        self.assertEqual(dialog.text.get('1.0','end').strip(),self.app.get_text())
        dialog.vars['speed_wpm'].set(15)
        dialog.vars['spacing.letter_pause_ms'].set(600)
        dialog.vars['spacing.group_pause_ms'].set(1400)
        dialog.apply_to_app()
        self.assertEqual(self.app.speed.get(),15)
        self.assertEqual(self.app.radio_settings.spacing['letter_pause_ms'],600)
        dialog.close()

    def test_explicit_standard_morse_matches_old_audio(self):
        import threading
        import numpy as np
        from radiogram import render_plan
        self.assertEqual(self.app.transmission_kind.get(),'Стандартный Морзе')
        plan=self.app.main_plan()
        audio,_=render_plan(plan,threading.Event())
        old=adkm85.make_audio(self.app.get_text(),20,700,0,threading.Event())
        np.testing.assert_array_equal(audio,old)

    def test_alphabet_and_errors_windows(self):
        self.app.open_alphabet()
        dialogs = [w for w in self.root.winfo_children() if isinstance(w, adkm85.tk.Toplevel)]
        self.assertEqual(len(dialogs), 1)
        dialogs[0].protocol('WM_DELETE_WINDOW')
        self.root.tk.call(dialogs[0].protocol('WM_DELETE_WINDOW'))
        self.app.open_errors()
        dialogs = [w for w in self.root.winfo_children() if isinstance(w, adkm85.tk.Toplevel)]
        self.assertEqual(len(dialogs), 1)
        dialogs[0].destroy()


if __name__ == '__main__':
    unittest.main()
