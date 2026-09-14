# -*- coding: utf-8 -*-
"""Co o chatterboxu předpokládá cesta přes MLX.

Tenhle soubor nepotřebuje mlx ani Apple Silicon - běží všude, kde běží
chatterbox. To je jeho smysl: MLX cestu nejde vyzkoušet jinde než na Macu,
ale předpoklady, na kterých stojí, ověřit jde. Když se po zvednutí verze
chatterboxu něco z toho posune, spadne to tady a je vidět, co v mlx_t3.py
a v _MlxT3 překontrolovat - není potřeba k tomu Apple Silicon.

Postup, když některý z testů spadne, je v README v sekci o T3 na MLX.
"""
import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Zrcadlo mlx_t3.ALIGNED_HEADS. Schválně literálem, ne importem - mlx_t3 si
# tahá mlx, které je jen pro macOS na arm64, a tenhle test má běžet i bez něj.
ALIGNED_HEADS_V_MLX_T3 = [(12, 15), (13, 11), (9, 2)]

# Atributy, které si _ZarovnaniMlx v audiobookery.py vydává za analyzátor,
# a které z něj čte stav_zarovnani() a posud_zarovnani().
CTENE_ATRIBUTY = ("completed_at", "alignment", "complete")

# Pole T3Config, která mlx_t3.py čte při stavbě modelu.
POLE_T3CONFIG = ("llama_config_name", "start_speech_token", "stop_speech_token",
                 "speaker_embed_size", "n_channels", "text_tokens_dict_size",
                 "speech_tokens_dict_size", "max_text_tokens", "max_speech_tokens")

# Klíče konfigurace backbonu, na které se ptá Attention / MLP / LlamaBackbone.
KLICE_LLAMA = ("hidden_size", "num_attention_heads", "num_key_value_heads",
               "head_dim", "attention_bias", "rope_theta", "max_position_embeddings",
               "intermediate_size", "mlp_bias", "rms_norm_eps",
               "num_hidden_layers", "vocab_size")

try:
    from chatterbox.models.t3.t3 import T3
    from chatterbox.models.t3.llama_configs import LLAMA_CONFIGS
    from chatterbox.models.t3.modules.t3_config import T3Config
    from chatterbox.models.t3.inference.alignment_stream_analyzer import (
        AlignmentStreamAnalyzer, LLAMA_ALIGNED_HEADS)
except Exception:
    T3 = None


@unittest.skipIf(T3 is None, "chatterbox není nainstalovaný")
class PredpokladyMlxCesty(unittest.TestCase):

    def test_sledovane_hlavy_se_nezmenily(self):
        """mlx_t3 špehuje tři attention hlavy podle chatterboxu.

        Jiná čísla znamenají jinou mapu zarovnání, tedy nefunkční hlídání
        vadných bloků na MLX - a přitom by se nic nerozbilo viditelně.
        """
        self.assertEqual(list(LLAMA_ALIGNED_HEADS), ALIGNED_HEADS_V_MLX_T3,
                         "LLAMA_ALIGNED_HEADS se posunuly - srovnej "
                         "ALIGNED_HEADS v mlx_t3.py")

    def test_analyzator_drzi_cteny_stav(self):
        """_ZarovnaniMlx se vydává za analyzátor, tak musí mít co nabídnout.

        Analyzátor se nedá postavit bez transformeru - a stavět ho tu ani
        nechceme, protože si na něj hned zaregistruje hooky. Stačí se podívat,
        co si v __init__ nastavuje.
        """
        zdroj = inspect.getsource(AlignmentStreamAnalyzer.__init__)
        for atribut in CTENE_ATRIBUTY:
            with self.subTest(atribut=atribut):
                self.assertIn(f"self.{atribut} =", zdroj,
                              f"analyzátor už nedrží {atribut} - projdi "
                              f"_ZarovnaniMlx a AlignmentAnalyzer v mlx_t3.py")

    def test_step_bere_next_token(self):
        """oprav_predcasny_konec() obaluje step(logits, next_token=...).

        Jiná signatura znamená, že se oprava předčasného konce v torchové
        cestě tiše přestane uplatňovat.
        """
        parametry = list(inspect.signature(AlignmentStreamAnalyzer.step).parameters)
        self.assertEqual(parametry[:3], ["self", "logits", "next_token"])

    def test_t3_stavi_analyzator_pod_patched_model(self):
        """stav_zarovnani() čte t3.patched_model.alignment_stream_analyzer.

        _MlxT3 tu cestu napodobuje. Když ji chatterbox přestane stavět nebo
        přejmenuje, přestane hlídání fungovat na obou cestách.
        """
        zdroj = inspect.getsource(T3)
        for jmeno in ("patched_model", "alignment_stream_analyzer"):
            with self.subTest(jmeno=jmeno):
                self.assertIn(jmeno, zdroj,
                              f"T3 už nepracuje s {jmeno} - projdi "
                              f"stav_zarovnani() a _MlxT3.patched_model")

    def test_t3config_ma_pole_ktera_mlx_cte(self):
        hp = T3Config.multilingual()
        for pole in POLE_T3CONFIG:
            with self.subTest(pole=pole):
                self.assertTrue(hasattr(hp, pole),
                                f"T3Config už nemá {pole} - projdi mlx_t3.py")

    def test_konfigurace_backbonu_ma_klice_ktere_mlx_cte(self):
        hp = T3Config.multilingual()
        self.assertIn(hp.llama_config_name, LLAMA_CONFIGS)
        cfg = LLAMA_CONFIGS[hp.llama_config_name]
        for klic in KLICE_LLAMA:
            with self.subTest(klic=klic):
                self.assertIn(klic, cfg,
                              f"konfigurace Llamy už nemá {klic} - projdi "
                              f"Attention/MLP/LlamaBackbone v mlx_t3.py")


class ZrcadloJeVeShode(unittest.TestCase):
    """Na Macu navíc zkontrolovat, že literál výše opravdu sedí s mlx_t3."""

    def test_literal_sedi_s_mlx_t3(self):
        try:
            import mlx_t3
        except Exception as chyba:
            self.skipTest(f"mlx_t3 není k dispozici ({type(chyba).__name__})")
        self.assertEqual(list(mlx_t3.ALIGNED_HEADS), ALIGNED_HEADS_V_MLX_T3)


if __name__ == "__main__":
    unittest.main()
