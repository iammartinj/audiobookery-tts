# -*- coding: utf-8 -*-
"""T3 v MLX: hlídání vadných bloků nesmí zmizet a přesnost patří do otisku."""
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audiobookery as ab  # noqa: E402

try:
    import mlx_t3
except Exception:              # mlx je jen pro macOS na arm64
    mlx_t3 = None


class Platforma(unittest.TestCase):
    """MLX se smí nabízet jen tam, kde existuje."""

    def test_jen_apple_silicon(self):
        for platforma, stroj, ceka in (("darwin", "arm64", True),
                                       ("darwin", "x86_64", False),
                                       ("win32", "AMD64", False),
                                       ("linux", "x86_64", False)):
            with self.subTest(platforma=platforma, stroj=stroj):
                with mock.patch.object(ab.sys, "platform", platforma), \
                     mock.patch.object(ab.platform, "machine", return_value=stroj):
                    self.assertEqual(ab.mlx_mozny(), ceka)

    def test_volby_maji_prevod_na_bity(self):
        # Combobox nesmí nabídnout hodnotu, kterou _zapni_mlx neumí přeložit.
        for volba in ab.MLX_VOLBY:
            self.assertIn(volba, ab.MLX_PRESNOSTI)
        self.assertEqual(ab.MLX_PRESNOSTI["8-bit"], 8)
        self.assertEqual(ab.MLX_PRESNOSTI["4-bit"], 4)
        self.assertIsNone(ab.MLX_PRESNOSTI["float32"])


class FalesnyAnalyzator:
    """Tvar, ve kterém analyzátor drží stav po generování."""

    def __init__(self, completed_at, snimku):
        self.completed_at = completed_at
        self.alignment = np.zeros((snimku, 7), dtype="float32")


class HlidaniNaMlx(unittest.TestCase):
    """Kontrola vadných bloků čte stav zarovnání i přes MLX.

    Bez skořápky by stav_zarovnani() vracelo (None, 0) a posud_zarovnani()
    by každý blok prohlásilo za bezvadný - hlídání by potichu zmizelo.
    """

    def engine_s_mlx(self, analyzator):
        model_mlx = types.SimpleNamespace(last_analyzer=analyzator)
        t3 = ab._MlxT3(model_mlx, hp=types.SimpleNamespace())
        return types.SimpleNamespace(model=types.SimpleNamespace(t3=t3))

    def test_stav_se_precte_i_na_mlx(self):
        engine = self.engine_s_mlx(FalesnyAnalyzator(completed_at=40, snimku=55))
        self.assertEqual(ab.stav_zarovnani(engine), (40, 55))

    def test_nedoctene_se_pozna(self):
        # completed_at None = text nedošel do konce, blok něco postrádá.
        engine = self.engine_s_mlx(FalesnyAnalyzator(completed_at=None, snimku=55))
        dosel, snimku = ab.stav_zarovnani(engine)
        self.assertEqual((dosel, snimku), (None, 55))
        vada, _ponechat = ab.posud_zarovnani(dosel, snimku, 1000)
        self.assertEqual(vada, "nedocteno")

    def test_bez_generovani_je_analyzator_prazdny(self):
        engine = self.engine_s_mlx(None)
        self.assertEqual(ab.stav_zarovnani(engine), (None, 0))


class OtiskPresnosti(unittest.TestCase):
    """Přesnost T3 mění zvuk, takže se musí zapsat do stavu knihy."""

    def test_presnost_je_mezi_opravami(self):
        self.assertIn("mlx_presnost", ab.NASTAVENI_OPRAV)

    def test_zmena_presnosti_navazani_neblokuje(self):
        # Jako u rychlého dekodéru: změna se vypíše, ale kniha se dočte.
        zadani = {"otisk_hlasu": "abc", "otisk_verze_1": "x",
                  "opravy": {"mlx_presnost": "8-bit"}, "ulozitelne": {}}
        stav = {"verze": 2, "otisk_hlasu": "abc",
                "opravy": {"mlx_presnost": ab.MLX_VYPNUTO}}
        jde, zmeny = ab.posud_navazani(stav, zadani)
        self.assertTrue(jde)
        self.assertEqual(zmeny, ["mlx_presnost"])

    def test_kniha_z_doby_pred_mlx_se_docte_bez_hlaseni(self):
        # Starý stav klíč nezná, takže se nesmí tvářet jako změna.
        zadani = {"otisk_hlasu": "abc", "otisk_verze_1": "x",
                  "opravy": {"mlx_presnost": ab.MLX_VYPNUTO}, "ulozitelne": {}}
        stav = {"verze": 2, "otisk_hlasu": "abc", "opravy": {}}
        jde, zmeny = ab.posud_navazani(stav, zadani)
        self.assertTrue(jde)
        self.assertEqual(zmeny, [])


