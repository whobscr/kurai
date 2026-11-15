"""pyproject.toml and uv.lock with Kurai's own version replaced by "0", for the image's library layer: that layer is
then rebuilt only when the libraries change, not on every release (its cache key is these files' contents)."""

import pathlib
import re

pyproject = pathlib.Path("pyproject.toml")
pyproject.write_text(re.sub(r'(?m)^version = "[^"]*"$', 'version = "0"', pyproject.read_text(), count=1))
lock = pathlib.Path("uv.lock")
lock.write_text(re.sub(r'(\[\[package\]\]\nname = "kurai"\nversion = )"[^"]*"', r'\1"0"', lock.read_text(), count=1))
