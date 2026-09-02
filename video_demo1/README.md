# Calculator Demo

This small project exposes a `divide(a, b)` helper and a unittest suite.

This is an intentionally broken fixture for the bug-fixing video demo.
The test expectation is the specification: `divide(8, 2)` must return `4`.
The initial implementation returns `16`, so the test must fail before the agent runs.
Ask the agent to repair the implementation without weakening the test.

The suite covers ordinary division, fractional results, negative operands,
and a zero divisor. Division by zero must raise `ZeroDivisionError`.

Run the tests with:

```powershell
python -m unittest -v
```
