# -*- coding: utf-8 -*-
"""Blok, který nejde vygenerovat: rozdělí se a vynechá se jen to, co nevyjde ani tak."""
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audiobookery as ab  # noqa: E402

SR = 24000
P = {"referencni_wav": "", "exaggeration": 0.5, "cfg_weight": 0.5, "temperature": 0.8,
     "min_p": 0.05, "pauza_ms": 250, "orezat_okraje": False, "odstranit_lupance": False}


class FalesnyEngine:
    """Místo modelu: zvuk úměrný délce textu, u zakázaného nebo dlouhého textu chyba."""
    sr = SR

    def __init__(self, max_znaku=10 ** 9, zakazane=()):
        self.max_znaku = max_znaku
        self.zakazane = zakazane
        self.uspesne = []

    def generuj(self, text, *argumenty):
        if len(text) > self.max_znaku or any(z in text for z in self.zakazane):
            raise RuntimeError("simulovaná chyba modelu")
        self.uspesne.append(text)
        return np.full(int(SR * len(text) / 14.0), 0.1, dtype="float32")


class GenerujBlok(unittest.TestCase):
    def setUp(self):
        for zaplata in (mock.patch.object(ab.time, "sleep"), mock.patch.object(ab, "nastav_seed")):
            zaplata.start()
            self.addCleanup(zaplata.stop)

    def generuj(self, engine, text):
        return ab.generuj_blok(engine, text, P, 1, 1, lambda zprava: None)

    def test_bezny_blok(self):
        vzorky, vynechano = self.generuj(FalesnyEngine(), "Byl pozdní večer, první máj.")
        self.assertIsNotNone(vzorky)
        self.assertEqual(vynechano, [])

    def test_dlouhy_blok_se_rozdeli_a_nic_nechybi(self):
        text = "Byl pozdní večer, první máj. Večerní máj, byl lásky čas. Hrdliččin zval ku lásce hlas."
        engine = FalesnyEngine(max_znaku=40)
        vzorky, vynechano = self.generuj(engine, text)
        self.assertEqual(vynechano, [])
        self.assertIsNotNone(vzorky)
        # Části pokrývají celý text a jdou ve správném pořadí
        self.assertEqual(" ".join(engine.uspesne).split(), text.split())

    def test_vynecha_se_jen_cast_ktera_nevyjde(self):
        text = "Byl pozdní večer, první máj. Tady je XYZ nevyslovitelné. Hrdliččin zval ku lásce hlas."
        vzorky, vynechano = self.generuj(FalesnyEngine(zakazane=("XYZ",)), text)
        self.assertIsNotNone(vzorky)
        self.assertEqual(len(vynechano), 1)
        self.assertIn("XYZ", vynechano[0])
        self.assertNotIn("Hrdliččin", vynechano[0])

    def test_uplne_selhani_vrati_cely_text(self):
        text = "Krátká věta a nic víc."
        self.assertEqual(self.generuj(FalesnyEngine(zakazane=("a",)), text), (None, [text]))


if __name__ == "__main__":
    unittest.main()
