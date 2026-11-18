"""Lets `python -m tokenwise` work where the tokenwise command is not on PATH."""

from .cli import app

if __name__ == "__main__":
    app(prog_name="tokenwise")
