from math import isfinite, sqrt

def normalize(vector: list[float]) -> list[float]:
    if not vector or not all(isfinite(value) for value in vector):
        raise ValueError("向量必须非空，且只能包含有限数值")

    length = sqrt(sum(value * value for value in vector))

    if length == 0 or not isfinite(length):
        raise ValueError("向量长度必须是有限的正数")

    return [value / length for value in vector]
