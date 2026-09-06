"""
generator — build a static wiki from the Stonetop books.

Two phases, with a checked-in corpus between them:

  extract   PDF → marker lines            (``extract``, ``articles``; needs PyMuPDF)
  corpus    marker lines ↔ ``extracted/``  (``corpus``; plain text, committed)
  build     marker lines → HTML wiki      (``structure``, ``arcana``, ``chrome``,
                                           ``i18n``, ``sites``, ``build``)

``text`` is the layer both phases share: marker constants, inline-format
sentinels, and the line classifiers.
"""

from pathlib import Path

# The repository root: pages/, i18n/, extracted/ and Stonetop_Wiki/ live beside
# this package, not inside it.
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
