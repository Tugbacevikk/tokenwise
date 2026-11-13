import os, sys

sys.path.insert(0, os.path.dirname(__file__))
from _helpers import load_file, stub_missing

stub_missing(
    "paho",
    "paho.mqtt",
    "paho.mqtt.client",
    "matplotlib",
    "matplotlib.pyplot",
    "matplotlib.gridspec",
    "numpy",
    "pymongo",
)
m = load_file("subscriber.py")  # stubs make the MQTT/DB start-up at import a no-op
assert m.BITKI_PROFILLERI["domates"] == {"nem_min": 55, "nem_max": 75, "sicaklik_max": 30}
assert m.BITKI_PROFILLERI["marul"]["nem_min"] == 60
