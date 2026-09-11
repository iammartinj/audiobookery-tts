# -*- coding: utf-8 -*-
"""Stavový soubor rozdělané knihy."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audiobookery as ab  # noqa: E402

ZADANI = {"otisk_hlasu": "H", "otisk_verze_1": "", "opravy": {"slovnik": "S"},
          "zdroj": "kniha.epub", "ulozitelne": {"temperature": 0.8}}
PRESKOCENE = [{"blok": 7, "kapitola": 2, "text": "Nevyslovitelné."}]


class PostupNaDisku(unittest.TestCase):
    def setUp(self):
        self.slozka = tempfile.TemporaryDirectory()
        self.addCleanup(self.slozka.cleanup)
        self.cesta = Path(self.slozka.name) / "Kniha.progress.json"

    def test_ulozi_a_nacte(self):
        ab.Postup.nacti(self.cesta).uloz(ZADANI, 12, 40, ["01 - Úvod.mp3"], "02.wav", 48000, 1,
                                         preskocene=PRESKOCENE, nazev="Kniha")
        znovu = ab.Postup.nacti(self.cesta)
        self.assertEqual(znovu.data["verze"], 2)
        self.assertEqual(znovu.hotovo_bloku, 12)
        self.assertEqual(znovu.preskocene, PRESKOCENE)
        self.assertEqual(znovu.data["parametry"], {"temperature": 0.8})
        self.assertEqual(ab.posud_navazani(znovu.data, ZADANI), (True, []))

    def test_ulozeni_bez_seznamu_ho_nesmaze(self):
        postup = ab.Postup.nacti(self.cesta)
        postup.uloz(ZADANI, 5, 40, [], preskocene=PRESKOCENE)
        postup.uloz(ZADANI, 6, 40, [])
        self.assertEqual(ab.Postup.nacti(self.cesta).preskocene, PRESKOCENE)

    def test_najde_rozdelanou_knihu(self):
        ab.Postup.nacti(self.cesta).uloz(ZADANI, 12, 40, [], nazev="Kniha")
        nalezene = ab.najdi_rozdelane([self.slozka.name])
        self.assertEqual(len(nalezene), 1)
        self.assertEqual((nalezene[0]["nazev"], nalezene[0]["hotovo"]), ("Kniha", 12))


if __name__ == "__main__":
    unittest.main()
