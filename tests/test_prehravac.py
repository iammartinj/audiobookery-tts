# -*- coding: utf-8 -*-
"""Přehrávač knihy: čtení rozepsaného WAV, vlna, časy a přechody mezi kapitolami."""
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audiobookery as ab  # noqa: E402

SR = 24000


def wav(cesta: Path, vzorky, uzavrit=True):
    z = ab.WavZapisovacRaw(cesta, SR)
    z.zapis(vzorky)
    if uzavrit:
        z.zavri()
    else:
        z.soubor.flush()
    return z


class Pomocne(unittest.TestCase):
    def setUp(self):
        self.slozka = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.slozka, True)

    def test_rozepsany_wav_se_precte(self):
        vzorky = np.linspace(-0.5, 0.5, SR, dtype="float32")
        z = wav(self.slozka / "a.wav", vzorky, uzavrit=False)
        self.addCleanup(z.zavri)
        cast = ab.precti_pcm(self.slozka / "a.wav", 100, 200)
        self.assertEqual(len(cast), 100)
        self.assertEqual(int(cast[0]), int(vzorky[100] * 32767))
        self.assertEqual(len(ab.precti_pcm(self.slozka / "a.wav")), SR)
        self.assertEqual(len(ab.precti_pcm(self.slozka / "a.wav", SR - 10, SR * 5)), 10)

    def test_delka_wav(self):
        wav(self.slozka / "b.wav", np.zeros(SR * 2, dtype="float32"))
        self.assertEqual(ab.delka_souboru_vzorku(self.slozka / "b.wav", SR), SR * 2)

    def test_vlna_pres_osu_delsi_nez_zvuk(self):
        obalka = ab.rms_oken((np.ones(SR, dtype="float32") * 16000).astype("<i2"), 2400)
        vysky, dostupne = ab.sloupce_vlny(obalka, 2400, SR, SR * 2, 10)
        self.assertEqual(dostupne, [True] * 5 + [False] * 5)
        self.assertTrue(all(v > 0.9 for v in vysky[:5]))
        self.assertEqual(vysky[5:], [0.0] * 5)

    def test_ticho_je_nizke(self):
        obalka = ab.rms_oken(np.zeros(SR, dtype="<i2"), 2400)
        vysky, _ = ab.sloupce_vlny(obalka, 2400, SR, SR, 4)
        self.assertEqual(vysky, [0.0] * 4)

    def test_casy(self):
        self.assertEqual(ab.lidsky_cas(45), "45 s")
        self.assertEqual(ab.lidsky_cas(19 * 60 + 20), "19 min")
        self.assertEqual(ab.lidsky_cas(9 * 3600 + 45 * 60), "9 h 45 min")
        self.assertEqual(ab.lidsky_cas(-1), "—")
        self.assertEqual(ab.kratky_cas(24 * 60 + 49), "24:49")
        self.assertEqual(ab.kratky_cas(3723), "1:02:03")


class FalesnyFont:
    def measure(self, text):
        return len(text) * 10


class Zkraceni(unittest.TestCase):
    def test_vejde_se(self):
        self.assertEqual(ab.zkrat_text("kniha", FalesnyFont(), 100), "kniha")

    def test_zkrati_na_sirku(self):
        vysledek = ab.zkrat_text("D:/AI/audiobooky/vystup", FalesnyFont(), 100)
        self.assertTrue(vysledek.endswith("…"))
        self.assertLessEqual(FalesnyFont().measure(vysledek), 100)


