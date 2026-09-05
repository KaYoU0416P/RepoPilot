"""A tiny calculator module. Intentionally contains one bug for RepoPilot to fix."""


def add(a: float, b: float) -> float:
    return a + b


def subtract(a: float, b: float) -> float:
    return a - b


def multiply(a: float, b: float) -> float:
    return a * b


def divide(a: float, b: float) -> float:
    return a / b


def average(values: list[float]) -> float:
    if not values:
        raise ValueError("empty sequence")
    return sum(values) / len(values)
