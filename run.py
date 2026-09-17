"""Point d'entree de l'executable gele (PyInstaller).

Un module top-level plutot que `python -m xwipe` : PyInstaller demarre par un
script, pas par un package.
"""
from xwipe.app import main

if __name__ == "__main__":
    main()
