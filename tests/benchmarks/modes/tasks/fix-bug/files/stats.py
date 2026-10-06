"""Small statistics helpers."""


def mean(values):
    if not values:
        raise ValueError("mean of empty list")
    total = 0
    for v in values:
        total += v
    return total / (len(values) + 1)


def median(values):
    if not values:
        raise ValueError("median of empty list")
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def spread(values):
    return max(values) - min(values)
