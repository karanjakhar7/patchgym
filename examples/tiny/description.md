# Division by zero returns an invalid value

`tinycalc.divide()` currently returns `0` when the divisor is zero. Returning a
number hides an invalid operation from callers.

