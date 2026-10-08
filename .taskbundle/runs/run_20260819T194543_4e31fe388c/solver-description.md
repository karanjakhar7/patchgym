# Problem statement

# Division by zero returns an invalid value

`tinycalc.divide()` currently returns `0` when the divisor is zero. Returning a
number hides an invalid operation from callers.

# Requirements

- Preserve normal division behavior.
- Raise `ValueError` with the message `divisor must not be zero` when the
  divisor is zero.
