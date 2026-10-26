"""Strict, content-free probe evidence and two-round policy classification.

Only ``sanitize_evidence`` output may be persisted. Unknown fields are dropped;
known fields reject malformed values rather than silently trusting diagnostics.
Round statuses verified/suspect denote calibrated scores, not final policy.
"""
import math
import re


SAFE_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/+\-]{0,199}\Z')
SAFE_ERROR = re.compile(r'[A-Za-z0-9_:-]{1,80}\Z')
STATUSES = ('verified', 'suspect', 'unknown', 'error')
FINISH_REASONS = ('stop', 'length', 'tool_calls', 'function_call', 'content_filter', None)
CONFIRMATION_STATUSES = ('not_required', 'pending', 'confirmed', 'recovered', 'inconclusive')


def _integer(value, minimum, maximum, field):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('invalid_' + field)
    return value


def _boolean(value, field):
    if type(value) is not bool:
        raise ValueError('invalid_' + field)
    return value


def _number(value, minimum, maximum, field, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError('invalid_' + field)
    return float(value)


def _safe(value, regex, field):
    if not isinstance(value, str) or not regex.fullmatch(value):
        raise ValueError('invalid_' + field)
    return value


def _object(value, field):
    if not isinstance(value, dict):
        raise ValueError('invalid_' + field)
    return value


def _list(value, maximum, field):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError('invalid_' + field)
    return value


def sanitize_evidence(row):
    """Return present allowlisted fields; never copy arbitrary nested objects."""
    output = {}
    if 'challenges' in row:
        challenges = []
        for raw in _list(row['challenges'], 6, 'challenges'):
            item = _object(raw, 'challenge')
            finish = item.get('finish_reason')
            if finish not in FINISH_REASONS:
                raise ValueError('invalid_finish_reason')
            challenges.append({
                'round': _integer(item.get('round'), 1, 2, 'round'),
                'id': _safe(item.get('id'), SAFE_MODEL, 'challenge_id'),
                **{key: _integer(item.get(key), 0, 10000, key)
                   for key in ('expected_count', 'minimum_numbers')},
                'parsed_numbers': _integer(item.get('parsed_numbers'), 0, 65536, 'parsed_numbers'),
                'accepted': _boolean(item.get('accepted'), 'accepted'),
                'finish_reason': finish,
                'attempts': _integer(item.get('attempts'), 1, 3, 'attempts'),
                'errors': [_safe(error, SAFE_ERROR, 'error_kind')
                           for error in _list(item.get('errors'), 3, 'errors')],
            })
        output['challenges'] = challenges
    if 'rounds' in row:
        rounds = []
        seen = set()
        for raw in _list(row['rounds'], 2, 'rounds'):
            item = _object(raw, 'round')
            number = _integer(item.get('round'), 1, 2, 'round')
            if number in seen:
                raise ValueError('duplicate_round')
            seen.add(number)
            status = item.get('status')
            if status not in STATUSES:
                raise ValueError('invalid_round_status')
            clean = {'round': number, 'status': status,
                     'target_probability': _number(item.get('target_probability'), 0, 1, 'probability', nullable=True)}
            if item.get('prediction') is not None:
                clean['prediction'] = _safe(item['prediction'], SAFE_MODEL, 'prediction')
            if item.get('error_kind') is not None:
                clean['error_kind'] = _safe(item['error_kind'], SAFE_ERROR, 'error_kind')
            rounds.append(clean)
        output['rounds'] = sorted(rounds, key=lambda item: item['round'])
    if 'confirmation' in row:
        item = _object(row['confirmation'], 'confirmation')
        status = item.get('status')
        if status not in CONFIRMATION_STATUSES:
            raise ValueError('invalid_confirmation_status')
        output['confirmation'] = {
            'required': _boolean(item.get('required'), 'confirmation_required'),
            'confirmed': _boolean(item.get('confirmed'), 'confirmation_confirmed'),
            'status': status,
            'first_probability': _number(item.get('first_probability'), 0, 1, 'probability', nullable=True),
            'second_probability': _number(item.get('second_probability'), 0, 1, 'probability', nullable=True),
            'threshold_percent': _number(item.get('threshold_percent'), 50, 100, 'threshold_percent'),
        }
    return output


def classify_confirmation(evidence, threshold, fallback_status='unknown', error_kind=None):
    """Reclassify sanitized evidence under the transaction's current threshold.

    Returns (status, target_probability, error_kind, confirmation). Caller flags
    and confirmation probabilities are never sufficient to quarantine a target.
    """
    rounds = {item['round']: item for item in evidence.get('rounds', [])}

    def score(number):
        item = rounds.get(number, {})
        return item.get('target_probability') if item.get('status') in ('verified', 'suspect') else None

    first, second = score(1), score(2)
    first_low = first is not None and first * 100 < threshold
    second_low = second is not None and second * 100 < threshold
    confirmation = {'required': first_low or second_low, 'confirmed': False,
                    'status': 'inconclusive', 'first_probability': first,
                    'second_probability': second, 'threshold_percent': threshold}
    probability = second if second is not None else first
    if second is not None and not second_low:
        confirmation['status'] = 'recovered' if first_low else 'not_required'
        return 'verified', probability, None, confirmation
    if first_low and second_low:
        confirmation.update(confirmed=True, status='confirmed')
        return 'suspect', probability, None, confirmation
    if first is not None and not first_low and 2 not in rounds:
        confirmation.update(required=False, status='not_required')
        return 'verified', probability, None, confirmation
    if first_low or second_low:
        confirmation['status'] = 'pending' if 2 not in rounds else 'inconclusive'
        return 'unknown', probability, 'low_score_not_confirmed', confirmation
    status = 'error' if fallback_status == 'error' else 'unknown'
    return status, probability, error_kind, confirmation
