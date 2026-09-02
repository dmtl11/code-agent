def divide(a: float, b: float) -> float:
    """Return a divided by b."""
    if b == 0:
        raise ZeroDivisionError("division by zero")
    return a / b
