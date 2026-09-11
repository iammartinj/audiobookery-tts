# -*- coding: utf-8 -*-
"""Rozdělení na bloky: slovníček výslovnosti nesmí posunout hranice."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audiobookery as ab  # noqa: E402

TEXT = ("Ticho v divadle bylo tak hluboké, že nikdo nechtěl nic říct. " * 12 + "\n\n" +
        "Chodili jsme spolu po nábřeží, a ona mi na nic neodpověděla, ani když jsem se ptal. " * 10)


@unittest.skipUnless(ab.VYSLOVNOST_CS_PATH.exists(), "chybí vyslovnost_cs.json")
class HraniceBloku(unittest.TestCase):
    def test_slovnicek_neposune_hranice(self):
        with mock.patch.multiple(ab, VZOR_VYSLOVNOSTI=None, MAPA_VYSLOVNOSTI={}):
            upraveny = ab.uprav_vyslovnost(TEXT, "cs")[0]
        self.assertNotEqual(upraveny, TEXT)
        for max_znaku in (80, 200, 400):
            with self.subTest(max_znaku=max_znaku):
                puvodni = [len(b) for b in ab.rozdel_na_bloky(TEXT, max_znaku)]
                s_hacky = [len(b) for b in ab.rozdel_na_bloky(upraveny, max_znaku)]
                self.assertEqual(puvodni, s_hacky)

    def test_bloky_nepresahnou_limit(self):
        for max_znaku in (80, 200):
            with self.subTest(max_znaku=max_znaku):
                self.assertTrue(all(len(b) <= max_znaku for b in ab.rozdel_na_bloky(TEXT, max_znaku)))


if __name__ == "__main__":
    unittest.main()
