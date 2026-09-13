# -*- coding: utf-8 -*-
"""Model, který po dočtení nepřestal nebo text nedočetl: pozná se a zkusí znovu."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audiobookery as ab  # noqa: E402

SR = 24000
P = {"referencni_wav": "", "exaggeration": 0.5, "cfg_weight": 0.5, "temperature": 0.8,
     "min_p": 0.05, "pauza_ms": 250, "orezat_okraje": False, "odstranit_lupance": False}
NA_SNIMEK = SR // 25


class Posouzeni(unittest.TestCase):
    def test_bez_analyzatoru_nic(self):
        self.assertEqual(ab.posud_zarovnani(None, 0, 1000), ("", 1000))

    def test_nedocteno(self):
        self.assertEqual(ab.posud_zarovnani(None, 300, 1000)[0], "nedocteno")

    def test_bezny_dobeh_je_v_poradku(self):
        # 16 snímků po dočtení = 0,64 s, řeč dozněla 0,3 s po bodu dočtení
        pocet = 364 * NA_SNIMEK
        self.assertEqual(ab.posud_zarovnani(348, 364, pocet, (348 + 7) * NA_SNIMEK), ("", pocet))

    def test_dlouhy_presah_je_ocas(self):
        # Blok 8 z Ovidia: text došel ve 12 s, celkem 25,5 s
        vada, ponechat = ab.posud_zarovnani(300, 638, 638 * NA_SNIMEK)
        self.assertEqual(vada, "ocas")
        rezerva = int(ab.REZERVA_ZA_KONCEM_S * ab.SNIMKU_ZA_SEKUNDU)
        self.assertEqual(ponechat, (300 + rezerva) * NA_SNIMEK)

    def test_slysitelna_rec_za_koncem_je_ocas(self):
        # "Prosím, to siká": přesah jen 1,6 s, ale celý je slyšet
        pocet = 364 * NA_SNIMEK
        self.assertEqual(ab.posud_zarovnani(324, 364, pocet, pocet)[0], "ocas")

    def test_tichy_dobeh_neni_ocas(self):
        # Krátký blok: 1,48 s po dočtení, ale jen ticho
        pocet = 103 * NA_SNIMEK
        self.assertEqual(ab.posud_zarovnani(66, 103, pocet, 63 * NA_SNIMEK)[0], "")

    def test_rezerva_nepresahne_konec(self):
        snimku = 300 + int(ab.MAX_PRESAH_S * ab.SNIMKU_ZA_SEKUNDU) + 1
        vada, ponechat = ab.posud_zarovnani(300, snimku, snimku * NA_SNIMEK)
        self.assertEqual(vada, "ocas")
        self.assertLessEqual(ponechat, snimku * NA_SNIMEK)


def rec(delka_s, ticho_od=None, ticho_s=0.0):
    """Hlasitý signál s volitelnou tichou mezerou (brblání -50 dB)."""
    vzorky = np.full(int(SR * delka_s), 0.1, dtype="float32")
    if ticho_od is not None:
        vzorky[int(SR * ticho_od):int(SR * (ticho_od + ticho_s))] = 0.003
    return vzorky


class Rozbor(unittest.TestCase):
    def test_konec_a_mezera(self):
        vzorky = np.concatenate([rec(6.0, 1.0, 2.5), np.zeros(SR, dtype="float32")])
        konec, ticho = ab.rozbor_reci(vzorky, SR)
        self.assertAlmostEqual(konec / SR, 6.0, delta=0.05)
        self.assertAlmostEqual(ticho, 2.5, delta=0.06)

    def test_ticho_na_okrajich_se_nepocita(self):
        vzorky = np.concatenate([np.zeros(SR * 3, dtype="float32"), rec(2.0), np.zeros(SR * 3, dtype="float32")])
        self.assertEqual(ab.rozbor_reci(vzorky, SR)[1], 0.0)


class FalesnyEngine:
    """Vrací připravené výsledky (vzorky, dosel, snimku) jeden po druhém."""
    sr = SR

    def __init__(self, vysledky):
        self.vysledky = list(vysledky)
        self.model = None
        self.pokusu = 0

    def generuj(self, text, *argumenty):
        vzorky, dosel, snimku = self.vysledky[self.pokusu]
        if not isinstance(vzorky, np.ndarray):
            vzorky = rec(vzorky)
        self.pokusu += 1
        analyzator = SimpleNamespace(completed_at=dosel, alignment=np.zeros((snimku, 5)))
        self.model = SimpleNamespace(t3=SimpleNamespace(
            patched_model=SimpleNamespace(alignment_stream_analyzer=analyzator)))
        return vzorky


class GenerujJednou(unittest.TestCase):
    def setUp(self):
        for zaplata in (mock.patch.object(ab.time, "sleep"), mock.patch.object(ab, "nastav_seed")):
            zaplata.start()
            self.addCleanup(zaplata.stop)
        self.hlasky = []

    def generuj(self, engine, p=P):
        return ab._generuj_jednou(engine, "Byl pozdní večer, první máj.", p, 1, 1, self.hlasky.append)

    def test_cisty_pokus_se_vezme_hned(self):
        engine = FalesnyEngine([(4.0, 90, 100)])
        vzorky = self.generuj(engine)
        self.assertEqual(engine.pokusu, 1)
        self.assertEqual(len(vzorky), 4 * SR)

    def test_po_ocasu_se_zkusi_znovu(self):
        engine = FalesnyEngine([(25.5, 300, 638), (12.0, 290, 300)])
        vzorky = self.generuj(engine)
        self.assertEqual(engine.pokusu, 2)
        self.assertEqual(len(vzorky), 12 * SR)

    def test_tri_ocasy_vrati_useknuty(self):
        engine = FalesnyEngine([(25.5, 300, 638)] * 3)
        vzorky = self.generuj(engine)
        self.assertEqual(engine.pokusu, 3)
        self.assertLess(len(vzorky) / SR, 13.5)

    def test_brblani_uvnitr_se_zkusi_znovu(self):
        engine = FalesnyEngine([(rec(12.0, 3.0, 5.0), 290, 300), (7.0, 170, 175)])
        vzorky = self.generuj(engine)
        self.assertEqual(engine.pokusu, 2)
        self.assertEqual(len(vzorky), 7 * SR)

    def test_useknuty_ocas_ma_prednost_pred_nedoctenim(self):
        engine = FalesnyEngine([(10.0, None, 250), (25.5, 300, 638), (9.0, None, 225)])
        vzorky = self.generuj(engine)
        self.assertLess(len(vzorky) / SR, 13.5)
        self.assertGreater(len(vzorky) / SR, 12.0)

    def test_prepis_vybere_nejblizsi(self):
        engine = FalesnyEngine([(25.5, 300, 638), (10.0, None, 250), (9.0, None, 225)])
        prepisy = iter(["Byl pozdní večer, prv", "Byl pozdní večer, první máj.", "Byl"])
        with mock.patch.object(ab, "prepis_reci", lambda *a: next(prepisy)):
            vzorky = self.generuj(engine, {**P, "kontrola_asr": True})
        self.assertEqual(len(vzorky), 10 * SR)

    def test_bez_analyzatoru_hlida_delku(self):
        class BezAnalyzatoru:
            sr = SR
            pokusu = 0

            def generuj(self, text, *argumenty):
                self.pokusu += 1
                return rec(30.0 if self.pokusu == 1 else 2.0)

        engine = BezAnalyzatoru()
        vzorky = self.generuj(engine)
        self.assertEqual(engine.pokusu, 2)
        self.assertEqual(len(vzorky), 2 * SR)


class Shoda(unittest.TestCase):
    def test_interpunkce_a_velikost_nevadi(self):
        self.assertEqual(ab.shoda_textu("Byl pozdní večer, první máj.", "byl pozdní večer první máj"), 1.0)

    def test_chybejici_konec_snizi_shodu(self):
        cely = ab.shoda_textu("Tak vůbec nevypadala. Ani zdaleka.", "Tak vůbec nevypadala. Ani zdaleka.")
        bez_konce = ab.shoda_textu("Tak vůbec nevypadala. Ani zdaleka.", "Tak vůbec nevypadala.")
        self.assertLess(bez_konce, cely)


if __name__ == "__main__":
    unittest.main()
