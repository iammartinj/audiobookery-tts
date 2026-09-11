# -*- coding: utf-8 -*-
"""Slovníček výslovnosti: přejatá slova tvrdě, domácí měkce, délka beze změny."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audiobookery as ab  # noqa: E402


@unittest.skipUnless(ab.VYSLOVNOST_CS_PATH.exists(), "chybí vyslovnost_cs.json")
class CeskySlovnik(unittest.TestCase):
    def setUp(self):
        # Ruční pravidla patří uživateli - testy na nich nesmějí záviset
        bez_rucnich = mock.patch.multiple(ab, VZOR_VYSLOVNOSTI=None, MAPA_VYSLOVNOSTI={})
        bez_rucnich.start()
        self.addCleanup(bez_rucnich.stop)

    def prepis(self, text, jazyk="cs"):
        return ab.uprav_vyslovnost(text, jazyk)[0]

    def test_prejata_slova_zustanou_tvrda(self):
        for slovo in ("politika", "politice", "diplom", "titul", "technika", "nikotin",
                      "anestetický", "arcikomunista", "transhumanismus", "lokomotivám", "fetišista"):
            with self.subTest(slovo=slovo):
                self.assertEqual(self.prepis(slovo), slovo)

    def test_domaci_slova_dostanou_hacek(self):
        for slovo, cekano in (("tichý", "ťichý"), ("divadlo", "ďivadlo"), ("nic", "ňic"),
                              ("nikomu", "ňikomu"), ("tiše", "ťiše"), ("aniž", "aňiž")):
            with self.subTest(slovo=slovo):
                self.assertEqual(self.prepis(slovo), cekano)

    def test_velikost_pismen(self):
        self.assertEqual(self.prepis("Tichý"), "Ťichý")
        self.assertEqual(self.prepis("TICHÝ"), "ŤICHÝ")

    def test_jiny_jazyk_beze_zmeny(self):
        self.assertEqual(self.prepis("Ticho v divadle, nikdo nic.", "sk"), "Ticho v divadle, nikdo nic.")

    def test_hacek_nemeni_delku_slova(self):
        # Na tom stojí, že slovníček neposune hranice bloků ani otisk
        spatne = [k for k, v in ab.slovnik_cs().items() if len(k) != len(v)]
        self.assertEqual(spatne, [])

    def test_rucni_pravidlo_slovnik_prebije(self):
        vzor, mapa = ab._sestav_vzor({"tichý": "tychý"})
        with mock.patch.multiple(ab, VZOR_VYSLOVNOSTI=vzor, MAPA_VYSLOVNOSTI=mapa):
            self.assertEqual(ab.uprav_vyslovnost("tichý a nic", "cs")[0], "tychý a ňic")


if __name__ == "__main__":
    unittest.main()
