#!/usr/bin/env python3
"""Run localdish from a clone: python3 localdish.py [--help]"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from localdish.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
