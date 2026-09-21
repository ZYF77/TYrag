"""Conservative quantity comparison; absence is unknown, never contradiction."""
import re
from decimal import Decimal

_UNITS = {
    'V':('voltage','1'), 'mV':('voltage','.001'), 'kV':('voltage','1000'),
    'A':('current','1'), 'mA':('current','.001'), 'W':('power','1'), 'kW':('power','1000'),
    'Pa':('pressure','1'), 'kPa':('pressure','1000'), 'MPa':('pressure','1000000'),
    'mm':('length','.001'), 'cm':('length','.01'), 'm':('length','1'),
    'ms':('time','.001'), 's':('time','1'), 'min':('time','60'),
    'Hz':('frequency','1'), 'kHz':('frequency','1000'), '℃':('temperature','1'),
    '°C':('temperature','1'), '%':('ratio','.01'), '％':('ratio','.01'),
}
_NUM = r'[+−-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?'
_PATTERN = re.compile(r'(?<![A-Za-z0-9_.])(?P<op><=|>=|≤|≥|<|>|≈)?\s*(?P<lo>'+_NUM+r')(?:\s*(?:至|–|~|～|-)\s*(?P<hi>'+_NUM+r'))?\s*(?P<unit>'+ '|'.join(sorted(map(re.escape, _UNITS), key=len, reverse=True))+r')(?![A-Za-z])')


def quantities(text):
    values = []
    for m in _PATTERN.finditer(text):
        dimension, factor = _UNITS[m['unit']]
        lo = Decimal(m['lo'].replace('−','-')) * Decimal(factor)
        hi = Decimal(m['hi'].replace('−','-')) * Decimal(factor) if m['hi'] else None
        op = {'≤':'<=','≥':'>='}.get(m['op'], m['op'] or '=')
        values.append((dimension, op, lo, hi))
    return values


def compare_fact(fact, answer, evidence):
    """Fields/context must literally exist in both spans before comparing values."""
    if not isinstance(fact, dict):
        return 'unknown'
    claim_span, evidence_span = fact.get('claim_span'), fact.get('evidence_span')
    if not isinstance(claim_span, str) or not claim_span or claim_span not in answer:
        return 'unknown'
    if not isinstance(evidence_span, str) or not evidence_span or evidence_span not in evidence:
        return 'unknown'
    for field in ('subject', 'attribute'):
        name = fact.get(field)
        if not isinstance(name, str) or not name or name not in claim_span or name not in evidence_span:
            return 'unknown'
    context = fact.get('context')
    if not isinstance(context, str) or (context and (context not in claim_span or context not in evidence_span)):
        return 'unknown'
    left, right = quantities(claim_span), quantities(evidence_span)
    if len(left) != 1 or len(right) != 1 or left[0][0] != right[0][0]:
        return 'unknown'
    if left == right:
        return 'supported'
    # A bound or range is not an exact value. Overlapping possibilities cannot
    # prove a conflict, and a broader source cannot prove a narrower claim.
    def interval(value):
        _, op, lo, hi = value
        if hi is not None:
            return (lo, True, hi, True) if op == '=' and lo <= hi else None
        if op == '=':
            return lo, True, lo, True
        if op in ('<', '<='):
            return Decimal('-Infinity'), False, lo, op == '<='
        if op in ('>', '>='):
            return lo, op == '>=', Decimal('Infinity'), False
        return None
    claim, source = interval(left[0]), interval(right[0])
    if claim is None or source is None:
        return 'unknown'
    def before(a, b):
        return a[2] < b[0] or (a[2] == b[0] and not (a[3] and b[1]))
    if before(claim, source) or before(source, claim):
        return 'contradicted'
    lower_inside = source[0] > claim[0] or (source[0] == claim[0] and (claim[1] or not source[1]))
    upper_inside = source[2] < claim[2] or (source[2] == claim[2] and (claim[3] or not source[3]))
    return 'supported' if lower_inside and upper_inside else 'unknown'


def has_unaccounted_numbers(answer, facts):
    # Citation/device identities are not measurements. Plain numbers without
    # a supported quantity remain unknown instead of being coerced to a unit.
    remainder = re.sub(r'\[ID:\d+\]', '', answer)
    for fact in facts:
        remainder = remainder.replace(fact.get('claim_span', ''), '')
    remainder = re.sub(r'(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*', '', remainder)
    return bool(re.search(r'(?<![A-Za-z0-9_])[-+]?\d+(?:\.\d+)?(?![A-Za-z0-9_])', remainder))
