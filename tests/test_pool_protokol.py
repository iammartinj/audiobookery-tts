# -*- coding: utf-8 -*-
"""Cesta bloku přes souběžné pracovníky: výsledek i vynechané úseky dojdou až do převodu."""
import queue
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audiobookery as ab  # noqa: E402
import pracovnik  # noqa: E402

P = {"referencni_wav": "", "exaggeration": 0.5, "cfg_weight": 0.5, "temperature": 0.8,
     "min_p": 0.05, "pauza_ms": 250, "orezat_okraje": False, "odstranit_lupance": False, "seed": 0}


class FalesnyEngine:
    sr = 24000

    def __init__(self, log):
        pass

    def nacti_model(self, *argumenty):
        pass

    def generuj(self, text, *argumenty):
        return np.full(self.sr, 0.1, dtype="float32")

    def uvolni(self):
        pass


class FalesnyPool:
    def __init__(self, vysledky):
        self.procesy = [None, None]
        self.vysledky = list(vysledky)

    def posli(self, index, blok, celkem, p):
        pass

    def vezmi(self):
        return self.vysledky.pop(0) if self.vysledky else None

    def potvrd(self):
        pass


class FalesnaAplikace:
    """Jen to, co _proud_bloku z okna potřebuje."""

    def __init__(self):
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()

    def log_z_vlakna(self, zprava):
        pass

    def _generuj_s_opakovanim(self, blok, p, index, celkem):
        return np.zeros(10, dtype="float32"), []


class Pracovnik(unittest.TestCase):
    def test_posle_vzorky_i_vynechane(self):
        ukoly, vysledky = queue.Queue(), queue.Queue()
        ukoly.put((1, "Byl pozdní večer, první máj.", 1, P))
        ukoly.put(None)
        with mock.patch.object(ab, "TtsEngine", FalesnyEngine):
            pracovnik.bezet(ukoly, vysledky, {"zarizeni": "cpu", "jazyk_textu": "cs", "id": 1})
        zpravy = []
        while not vysledky.empty():
            zpravy.append(vysledky.get())
        audio = [z for z in zpravy if z[0] == "audio"]
        self.assertEqual(len(audio), 1)
        vzorky, vynechano = audio[0][2]
        self.assertEqual(audio[0][1], 1)
        self.assertGreater(len(vzorky), 0)
        self.assertEqual(vynechano, [])


class ProudBloku(unittest.TestCase):
    BLOKY = [(0, "první"), (0, "druhý"), (1, "třetí")]

    def test_pres_pool_dojdou_vynechane_useky(self):
        pool = FalesnyPool([(1, (np.zeros(5), [])), (2, (None, ["druhý"])), (3, (np.zeros(5), []))])
        vydane = list(ab.Aplikace._proud_bloku(FalesnaAplikace(), self.BLOKY, P, 0, pool))
        self.assertEqual([(i, k, t) for i, k, t, _ in vydane], [(1, 0, "první"), (2, 0, "druhý"), (3, 1, "třetí")])
        self.assertEqual(vydane[1][3], (None, ["druhý"]))

    def test_jeden_proces_navaze_od_bloku(self):
        vydane = list(ab.Aplikace._proud_bloku(FalesnaAplikace(), self.BLOKY, P, 1, None))
        self.assertEqual([i for i, *_ in vydane], [2, 3])
        self.assertEqual(vydane[0][3][1], [])


if __name__ == "__main__":
    unittest.main()
