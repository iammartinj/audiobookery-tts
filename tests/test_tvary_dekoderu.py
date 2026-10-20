# -*- coding: utf-8 -*-
"""Dekodér na MPS: doplnění vstupu na násobek musí zklidnit paměť, ne délku zvuku.

MPS alokátor drží bloky podle tvaru vstupu, takže každý blok knihy (jiný počet
řečových tokenů) přidá desítky MB, které `empty_cache()` nevrátí. Obal dekodéru
doplní tokeny na násobek a zvuk uřízne - tyhle testy hlídají, že uříznutí sedí
na vzorek a že se nikdy nedoplňuje něco, co se pak přečte.
"""
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audiobookery as ab  # noqa: E402


class FalesnyDekoder:
    """Místo s3gen: zvuk dlouhý jako skutečný dekodér, 960 vzorků na token."""

    VZORKU_NA_TOKEN = 960

    def __init__(self):
        self.vstupy = []

    def inference(self, speech_tokens=None, ref_dict=None, **ostatni):
        self.vstupy.append(None if speech_tokens is None else speech_tokens.clone())
        n = 0 if speech_tokens is None else int(speech_tokens.shape[-1])
        return torch.zeros(1, n * self.VZORKU_NA_TOKEN), None


def obaleny(bucket=128):
    dekoder = FalesnyDekoder()
    return dekoder, ab.obal_dekoder_na_tvary(dekoder.inference, bucket)


class DelkaZvuku(unittest.TestCase):
    """Ať se doplňuje cokoli, ze vstupu se nesmí ztratit ani vzorek."""

    def test_delka_sedi_na_vzorek(self):
        dekoder, inference = obaleny()
        for n in (1, 5, 100, 127, 128, 129, 255, 256, 500, 1024):
            with self.subTest(tokenu=n):
                zvuk, _ = inference(
                    speech_tokens=torch.zeros(n, dtype=torch.long), ref_dict={})
                self.assertEqual(zvuk.shape[-1], n * FalesnyDekoder.VZORKU_NA_TOKEN)

    def test_nasobek_se_nedoplnuje(self):
        dekoder, inference = obaleny()
        tokeny = torch.arange(256, dtype=torch.long)
        inference(speech_tokens=tokeny, ref_dict={})
        self.assertTrue(torch.equal(dekoder.vstupy[-1], tokeny))   # beze změny

    def test_doplneni_je_ticho_na_konci(self):
        dekoder, inference = obaleny()
        tokeny = torch.arange(100, dtype=torch.long)    # 100 -> 128
        inference(speech_tokens=tokeny, ref_dict={})
        vstup = dekoder.vstupy[-1]
        self.assertEqual(vstup.shape, (128,))
        self.assertTrue(torch.equal(vstup[:100], tokeny))
        self.assertTrue(torch.all(vstup[100:] == ab.S3GEN_SIL))

    def test_bez_tokenu_se_nic_nemeni(self):
        dekoder, inference = obaleny()
        zvuk, _ = inference(ref_dict={})
        self.assertEqual(zvuk.shape[-1], 0 * FalesnyDekoder.VZORKU_NA_TOKEN)

    def test_delky_se_doplni_s_tokeny(self):
        # s3gen dnes speech_token_lens neposílá; kdyby začal, musí sedět na vstup.
        dekoder = FalesnyDekoder()
        zebrik = []

        def inference(speech_tokens=None, ref_dict=None, speech_token_lens=None, **kw):
            zebrik.append((int(speech_tokens.shape[-1]), None if speech_token_lens is None
                           else int(speech_token_lens[0])))
            return torch.zeros(1, int(speech_tokens.shape[-1]) * 960), None

        obal = ab.obal_dekoder_na_tvary(inference)
        obal(speech_tokens=torch.zeros(100, dtype=torch.long), ref_dict={},
             speech_token_lens=torch.tensor([100]))
        self.assertEqual(zebrik[-1], (128, 128))

class NasaditObal(unittest.TestCase):
    """`zpevni_tvary_dekoderu` se smí nasadit jen na MPS a jen jednou."""

    def model(self):
        return types.SimpleNamespace(s3gen=FalesnyDekoder())

    def test_na_mps_se_nasadi(self):
        model = self.model()
        with mock.patch.object(torch.backends, "mps",
                               types.SimpleNamespace(is_available=lambda: True)):
            self.assertTrue(ab.zpevni_tvary_dekoderu(model))
        self.assertTrue(getattr(model.s3gen, "audiobookery_puvodni_inference", None))

    def test_podruhe_už_ne(self):
        model = self.model()
        with mock.patch.object(torch.backends, "mps",
                               types.SimpleNamespace(is_available=lambda: True)):
            self.assertTrue(ab.zpevni_tvary_dekoderu(model))
            self.assertFalse(ab.zpevni_tvary_dekoderu(model))

    def test_bez_mps_nic(self):
        model = self.model()
        with mock.patch.object(torch.backends, "mps",
                               types.SimpleNamespace(is_available=lambda: False)):
            self.assertFalse(ab.zpevni_tvary_dekoderu(model))
        self.assertFalse(getattr(model.s3gen, "audiobookery_puvodni_inference", None))

    def test_nasadnuty_obal_doplňuje(self):
        model = self.model()
        with mock.patch.object(torch.backends, "mps",
                               types.SimpleNamespace(is_available=lambda: True)):
            ab.zpevni_tvary_dekoderu(model)
        zvuk, _ = model.s3gen.inference(speech_tokens=torch.zeros(100, dtype=torch.long),
                                        ref_dict={})
        self.assertEqual(zvuk.shape[-1], 100 * FalesnyDekoder.VZORKU_NA_TOKEN)
        self.assertEqual(model.s3gen.vstupy[-1].shape, (128,))


if __name__ == "__main__":
    unittest.main()
