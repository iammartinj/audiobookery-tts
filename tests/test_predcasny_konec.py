# -*- coding: utf-8 -*-
"""Opakování tokenu smí ukončit řeč až po dočtení textu."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audiobookery as ab  # noqa: E402


class FalesnyAnalyzator:
    def __init__(self):
        self.complete = False
        self.predane = []

    def step(self, logits, next_token=None):
        self.predane.append(next_token)
        return logits


class OpakovaniAzPoDocteni(unittest.TestCase):
    def setUp(self):
        self.puvodni = FalesnyAnalyzator.step
        FalesnyAnalyzator.step = ab._opakovani_az_po_doceteni(self.puvodni)
        self.addCleanup(setattr, FalesnyAnalyzator, "step", self.puvodni)

    def test_pred_doctenim_se_token_nepreda(self):
        a = FalesnyAnalyzator()
        self.assertEqual(a.step("logity", next_token=42), "logity")
        self.assertEqual(a.predane, [None])

    def test_po_docteni_se_preda(self):
        a = FalesnyAnalyzator()
        a.complete = True
        a.step("logity", next_token=42)
        self.assertEqual(a.predane, [42])

    def test_obal_je_poznat(self):
        self.assertTrue(FalesnyAnalyzator.step.audiobookery)
        self.assertEqual(FalesnyAnalyzator.step.__name__, "step")


if __name__ == "__main__":
    unittest.main()
