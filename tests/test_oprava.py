# -*- coding: utf-8 -*-
"""Oprava úseku: mapa bloků, dohledání hranic podle pauz a výměna vzorků."""
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audiobookery as ab  # noqa: E402

SR = 24000


def nahravka(delky_s, pauza_ms=250, vnitrni=()):
    """Bloky šumu oddělené digitálním tichem, jak je zapisuje převod.

    'vnitrni' jsou indexy bloků, které mají uprostřed 150ms pauzu po rozdělení.
    """
    rng = np.random.default_rng(1)
    kusy, useky, od = [], [], 0
    pauza = np.zeros(int(SR * pauza_ms / 1000), dtype="<i2")
    for i, delka in enumerate(delky_s):
        blok = (rng.uniform(-0.3, 0.3, int(SR * delka)) * 32767).astype("<i2")
        blok[np.abs(blok) <= 2] = 3
        if i in vnitrni:
            pul = len(blok) // 2
            blok = np.concatenate([blok[:pul], np.zeros(int(SR * 0.15), dtype="<i2"), blok[pul:]])
        useky.append((od, len(blok)))
        kusy += [blok, pauza]
        od += len(blok) + len(pauza)
    return np.concatenate(kusy), useky


class Hranice(unittest.TestCase):
    def test_dohledani_podle_pauz(self):
        pcm, useky = nahravka([3.0, 1.2, 4.5, 0.6], vnitrni=(2,))
        nalezene, pocet = ab.rekonstruuj_bloky(pcm, SR, 250, 4)
        self.assertEqual(pocet, 4)
        self.assertEqual(nalezene, useky)

    def test_nesedi_pocet(self):
        pcm, _ = nahravka([3.0, 1.2, 4.5])
        self.assertEqual(ab.rekonstruuj_bloky(pcm, SR, 250, 4), (None, 3))

    def test_bez_pauzy_nejde(self):
        pcm, _ = nahravka([3.0, 1.2], pauza_ms=0)
        self.assertIsNone(ab.rekonstruuj_bloky(pcm, SR, 0, 2)[0])


class Cas(unittest.TestCase):
    def test_formaty(self):
        self.assertEqual(ab.cas_na_sekundy("85"), 85)
        self.assertEqual(ab.cas_na_sekundy("1:25"), 85)
        self.assertEqual(ab.cas_na_sekundy(" 01:25,5 "), 85.5)
        self.assertEqual(ab.cas_na_sekundy("1:01:25"), 3685)

    def test_nesmysly(self):
        for text in ("", "a:b", "1:2:3:4", "-5"):
            with self.subTest(text=text):
                self.assertIsNone(ab.cas_na_sekundy(text))


class Mapa(unittest.TestCase):
    def setUp(self):
        self.slozka = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.slozka, True)

    def test_posledni_zapis_plati(self):
        mapa = ab.MapaBloku(self.slozka / ("kniha" + ab.MapaBloku.PRIPONA))
        mapa.hlavicka({"seed": 1}, SR, "kniha")
        mapa.pridej(1, "01 - 1", 0, 100, "první")
        mapa.pridej(2, "01 - 1", 350, 80, "druhý")
        mapa.pridej(3, "02 - 2", 0, 50, "třetí")
        # navázání po přerušení vygeneruje blok 2 znovu
        mapa.hlavicka({"seed": 2}, SR, "kniha")
        mapa.pridej(2, "01 - 1", 350, 90, "druhý znovu")
        hlavicka, soubory = mapa.nacti()
        self.assertEqual(hlavicka["parametry"], {"seed": 2})
        self.assertEqual([z["text"] for z in soubory["01 - 1"]], ["první", "druhý znovu"])
        self.assertEqual(len(soubory["02 - 2"]), 1)

        mapa.prepis(hlavicka, soubory)
        self.assertEqual(mapa.nacti(), (hlavicka, soubory))

    def test_najdi_mapu_podle_souboru(self):
        for nazev, soubor in (("a", "01 - 1"), ("b", "jiny")):
            mapa = ab.MapaBloku(self.slozka / (nazev + ab.MapaBloku.PRIPONA))
            mapa.pridej(1, soubor, 0, 10, "x")
        self.assertEqual(ab.najdi_mapu(self.slozka / "01 - 1.mp3").name, "a" + ab.MapaBloku.PRIPONA)
        self.assertIsNone(ab.najdi_mapu(self.slozka / "neni.mp3"))

    def test_jmeno_kapitoly(self):
        self.assertEqual(ab.nazev_souboru_kapitoly(0, "1"), "01 - 1")
        self.assertEqual(ab.nazev_souboru_kapitoly(11, ' Co: "to"? '), "12 - Co to")
        self.assertEqual(ab.nazev_souboru_kapitoly(2, ""), "03")


class Souvislost(unittest.TestCase):
    """Kapitola rozepsaná ve starší verzi má v mapě jen bloky od navázání."""

    def zaznamy(self, useky):
        return [{"blok": i + 1, "od": od, "delka": delka} for i, (od, delka) in enumerate(useky)]

    def test_cela_kapitola(self):
        _, useky = nahravka([3.0, 1.2, 4.5])
        self.assertTrue(ab.DialogOprava.souvisla(self.zaznamy(useky), SR, 250))

    def test_chybi_zacatek(self):
        _, useky = nahravka([3.0, 1.2, 4.5])
        self.assertFalse(ab.DialogOprava.souvisla(self.zaznamy(useky[1:]), SR, 250))

    def test_chybi_prostredek(self):
        _, useky = nahravka([3.0, 1.2, 4.5])
        self.assertFalse(ab.DialogOprava.souvisla(self.zaznamy([useky[0], useky[2]]), SR, 250))

    def test_prazdna(self):
        self.assertFalse(ab.DialogOprava.souvisla([], SR, 250))


class Vymena(unittest.TestCase):
    def test_posun_a_okoli_zustane(self):
        pcm, useky = nahravka([1.0, 2.0, 1.0])
        od, delka = useky[1]
        nove = np.full(SR, 0.5, dtype="float32")
        vysledek, posun = ab.vymen_usek(pcm, od, delka, nove)
        self.assertEqual(posun, SR - delka)
        self.assertTrue(np.array_equal(vysledek[:od], pcm[:od]))
        self.assertTrue(np.array_equal(vysledek[od + SR:], pcm[od + delka:]))
        self.assertEqual(int(vysledek[od]), int(0.5 * 32767))

    def test_uloz_wav_se_zalohou(self):
        slozka = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, slozka, True)
        cesta = slozka / "01 - 1.wav"
        puvodni, _ = nahravka([1.0])
        with wave.open(str(cesta), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(puvodni.tobytes())
        nove, _ = nahravka([2.0])
        with mock.patch.object(ab, "TEMP_DIR", slozka / "temp"):
            zaloha = ab.uloz_zvuk(cesta, nove, SR)
        with wave.open(str(cesta), "rb") as w:
            self.assertEqual(np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").tolist(), nove.tolist())
        with wave.open(str(zaloha), "rb") as w:
            self.assertEqual(w.getnframes(), len(puvodni))
        self.assertEqual(sorted(p.name for p in slozka.glob("*.wav")), ["01 - 1.wav"])


if __name__ == "__main__":
    unittest.main()
