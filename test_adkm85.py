import threading
import tempfile
import queue
import random
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
import adkm85


class ExerciseTests(unittest.TestCase):
    def test_complete_photo_bank(self):
        rows = adkm85.load_rows()
        self.assertEqual(list(rows), list(range(1, 151)))
        self.assertTrue(all(len(text) == 20 and set(text) <= set(adkm85.MORSE) for text in rows.values()))
        self.assertEqual(rows[1], 'БРЗУНПЧСЬРЩЮНЙТТХМЛЦ')
        self.assertEqual(rows[150], 'КФОАБХГЛКЮШАЬЕТЯАЧФК')
        self.assertEqual(rows[76], rows[77])

    def test_invalid_bank_is_reported(self):
        for content in ['1;АБВ', '1;' + 'А' * 20 + '\n1;' + 'Б' * 20, '1;' + 'X' * 20]:
            with self.subTest(content=content), patch('pathlib.Path.read_text', return_value=content):
                with self.assertRaises(ValueError):
                    adkm85.load_rows()

    def test_exe_uses_external_bank_and_embedded_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = str(Path(folder) / 'ADKM85.exe')
            with patch.object(adkm85.sys, 'frozen', True, create=True), patch.object(adkm85.sys, 'executable', exe):
                self.assertEqual(len(adkm85.load_rows()), 150)
                bank = Path(folder) / 'texts.txt'
                bank.write_text('7;' + 'Б' * 20, encoding='utf-8')
                self.assertEqual(adkm85.load_rows(), {7: 'Б' * 20})
                bank.write_text('7;ошибка', encoding='utf-8')
                with self.assertRaises(ValueError):
                    adkm85.load_rows()

    def test_exact_answer(self):
        errors, alignment = adkm85.compare_answer('АБВГД', 'АБВГД')
        self.assertEqual(errors, 0)
        self.assertTrue(all(tag == 'ok' for _, _, tag in alignment))

    def test_missing_letter_does_not_shift_rest(self):
        errors, alignment = adkm85.compare_answer('АБВГД', 'АВГД')
        self.assertEqual(errors, 1)
        self.assertEqual(sum(tag == 'ok' for _, _, tag in alignment), 4)
        self.assertIn(('Б', '·', 'missing'), alignment)

    def test_extra_and_wrong_letters(self):
        errors, alignment = adkm85.compare_answer('АБВГД', 'АБЯВГД')
        self.assertEqual(errors, 1)
        self.assertIn(('·', 'Я', 'extra'), alignment)
        self.assertEqual(adkm85.compare_answer('АБВ', 'АБЯ')[0], 1)
        self.assertEqual(adkm85.compare_answer('АБВ', '')[0], 3)

    def test_audio_timing_and_levels(self):
        audio = adkm85.make_audio('Е', 20, 700, 0, threading.Event())
        self.assertEqual(len(audio), round(22050 * 1.2 / 20))
        self.assertEqual(audio.dtype, np.float32)
        self.assertLessEqual(float(np.max(np.abs(audio))), 0.5)
        dash = adkm85.make_audio('Т', 20, 700, 0, threading.Event())
        self.assertEqual(len(dash), 3 * len(audio))
        noisy = adkm85.make_audio('ЕТ', 20, 700, 100, threading.Event())
        self.assertTrue(np.all(np.abs(noisy) <= 1))

    def test_custom_training(self):
        text = adkm85.generate_training('а о а', 20, random.Random(42))
        self.assertEqual(len(text), 20)
        self.assertEqual(set(text), {'А', 'О'})
        self.assertEqual(adkm85.generate_training('Б', 1), 'Б')
        for alphabet, length in [('', 20), ('ABC', 20), ('А!', 5), ('АО', 0), ('АО', 100)]:
            with self.subTest(alphabet=alphabet, length=length), self.assertRaises(ValueError):
                adkm85.generate_training(alphabet, length)

    def test_morse_marks_share_audio_sample_boundaries(self):
        marks = []
        audio = adkm85.make_audio('А Т', 20, 700, 0, threading.Event(), marks)
        unit = round(22050 * 1.2 / 20)
        self.assertEqual(marks, [(0, unit, 'А', 0), (2 * unit, 5 * unit, 'А', 1),
                                 (12 * unit, 15 * unit, 'Т', 0)])
        self.assertEqual(len(audio), marks[-1][1])
        self.assertTrue(np.all(audio[unit:2 * unit] == 0))
        # Fractional rounding cannot accumulate differently in sound and display.
        marks = []
        audio = adkm85.make_audio('ЖЩ', 17, 700, 0, threading.Event(), marks)
        self.assertEqual(marks[-1][1], len(audio))
        self.assertEqual(len(marks), len(adkm85.MORSE['Ж']) + len(adkm85.MORSE['Щ']))

    def test_stream_clock_drives_display_and_finishes(self):
        app = adkm85.App.__new__(adkm85.App)
        app.stop_event = threading.Event()
        app.events = queue.Queue()
        blocks = []

        class FakeStream:
            def __init__(self, **kwargs):
                self.callback = kwargs['callback']
                self.finished = kwargs['finished_callback']

            def __enter__(self):
                for _ in range(100):
                    block = np.empty((256, 1), dtype=np.float32)
                    try:
                        self.callback(block, 256, SimpleNamespace(outputBufferDacTime=100.1, currentTime=100.0), None)
                    except adkm85.sd.CallbackStop:
                        blocks.append(block.copy())
                        self.finished()
                        break
                    blocks.append(block.copy())
                return self

            def __exit__(self, *_):
                pass

        with patch.object(adkm85.sd, 'OutputStream', FakeStream), patch.object(adkm85.time, 'monotonic', return_value=10):
            app.play('А', 20, 700, 0)
        event, (started, duration, marks) = app.events.get_nowait()
        self.assertEqual(event, 'playing')
        self.assertAlmostEqual(started, 10.1)
        self.assertGreater(duration, 0)
        self.assertEqual(len(marks), 2)
        self.assertEqual(app.events.get_nowait(), ('done', None))
        sound = np.concatenate(blocks)[:, 0]
        self.assertTrue(np.any(sound != 0))
        self.assertTrue(np.all(sound[marks[-1][1]:] == 0))

    def test_audio_cancellation(self):
        stop = threading.Event()
        stop.set()
        self.assertIsNone(adkm85.make_audio('АБВ', 20, 700, 0, stop))


if __name__ == '__main__':
    unittest.main()
