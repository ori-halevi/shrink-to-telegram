# -*- coding: utf-8 -*-
"""Entry point: double click this file (or run.bat) to open the application.

Shrink To Telegram - smart video shrinking for Telegram uploads.
Copyright (C) 2026 Ori Halevi

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.  It is distributed WITHOUT ANY WARRANTY; without even the implied
warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
General Public License (the LICENSE file) for more details.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shrink2tg.gui import main            # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