class UvolneniPameti(unittest.TestCase):
    """Uvolnění cache nesmí spadnout, ať je pod tím cokoli."""

    def test_bez_torche_jen_projde(self):
        with mock.patch.dict(sys.modules, {"torch": None}):
            ab.uvolni_pamet_zarizeni()          # nic nevyhodí

    def test_na_mps_pusti_mps_cache(self):
        mps = types.SimpleNamespace(empty_cache=mock.Mock())
        torch = types.SimpleNamespace(
            cuda=types.SimpleNamespace(is_available=lambda: False,
                                       empty_cache=mock.Mock()),
            backends=types.SimpleNamespace(
                mps=types.SimpleNamespace(is_available=lambda: True)),
            mps=mps)
        with mock.patch.dict(sys.modules, {"torch": torch}):
            ab.uvolni_pamet_zarizeni("mps")
        mps.empty_cache.assert_called_once()
        torch.cuda.empty_cache.assert_not_called()


@unittest.skipUnless(mlx_t3 is not None, "mlx není k dispozici (jen Apple Silicon)")
class PredcasnyKonecNaMlx(unittest.TestCase):
    """Dva stejné tokeny nesmí uříznout blok, dokud není text dočtený.

    Chatterbox 0.1.7 tuhle podmínku vyhodnocuje kdykoli a audiobookery si ji
    v torchové cestě opravuje (oprav_predcasny_konec). MLX má vlastní
    analyzátor, takže stejná oprava musí být i tady - jinak by se na Apple
    Siliconu vracela chyba, kterou torchová cesta už nemá.
    """

    S = 10          # počet textových tokenů

    def analyzator(self):
        return mlx_t3.AlignmentAnalyzer(text_tokens_slice=(0, self.S), eos_idx=99)

    def pozornost(self, spicka, prvni=False):
        """Attention s maximem na daném textovém tokenu."""
        radku = self.S + 1 if prvni else 1
        a = np.full((radku, self.S), 0.01, dtype="float32")
        a[:, spicka] = 0.9
        return a

    def krok(self, analyzator, spicka, token, prvni=False):
        return analyzator.step(self.pozornost(spicka, prvni), next_token=token)

    def docti_text(self, a):
        """Posune špičku pozornosti po textu až na konec.

        Po jednom tokenu za krok - skok dopředu by analyzátor vyhodnotil jako
        nespojitost a pozici by vůbec nepřevzal.
        """
        self.krok(a, 0, 1, prvni=True)
        for spicka in range(1, self.S):
            self.krok(a, spicka, spicka + 1)
        return a

    def test_stejne_tokeny_pred_doctenim_neukonci(self):
        # Jádro opravy: v pauze uprostřed textu se dva stejné tokeny nepočítají.
        a = self.analyzator()
        self.krok(a, 0, 7, prvni=True)
        for _ in range(5):
            _suppress, force = self.krok(a, 0, 7)
            self.assertFalse(a.complete, "text nemá být dočtený")
            self.assertFalse(force, "konec se nesmí vynutit před dočtením textu")

    def test_stejne_tokeny_po_docteni_ukonci(self):
        a = self.docti_text(self.analyzator())
        self.assertTrue(a.complete, "po projití textu má být dočteno")
        force = False
        for _ in range(4):
            _suppress, force = self.krok(a, self.S - 1, 7)
            if force:
                break
        self.assertTrue(force, "po dočtení se opakování hlídat má")

    def test_rozdilne_tokeny_po_docteni_neukonci(self):
        # Kontrola, že za vynuceným koncem výše stojí opakování tokenů,
        # ne dlouhý ocas nebo opakované zarovnání.
        a = self.docti_text(self.analyzator())
        for poradi in range(4):
            _suppress, force = self.krok(a, self.S - 1, 100 + poradi)
            self.assertFalse(force, "různé tokeny konec vynutit nemají")


if __name__ == "__main__":
    unittest.main()
