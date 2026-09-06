#!/usr/bin/env python3
"""
Build a static wiki from the Stonetop books.

Usage:
  python stonetop-wiki-generator.py [--input <folder with the PDFs>] [--output <wiki folder>]
  python stonetop-wiki-generator.py --extract --input <folder>   # re-extract the text first

The work is done by the ``generator`` package beside this file: the
books' text is extracted from the 1-up PDFs into ``extracted/`` (one plain
text file per article, checked in), and the wiki is built from that — so a
checkout builds without the PDFs. ``python -m generator`` is the same
entry point.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generator.build import main  # noqa: E402

if __name__ == "__main__":
    main()
