# -*- coding: utf-8 -*-
"""Otisk rozdělané knihy: co navázání blokuje a co ne."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audiobookery as ab  # noqa: E402

ZAKLAD = {"jazyk_textu": "cs|Thomcles/Chatterbox-TTS-Czech", "referencni_wav": "",
          "exaggeration": 0.5, "cfg_weight": 0.5, "temperature": 0.8, "min_p": 0.05,
          "seed": 0, "pauza_ms": 250, "format": "MP3", "bitrate": "128k",
          "odstranit_lupance": True, "orezat_okraje": True, "rychly_dekoder": False}
BLOKY = ["Byl pozdní večer, první máj.", "Večerní máj, byl lásky čas."]


class OtiskHlasu(unittest.TestCase):
    def setUp(self):
        self.slozka = tempfile.TemporaryDirectory()
        self.kniha = Path(self.slozka.name) / "kniha.txt"
        self.kniha.write_text("\n".join(BLOKY), encoding="utf-8")
        self.otisk = ab.otisk_hlasu(self.kniha, ZAKLAD, BLOKY)

    def tearDown(self):
        self.slozka.cleanup()

    def test_opravy_otisk_nemeni(self):
        for klic, hodnota in (("odstranit_lupance", False), ("orezat_okraje", False),
                              ("rychly_dekoder", True)):
            with self.subTest(klic=klic):
                self.assertEqual(ab.otisk_hlasu(self.kniha, {**ZAKLAD, klic: hodnota}, BLOKY), self.otisk)

    def test_nastaveni_hlasu_otisk_meni(self):
        for klic, hodnota in (("temperature", 0.7), ("seed", 42), ("exaggeration", 0.6),
                              ("cfg_weight", 0.3), ("min_p", 0.1), ("pauza_ms", 300),
                              ("format", "WAV"), ("bitrate", "192k"), ("jazyk_textu", "sk"),
                              ("referencni_wav", "jiny.wav")):
            with self.subTest(klic=klic):
                self.assertNotEqual(ab.otisk_hlasu(self.kniha, {**ZAKLAD, klic: hodnota}, BLOKY), self.otisk)

    def test_hacek_otisk_nemeni_delsi_slovo_ano(self):
        s_hackem = ["Byl pozdňí večer, prvňí máj.", BLOKY[1]]
        self.assertEqual(ab.otisk_hlasu(self.kniha, ZAKLAD, s_hackem), self.otisk)
        delsi = ["Byl pozdní večer, první májek.", BLOKY[1]]
        self.assertNotEqual(ab.otisk_hlasu(self.kniha, ZAKLAD, delsi), self.otisk)

    def test_zmena_knihy_otisk_meni(self):
        self.kniha.write_text("Úplně jiný text.", encoding="utf-8")
        self.assertNotEqual(ab.otisk_hlasu(self.kniha, ZAKLAD, BLOKY), self.otisk)

    def test_otisk_verze_1_nevidi_orez(self):
        # Verze 1.9 ořez do převodu neposílala, takže v jejím otisku chyběl
        bez = {k: v for k, v in ZAKLAD.items() if k != "orezat_okraje"}
        self.assertEqual(ab.otisk_verze_1(self.kniha, ZAKLAD, 2), ab.otisk_verze_1(self.kniha, bez, 2))


class PosudNavazani(unittest.TestCase):
    OPRAVY = {"odstranit_lupance": True, "orezat_okraje": True, "rychly_dekoder": False, "slovnik": "S"}

    def stav(self):
        return {"verze": 2, "otisk_hlasu": "H", "opravy": dict(self.OPRAVY),
                "parametry": {**ZAKLAD, "max_znaku": 200}, "hotovo_bloku": 10, "celkem_bloku": 50}

    def zadani(self, otisk="H", opravy=None, **parametry):
        return {"otisk_hlasu": otisk, "otisk_verze_1": "V1",
                "opravy": {**self.OPRAVY, **(opravy or {})},
                "ulozitelne": {**ZAKLAD, "max_znaku": 200, **parametry}}

    def test_beze_zmeny(self):
        self.assertEqual(ab.posud_navazani(self.stav(), self.zadani()), (True, []))

    def test_zmenena_oprava_navazani_neblokuje(self):
        jde, zmeny = ab.posud_navazani(self.stav(), self.zadani(opravy={"odstranit_lupance": False,
                                                                      "slovnik": "S2"}))
        self.assertTrue(jde)
        self.assertEqual(sorted(zmeny), ["odstranit_lupance", "slovnik"])

    def test_zmena_hlasu_blokuje_a_rekne_co(self):
        jde, zmeny = ab.posud_navazani(self.stav(), self.zadani(otisk="H2", temperature=0.7))
        self.assertFalse(jde)
        self.assertEqual(zmeny, ["temperature"])

    def test_neznamy_duvod(self):
        # Otisk nesedí, nastavení ano - změnil se text, bloky nebo soubor s hlasem
        self.assertEqual(ab.posud_navazani(self.stav(), self.zadani(otisk="H2")), (False, []))

    def test_prazdny_stav(self):
        self.assertEqual(ab.posud_navazani({}, self.zadani()), (False, []))

    def test_stav_z_verze_1(self):
        self.assertTrue(ab.posud_navazani({"verze": 1, "otisk": "V1"}, self.zadani())[0])
        self.assertFalse(ab.posud_navazani({"verze": 1, "otisk": "jiný"}, self.zadani())[0])


if __name__ == "__main__":
    unittest.main()