class Kapitoly(unittest.TestCase):
    """Přehrávač bez zvukové karty: falešný sounddevice jen počítá zapsané vzorky."""

    def setUp(self):
        self.slozka = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.slozka, True)
        self.zapsano = []
        zapsano = self.zapsano

        class Proud:
            def __init__(self, samplerate, channels, dtype):
                pass

            def start(self):
                pass

            def write(self, data):
                zapsano.append(len(data))

            def stop(self):
                pass

            def close(self):
                pass

            def abort(self):
                pass

        self._puvodni = sys.modules.get("sounddevice")
        sys.modules["sounddevice"] = types.SimpleNamespace(OutputStream=Proud)
        self.addCleanup(self._obnov_sd)
        self.p = ab.Prehravac(SR, lambda z: None)
        self.addCleanup(self.p.zastav)

    def _obnov_sd(self):
        if self._puvodni is None:
            sys.modules.pop("sounddevice", None)
        else:
            sys.modules["sounddevice"] = self._puvodni

    def cekej(self, podminka, strop=5.0):
        start = time.time()
        while time.time() - start < strop:
            if podminka():
                return True
            time.sleep(0.02)
        return False

    def test_rozepsana_kapitola_se_dočítá_a_pak_navaze_dalsi(self):
        prvni = np.full(SR // 2, 0.2, dtype="float32")
        z = wav(self.slozka / "01.wav", prvni, uzavrit=False)
        self.p.nastav_kapitoly([1.0, 1.0])
        self.p.aktualizuj(0, cesta=str(z.cesta), od=0, do=z.pocet_vzorku, hotova=False, roste=True)
        self.p.vyber(0, 0.0, hrat=True)
        self.assertTrue(self.cekej(lambda: self.p.stav()["dostupno"] == SR // 2))

        # dojede na živý konec a počká na další blok
        self.assertTrue(self.cekej(lambda: self.p.stav()["pozice_s"] >= 0.5))
        self.assertTrue(self.p.stav()["hraje"])
        z.zapis(prvni)
        z.soubor.flush()
        self.p.aktualizuj(0, do=z.pocet_vzorku)
        self.assertTrue(self.cekej(lambda: self.p.stav()["dostupno"] == SR))
        z.zavri()

        druha = wav(self.slozka / "02.wav", np.full(SR // 4, 0.1, dtype="float32"))
        self.p.aktualizuj(0, hotova=True, roste=False)
        self.p.aktualizuj(1, cesta=str(druha.cesta), od=0, do=druha.pocet_vzorku, hotova=True)
        self.assertTrue(self.cekej(lambda: self.p.stav()["kapitola"] == 1))
        self.assertTrue(self.cekej(lambda: not self.p.stav()["hraje"]))

    def test_prejmenovani_na_mp3_zvuk_v_pameti_nezahodi(self):
        z = wav(self.slozka / "01.wav", np.full(SR, 0.2, dtype="float32"))
        self.p.nastav_kapitoly([1.0])
        self.p.aktualizuj(0, cesta=str(z.cesta), od=0, do=SR, hotova=True)
        self.p.vyber(0, 0.5, hrat=False)
        self.assertTrue(self.cekej(lambda: self.p.stav()["dostupno"] == SR))
        mp3 = self.slozka / "01.mp3"
        z.cesta.rename(mp3)             # soubor MP3 se nečte, zvuk už je v paměti
        self.p.prejmenuj(str(self.slozka / "01.wav"), str(mp3))
        time.sleep(0.3)
        st = self.p.stav()
        self.assertEqual((st["dostupno"], st["cesta"], round(st["pozice_s"], 1)), (SR, str(mp3), 0.5))

    def test_skok_jen_do_vygenerovaneho(self):
        z = wav(self.slozka / "01.wav", np.full(SR, 0.2, dtype="float32"))
        self.p.nastav_kapitoly([4.0])
        self.p.aktualizuj(0, cesta=str(z.cesta), od=0, do=SR, hotova=False, roste=True)
        self.p.vyber(0, 0.0, hrat=False)
        self.assertTrue(self.cekej(lambda: self.p.stav()["dostupno"] == SR))
        self.assertEqual(self.p.stav()["osa"], SR * 4)
        self.p.skoc(0.9)                 # 3,6 s z odhadnutých 4 s - vygenerovaná je 1 s
        self.assertEqual(self.p.stav()["pozice_s"], 1.0)


if __name__ == "__main__":
    unittest.main()
