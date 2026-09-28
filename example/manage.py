#!/usr/bin/env python
"""Entry point for the example project.

Run it from this directory::

    python manage.py migrate
    python manage.py seed
    python manage.py runserver
"""

import os
import sys
from pathlib import Path

# Make the checked-out drf_pollable package importable without installing it.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
