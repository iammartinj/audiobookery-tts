# -*- coding: utf-8 -*-
"""Každé nastavení, které mění zvuk, se musí do převodu opravdu poslat.

V 1.6 přibyl přepínač ořezu, ale do převodu se nikdy neposílal, takže nic
nedělal. Kontroluje se to ve zdrojovém kódu, protože postavit kvůli tomu
celé okno by test zdržovalo a sahalo na konfiguraci uživatele.
"""
import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audiobookery as ab  # noqa: E402

KLICE = ab.NASTAVENI_HLASU + ab.NASTAVENI_OPRAV


class NastaveniJdeDoPrevodu(unittest.TestCase):
    def test_posbirej_parametry(self):
        zdroj = inspect.getsource(ab.Aplikace._posbirej_parametry)
        for klic in KLICE:
            with self.subTest(klic=klic):
                self.assertIn(f'"{klic}"', zdroj)

    def test_obnova_po_preruseni(self):
        zdroj = inspect.getsource(ab.Aplikace._obnov_nastaveni)
        for klic in KLICE:
            with self.subTest(klic=klic):
                self.assertIn(f'"{klic}"', zdroj)

    def test_ulozeni_konfigurace(self):
        zdroj = inspect.getsource(ab.Aplikace._uloz_config)
        for klic in ab.NASTAVENI_OPRAV:
            with self.subTest(klic=klic):
                self.assertIn(f'"{klic}"', zdroj)


if __name__ == "__main__":
    unittest.main()
