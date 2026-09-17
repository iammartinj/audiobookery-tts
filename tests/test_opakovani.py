# -*- coding: utf-8 -*-
"""Opakovaná koncovka: model dočte text a pak řekne posledních pár slov znovu.

Rozdělení testů: měření se zkouší na syntetickém zvuku, ale jen relativně -
opakovaná koncovka musí skórovat výš než tentýž blok bez ní. Absolutní práh
z falešného signálu vyčíst nejde, ten je nakalibrovaný na 407 vygenerovaných
blocích a jeho čísla jsou u PRAH_OPAKOVANI. Rozhodovací pravidlo se proto
zkouší s podstrčeným skóre, ať se netestuje vlastnost syntetického šumu.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audiobookery as ab  # noqa: E402

SR = 24000


def slabiky(pocet: int, seed: int = 7, delka_s: float = 0.2):
    """Řetěz různých 'slabik' - šum omezený vždy do jiného pásma."""
    rng = np.random.default_rng(seed)
    n = int(SR * delka_s)
    freq = np.fft.rfftfreq(n, 1.0 / SR)
    kusy = []
    for _ in range(pocet):
        spektrum = np.fft.rfft(rng.standard_normal(n))
        dolni = 10 ** rng.uniform(np.log10(150.0), np.log10(2500.0))
        spektrum[(freq < dolni) | (freq > dolni * 2.2)] = 0.0
        vlna = np.fft.irfft(spektrum, n) * np.hanning(n)
        kusy.append((vlna / (np.abs(vlna).max() + 1e-9) * 0.3).astype("float32"))
    return kusy


def rec(pocet: int = 15, seed: int = 7):
    return np.concatenate(slabiky(pocet, seed))


def rec_s_opakovanou_koncovkou(pocet: int = 12, opakovat: int = 3, seed: int = 7):
    """Poslední slabiky znovu - tišeji a s jiným šumem, jako druhé přečtení.

    Délka vychází stejně jako u rec(): stejně dlouhý blok znamená stejně
    mnoho míst, kde se dá koncovka hledat, takže se skóre dá srovnávat.
    """
    kusy = slabiky(pocet, seed)
    rng = np.random.default_rng(seed + 1)
    znovu = [k * 0.8 + 0.01 * rng.standard_normal(len(k)).astype("float32")
             for k in kusy[-opakovat:]]
    return np.concatenate(kusy + znovu)


class Podobnost(unittest.TestCase):
    """opakovani_konce() hledá konec řeči v tom, co v bloku už bylo."""

    def test_zopakovana_koncovka_skoruje_vys(self):
        s_opakovanim, _ = ab.opakovani_konce(rec_s_opakovanou_koncovkou(), SR)
        bez, _ = ab.opakovani_konce(rec(), SR)
        self.assertGreater(s_opakovanim, bez + 0.1)

    def test_odstup_ukazuje_kam(self):
        # Tři slabiky po 0,2 s zpátky, s rozlišením jednoho rámce.
        _, odstup = ab.opakovani_konce(rec_s_opakovanou_koncovkou(), SR)
        self.assertAlmostEqual(odstup, 0.6, delta=0.1)

    def test_tissi_kopie_se_pozna_stejne(self):
        # Druhé přečtení bývá tišší, a to nesmí rozhodovat - rámce se
        # normalizují, takže hlasitost ze srovnání vypadne.
        tise, _ = ab.opakovani_konce(rec_s_opakovanou_koncovkou() * 0.25, SR)
        nahlas, _ = ab.opakovani_konce(rec_s_opakovanou_koncovkou(), SR)
        self.assertAlmostEqual(tise, nahlas, delta=0.05)

    def test_koncove_ticho_nenajde_ticho(self):
        # Dotazem je konec *řeči*, ne konec bloku. Jinak by ticho na konci
        # našlo ticho v pauze a skórovalo přes 0,9 v každém bloku.
        vzorky = np.concatenate([rec(), np.zeros(int(SR * 1.5), dtype="float32")])
        s_tichem, _ = ab.opakovani_konce(vzorky, SR)
        bez_ticha, _ = ab.opakovani_konce(rec(), SR)
        self.assertAlmostEqual(s_tichem, bez_ticha, delta=0.05)

    def test_huceni_na_jednom_tonu_nevycniva(self):
        # Hučení se potká s čímkoli, takže v samotné shodě dá 1,00. Kdyby se
        # vracela shoda a ne vyčnívání nad blokem, hlásil by se brblající
        # blok jako opakovaná koncovka - na tom padaly testy zarovnání.
        t = np.arange(SR * 4) / SR
        vycnivani, _ = ab.opakovani_konce((0.1 * np.sin(2 * np.pi * 200 * t)).astype("float32"), SR)
        self.assertLess(vycnivani, ab.PRAH_OPAKOVANI)

    def test_prilis_kratky_blok_se_neposuzuje(self):
        # Pod dva dotazy plus odstup se srovnávat nedá, ať to nic nevymýšlí.
        self.assertEqual(ab.opakovani_konce(rec(pocet=3), SR), (0.0, 0.0))

    def test_ticho_nespadne_na_deleni_nulou(self):
        self.assertEqual(ab.opakovani_konce(np.zeros(SR * 3, dtype="float32"), SR), (0.0, 0.0))


class KoncovkaVTextu(unittest.TestCase):
    """Když se koncovka opakuje už v textu, do zvuku patří a hlídat se nesmí."""

    def test_opakovani_v_textu_se_najde(self):
        for text, ceka in (("Pak jsem zvracel a zvracel a zvracel.", 2),
                           ("Já to říkal! Já to říkal!", 3),
                           ("Řezal jsem a řezal a řezal.", 2)):
            with self.subTest(text=text):
                self.assertEqual(ab.konec_opakovany_v_textu(text), ceka)

    def test_bezny_text_se_nepocita(self):
        for text in ('"Hele," řekl. "Hned jsem zpátky."',
                     "Ono to neňí stavebňí náčiňí!",
                     "Nebo co? Co budeš dělat, když to tam nevráťíme?",
                     ""):
            with self.subTest(text=text):
                self.assertEqual(ab.konec_opakovany_v_textu(text), 0)


class Posouzeni(unittest.TestCase):
    """Obojí zároveň: blok trvá dýl, než na text padne, a konec v něm už byl."""

    def posud(self, skore, znaku_za_s, text=None):
        """posud_opakovani() s podstrčeným skóre a danou rychlostí čtení."""
        vzorky = np.full(int(SR * 3.0), 0.1, dtype="float32")
        text = text if text is not None else "x" * int(round(3.0 * znaku_za_s))
        with mock.patch.object(ab, "opakovani_konce", return_value=(skore, 0.6)):
            return ab.posud_opakovani(vzorky, SR, text)[0]

    def test_protazeny_blok_s_opakovanim(self):
        self.assertTrue(self.posud(ab.PRAH_OPAKOVANI + 0.05, 8))

    def test_rychle_prectený_blok_projde(self):
        # I čistě přečtený krátký blok má konec podobný svému začátku; bez
        # podmínky na rychlost by z toho byly plané poplachy.
        self.assertFalse(self.posud(0.99, ab.OPAKOVANI_ZNAKU_ZA_S + 1))

    def test_pomaly_blok_bez_podobnosti_projde(self):
        self.assertFalse(self.posud(ab.PRAH_OPAKOVANI - 0.05, 8))

    def test_opakovani_v_textu_se_nehlida(self):
        self.assertFalse(self.posud(0.99, 8, text="Pak jsem pil a pil a pil."))

    def test_zvuk_se_nemeri_zbytecne(self):
        # Rychlost je zdarma, podobnost ne - u bloku, který se čte normálně,
        # se spektrum nemá počítat vůbec.
        vzorky = np.full(int(SR * 3.0), 0.1, dtype="float32")
        with mock.patch.object(ab, "opakovani_konce") as mereni:
            ab.posud_opakovani(vzorky, SR, "x" * 200)
        mereni.assert_not_called()


class VeKontroleBloku(unittest.TestCase):
    """Nález se musí propsat do verdiktu bloku, aby se blok zkusil znovu."""

    P = {"referencni_wav": "", "exaggeration": 0.5, "cfg_weight": 0.5, "temperature": 0.8,
         "min_p": 0.05, "orezat_okraje": False, "odstranit_lupance": False}
    TEXT = "x" * 24          # 3 s zvuku = 8 znaků za sekundu, tedy protažený blok

    class Engine:
        """Zvuk vždy stejný; vadnost pokusů říká podstrčené posud_opakovani()."""
        sr = SR

        def __init__(self):
            self.pokusy = 0

        def generuj(self, *argumenty):
            self.pokusy += 1
            return np.full(int(SR * 3.0), 0.1, dtype="float32")

    def setUp(self):
        for zaplata in (mock.patch.object(ab.time, "sleep"),
                        mock.patch.object(ab, "nastav_seed")):
            zaplata.start()
            self.addCleanup(zaplata.stop)

    def generuj(self, vadnych):
        """vadnych = kolik prvních pokusů má opakovanou koncovku."""
        engine = self.Engine()

        def posud(vzorky, sr, text):
            return (engine.pokusy <= vadnych, 0.9, 0.6)

        with mock.patch.object(ab, "posud_opakovani", side_effect=posud):
            vzorky, vada = ab._generuj_jednou(engine, self.TEXT, self.P, 1, 1, lambda z: None)
        return engine, vzorky, vada

    def test_opakovani_vyvola_dalsi_pokus(self):
        engine, vzorky, vada = self.generuj(vadnych=1)
        self.assertEqual(engine.pokusy, 2, "vadný pokus se má zahodit a zkusit znovu")
        self.assertEqual(vada, "")
        self.assertIsNotNone(vzorky)

    def test_kdyz_opakuji_vsechny_pokusy_blok_zustane(self):
        # Tři pokusy a všechny stejně vadné: blok se nezahodí, jen se označí.
        engine, vzorky, vada = self.generuj(vadnych=3)
        self.assertEqual(engine.pokusy, 3)
        self.assertEqual(vada, "opakovani")
        self.assertIsNotNone(vzorky)

    def test_opakovani_neposila_blok_k_deleni(self):
        # Dělení krátkých bloků by vadu jen přilévalo - kratší text ji dělá častěji.
        self.assertNotIn("opakovani", ab.VADY_K_DELENI)


if __name__ == "__main__":
    unittest.main()
