import math


def parse_timeout(value):
    result = int(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError('positive finite timeout required')
    return result
