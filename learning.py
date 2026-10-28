"""Local learning history and error statistics, independent of the interface."""
import json
import os
from pathlib import Path
from datetime import datetime, timezone


def score_result(total, errors):
    score = round(max(0, total - errors) / total * 100, 1) if total else 0.0
    grade = 5 if score >= 95 else 4 if score >= 85 else 3 if score >= 70 else 2
    return score, grade


def compare_answer(original, typed):
    """Minimum edit alignment: a missing letter does not shift every later answer."""
    rows, cols = len(original), len(typed)
    costs = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows + 1):
        costs[i][0] = i
    for j in range(cols + 1):
        costs[0][j] = j
    for i in range(1, rows + 1):
        for j in range(1, cols + 1):
            costs[i][j] = min(costs[i - 1][j] + 1, costs[i][j - 1] + 1,
                              costs[i - 1][j - 1] + (original[i - 1] != typed[j - 1]))
    aligned = []
    i, j = rows, cols
    while i or j:
        if i and j and costs[i][j] == costs[i - 1][j - 1] + (original[i - 1] != typed[j - 1]):
            aligned.append((original[i - 1], typed[j - 1],
                            'ok' if original[i - 1] == typed[j - 1] else 'wrong'))
            i, j = i - 1, j - 1
        elif i and costs[i][j] == costs[i - 1][j] + 1:
            aligned.append((original[i - 1], '·', 'missing'))
            i -= 1
        else:
            aligned.append(('·', typed[j - 1], 'extra'))
            j -= 1
    return costs[rows][cols], list(reversed(aligned))


class ProgressStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'ADKM85' / 'progress.json'
        self.data = {'version': 1, 'history': [], 'confusions': {}, 'attempts': 0}
        self.error = None
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding='utf-8'))
                if (data.get('version') != 1 or not isinstance(data.get('history'), list)
                        or not isinstance(data.get('confusions'), dict)
                        or not isinstance(data.get('attempts'), int) or data['attempts'] < 0):
                    raise ValueError('неверный формат истории')
                if any(not isinstance(k, str) or len(k.split('|')) != 2 or not isinstance(v, int) or v < 0
                       for k, v in data['confusions'].items()):
                    raise ValueError('неверный формат статистики')
                if any(not isinstance(row, dict) for row in data['history']):
                    raise ValueError('неверный формат попыток')
                self.data = data
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self.error = f'Не удалось прочитать историю: {exc}. Файл: {self.path}'

    def record(self, attempt_id, kind, original, typed='', alignment=(), errors=0,
               eligible=False, status='completed', number=None):
        if self.error:
            raise OSError(self.error)
        if any(row.get('id') == attempt_id for row in self.data['history']):
            return False
        # Commit only after an atomic file replacement succeeds.
        data = json.loads(json.dumps(self.data))
        score, grade = score_result(len(original), errors)
        row = {'id': attempt_id, 'at': datetime.now(timezone.utc).isoformat(), 'kind': kind,
               'status': status, 'number': number, 'original': original, 'typed': typed,
               'errors': errors if status == 'completed' else None,
               'score': score if status == 'completed' else None,
               'grade': grade if status == 'completed' else None, 'eligible': eligible and status == 'completed'}
        data['history'].append(row)
        data['history'] = data['history'][-500:]
        if row['eligible']:
            data['attempts'] += 1
            for expected, received, tag in alignment:
                if tag != 'ok':
                    key = expected + '|' + received
                    data['confusions'][key] = data['confusions'].get(key, 0) + 1
        self._commit(data)
        return True

    def _commit(self, data):
        if self.error:
            raise OSError(self.error)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.path)
        self.data = data

    def delete_attempts(self, attempt_ids):
        ids = set(attempt_ids)
        removed = [row for row in self.data['history'] if row.get('id') in ids]
        if not removed:
            return 0
        data = json.loads(json.dumps(self.data))
        data['history'] = [row for row in data['history'] if row.get('id') not in ids]
        # Subtract only these attempts; older totals may outlive the 500-row history.
        for row in removed:
            if row.get('eligible') and row.get('status') == 'completed':
                data['attempts'] = max(0, data['attempts'] - 1)
                _, aligned = compare_answer(row['original'], row['typed'])
                for expected, received, tag in aligned:
                    if tag != 'ok':
                        key = expected + '|' + received
                        count = data['confusions'].get(key, 0) - 1
                        if count > 0:
                            data['confusions'][key] = count
                        else:
                            data['confusions'].pop(key, None)
        self._commit(data)
        return len(removed)

    def clear_history(self):
        self._commit({'version': 1, 'history': [], 'confusions': {}, 'attempts': 0})

    def confusions(self):
        return sorted(((key.split('|')[0], key.split('|')[1], value)
                       for key, value in self.data['confusions'].items()), key=lambda row: (-row[2], row[0], row[1]))

    def problem_letters(self, alphabet):
        weights = {}
        for expected, received, count in self.confusions():
            for letter in {expected, received}:
                if letter in alphabet:
                    weights[letter] = weights.get(letter, 0) + count
        return ''.join(sorted(weights, key=lambda letter: (-weights[letter], letter))[:10])
