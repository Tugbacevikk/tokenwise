import os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from _helpers import use_src

use_src()
from sesli_asistan.gateway.core.schedule import OfficeHours

hours = OfficeHours.parse("pzt-cum 08:30-18:00")
assert hours.is_open(datetime(2026, 11, 16, 9, 0)) is True  # Monday morning
assert hours.is_open(datetime(2026, 11, 16, 19, 0)) is False  # Monday evening
assert OfficeHours().is_open(datetime(2026, 11, 15, 3, 0)) is True
