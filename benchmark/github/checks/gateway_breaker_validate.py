import os, sys

sys.path.insert(0, os.path.dirname(__file__))
from _helpers import raises, use_src

use_src()
from sesli_asistan.gateway.core.resilience import CircuitBreaker

assert raises(ValueError, CircuitBreaker, "llm", failures=0)
assert raises(ValueError, CircuitBreaker, "llm", reset_s=0)
b = CircuitBreaker("llm", failures=1, reset_s=5, clock=lambda: 0.0)
b.failure()
assert b.state == "open"
