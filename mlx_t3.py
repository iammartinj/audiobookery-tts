"""MLX port of Chatterbox's T3 model (text tokens -> S3 speech tokens).

T3 is not a plain causal LM, which is why exporting `t3.tfmr` and loading it with
`mlx_lm.load` cannot work: the token embeddings, the *learned* position tables,
the speaker/emotion conditioning encoder and the output head all live outside the
Llama backbone. `tfmr.embed_tokens` (vocab_size=8) is a dummy the real model never
uses. This module reimplements every one of those pieces in MLX and keeps the
parameter names byte-identical to the torch checkpoint, so weights load straight
from `t3_cs.safetensors` with no conversion step.

The sampling loop mirrors `chatterbox.models.t3.t3.T3.inference` exactly, including
its quirks (the BOS speech token is embedded twice) so that output matches upstream.

Scope: only T3. The voice encoder and s3gen stay in torch on MPS.

Tested against mlx 0.32.2 / mlx-lm 0.31.3.
"""

from typing import Optional

import mlx.core as mx
import mlx.nn as nn
import numpy as np

from mlx_lm.models.base import create_causal_mask
from mlx_lm.models.cache import KVCache
from mlx_lm.models.rope_utils import initialize_rope

from chatterbox.models.t3.llama_configs import LLAMA_CONFIGS
from chatterbox.models.t3.modules.t3_config import T3Config

# (layer_idx, head_idx) pairs whose attention maps implicitly solve text-speech
# alignment. Copied from chatterbox's AlignmentStreamAnalyzer.
ALIGNED_HEADS = [(12, 15), (13, 11), (9, 2)]

NEG_INF = -float("inf")


# ---------------------------------------------------------------------------
# Llama backbone
# ---------------------------------------------------------------------------
class Attention(nn.Module):
    """Llama attention. Returns attention probabilities when spied on.

    `mx.fast.scaled_dot_product_attention` never materialises the probability
    matrix, so for the three heads the alignment analyzer needs we fall back to
    an explicit softmax. Three layers out of thirty costs little.
    """

    def __init__(self, cfg: dict):
        super().__init__()
        dim = cfg["hidden_size"]
        self.n_heads = cfg["num_attention_heads"]
        self.n_kv_heads = cfg.get("num_key_value_heads") or self.n_heads
        self.head_dim = cfg.get("head_dim") or dim // self.n_heads
        self.scale = self.head_dim**-0.5
        bias = cfg.get("attention_bias", False)

        self.q_proj = nn.Linear(dim, self.n_heads * self.head_dim, bias=bias)
        self.k_proj = nn.Linear(dim, self.n_kv_heads * self.head_dim, bias=bias)
        self.v_proj = nn.Linear(dim, self.n_kv_heads * self.head_dim, bias=bias)
        self.o_proj = nn.Linear(self.n_heads * self.head_dim, dim, bias=bias)

        self.rope = initialize_rope(
            self.head_dim,
            cfg["rope_theta"],
            cfg.get("rope_traditional", False),
            cfg.get("rope_scaling"),
            cfg["max_position_embeddings"],
        )

    def __call__(self, x, mask=None, cache=None, spy_head: Optional[int] = None):
        B, L, _ = x.shape
        q = self.q_proj(x).reshape(B, L, self.n_heads, -1).transpose(0, 2, 1, 3)
        k = self.k_proj(x).reshape(B, L, self.n_kv_heads, -1).transpose(0, 2, 1, 3)
        v = self.v_proj(x).reshape(B, L, self.n_kv_heads, -1).transpose(0, 2, 1, 3)

        offset = cache.offset if cache is not None else 0
        q = self.rope(q, offset=offset)
        k = self.rope(k, offset=offset)
        if cache is not None:
            k, v = cache.update_and_fetch(k, v)

        attn = None
        if spy_head is None:
            out = mx.fast.scaled_dot_product_attention(
                q, k, v, scale=self.scale, mask=mask
            )
        else:
            scores = (q * self.scale) @ k.transpose(0, 1, 3, 2)
            if mask is not None:
                scores = mx.where(mask, scores, NEG_INF)
            # Softmax in float32, but cast back before the matmul: upcasting the
            # cached values instead would copy the whole KV cache every step.
            probs = mx.softmax(scores.astype(mx.float32), axis=-1)
            out = probs.astype(v.dtype) @ v
            attn = probs[0, spy_head]  # (L, S), conditional branch only

        out = out.transpose(0, 2, 1, 3).reshape(B, L, -1)
        return self.o_proj(out), attn


class MLP(nn.Module):
    def __init__(self, cfg: dict):
        super().__init__()
        dim, hidden = cfg["hidden_size"], cfg["intermediate_size"]
        bias = cfg.get("mlp_bias", False)
        self.gate_proj = nn.Linear(dim, hidden, bias=bias)
        self.down_proj = nn.Linear(hidden, dim, bias=bias)
        self.up_proj = nn.Linear(dim, hidden, bias=bias)

    def __call__(self, x):
        return self.down_proj(nn.silu(self.gate_proj(x)) * self.up_proj(x))


class TransformerBlock(nn.Module):
    def __init__(self, cfg: dict):
        super().__init__()
        eps = cfg["rms_norm_eps"]
        self.self_attn = Attention(cfg)
        self.mlp = MLP(cfg)
        self.input_layernorm = nn.RMSNorm(cfg["hidden_size"], eps=eps)
        self.post_attention_layernorm = nn.RMSNorm(cfg["hidden_size"], eps=eps)

    def __call__(self, x, mask=None, cache=None, spy_head=None):
        h, attn = self.self_attn(self.input_layernorm(x), mask, cache, spy_head)
        x = x + h
        x = x + self.mlp(self.post_attention_layernorm(x))
        return x, attn


class LlamaBackbone(nn.Module):
    """Equivalent of `transformers.LlamaModel` as T3 uses it: embeddings bypassed,
    output taken after the final norm (HF's `hidden_states[-1]`)."""

    def __init__(self, cfg: dict):
        super().__init__()
        # Unused by T3, but present in the checkpoint.
        self.embed_tokens = nn.Embedding(cfg["vocab_size"], cfg["hidden_size"])
        self.layers = [
            TransformerBlock(cfg) for _ in range(cfg["num_hidden_layers"])
        ]
        self.norm = nn.RMSNorm(cfg["hidden_size"], eps=cfg["rms_norm_eps"])

    def __call__(self, h, cache, spies: Optional[dict] = None):
        L = h.shape[1]
        mask = create_causal_mask(L, cache[0].offset) if L > 1 else None
        attns = {}
        for i, (layer, c) in enumerate(zip(self.layers, cache)):
            spy = spies.get(i) if spies else None
            h, attn = layer(h, mask, c, spy)
            if attn is not None:
                attns[i] = attn
        return self.norm(h), attns


# ---------------------------------------------------------------------------
# Conditioning encoder (speaker embedding + perceiver-resampled speech prompt
# + emotion scalar)
# ---------------------------------------------------------------------------
class AttentionBlock2(nn.Module):
    """Cross/self attention block from chatterbox's perceiver.

    Note the shared LayerNorm applied to both inputs, and that upstream takes the
    `flash=True` path, i.e. ordinary attention over the sequence axis (the
    hand-written einsum branch next to it contracts the wrong axis and is dead
    code in the shipped model).
    """

    def __init__(self, channels: int, num_heads: int):
        super().__init__()
        self.n_heads = num_heads
        self.head_dim = channels // num_heads
        self.norm = nn.LayerNorm(channels)
        self.to_q = nn.Linear(channels, channels)
        self.to_k = nn.Linear(channels, channels)
        self.to_v = nn.Linear(channels, channels)
        self.proj_out = nn.Linear(channels, channels)

    def _split(self, x):
        B, L, _ = x.shape
        return x.reshape(B, L, self.n_heads, self.head_dim).transpose(0, 2, 1, 3)

    def __call__(self, x1, x2):
        n1, n2 = self.norm(x1), self.norm(x2)
        q, k, v = self._split(self.to_q(n1)), self._split(self.to_k(n2)), self._split(self.to_v(n2))
        h = mx.fast.scaled_dot_product_attention(
            q, k, v, scale=self.head_dim**-0.5, mask=None
        )
        B, _, L, _ = h.shape
        h = h.transpose(0, 2, 1, 3).reshape(B, L, -1)
        return x1 + self.proj_out(h)


class Perceiver(nn.Module):
    def __init__(self, n_query: int = 32, dim: int = 1024, num_heads: int = 4):
        super().__init__()
        self.pre_attention_query = mx.zeros((1, n_query, dim))
        self.attn = AttentionBlock2(dim, num_heads)

    def __call__(self, h):
        query = mx.broadcast_to(
            self.pre_attention_query, (h.shape[0],) + self.pre_attention_query.shape[1:]
        )
        pre_att = self.attn(query, h)
        return self.attn(pre_att, pre_att)


class T3CondEnc(nn.Module):
    def __init__(self, hp: T3Config):
        super().__init__()
        self.spkr_enc = nn.Linear(hp.speaker_embed_size, hp.n_channels)
        self.emotion_adv_fc = nn.Linear(1, hp.n_channels, bias=False)
        self.perceiver = Perceiver(dim=hp.n_channels)
        self._speaker_embed_size = hp.speaker_embed_size

    def __call__(self, speaker_emb, cond_prompt_speech_emb, emotion_adv):
        parts = [self.spkr_enc(speaker_emb.reshape(-1, self._speaker_embed_size))[:, None]]
        if cond_prompt_speech_emb is not None:
            parts.append(self.perceiver(cond_prompt_speech_emb))
        parts.append(self.emotion_adv_fc(emotion_adv.reshape(-1, 1, 1)))
        return mx.concatenate(parts, axis=1)


class LearnedPositionEmbeddings(nn.Module):
    def __init__(self, seq_len: int, dim: int):
        super().__init__()
        self.emb = nn.Embedding(seq_len, dim)

    def __call__(self, seq_len: int):
        return self.emb(mx.arange(seq_len))

    def at(self, idx: int):
        return self.emb(mx.array([[idx]]))


# ---------------------------------------------------------------------------
# Alignment analyzer (numpy; runs on a handful of floats per step)
# ---------------------------------------------------------------------------
def _reduce_or_zero(a, op, axis=None):
    """torch tolerates reductions over empty row slices here; numpy raises."""
    if a.size == 0:
        return np.float32(0.0)
    return getattr(a, op)(axis=axis)


class AlignmentAnalyzer:
    """Port of chatterbox's AlignmentStreamAnalyzer.

    Same heuristics, but it reports decisions instead of rewriting a logits
    tensor, and it holds no reference to the model -- upstream registers a
    forward hook per instance and never removes it, which is what makes repeated
    generations get slower and slower.
    """

    def __init__(self, text_tokens_slice, eos_idx: int):
        self.text_tokens_slice = (i, j) = text_tokens_slice
        self.eos_idx = eos_idx
        self.alignment = np.zeros((0, j - i), dtype=np.float32)
        self.curr_frame_pos = 0
        self.text_position = 0
        self.started = False
        self.started_at = None
        self.complete = False
        self.completed_at = None
        self.generated_tokens = []

    def step(self, aligned_attn: np.ndarray, next_token: Optional[int] = None):
        """Returns (suppress_eos, force_eos)."""
        i, j = self.text_tokens_slice
        if self.curr_frame_pos == 0:
            # First chunk holds conditioning, text tokens and BOS.
            A_chunk = aligned_attn[j:, i:j].copy()
        else:
            A_chunk = aligned_attn[:, i:j].copy()
        A_chunk[:, self.curr_frame_pos + 1:] = 0

        self.alignment = np.concatenate([self.alignment, A_chunk], axis=0)
        A = self.alignment
        T, S = A.shape

        cur_text_posn = int(A_chunk[-1].argmax())
        discontinuity = not (-4 < cur_text_posn - self.text_position < 7)
        if not discontinuity:
            self.text_position = cur_text_posn

        # Hallucinations at the start of speech show up as activations at the
        # bottom of the attention map.
        false_start = (not self.started) and (
            A[-2:, -2:].max() > 0.1 or A[:, :4].max() < 0.5
        )
        self.started = not false_start
        if self.started and self.started_at is None:
            self.started_at = T

        self.complete = self.complete or self.text_position >= S - 3
        if self.complete and self.completed_at is None:
            self.completed_at = T

        tail = A[self.completed_at:] if self.complete else A[:0]
        long_tail = self.complete and (
            _reduce_or_zero(tail[:, -3:].sum(axis=0), "max") >= 5
        )
        alignment_repetition = self.complete and (
            np.sum(_reduce_or_zero(tail[:, :-5], "max", axis=1)) > 5
        )

        # Only after the text has been read, like `long_tail` and
        # `alignment_repetition` above. Upstream chatterbox 0.1.7 checks this at
        # any point -- its "only once complete" condition is commented out -- so
        # two equal tokens in an ordinary pause force the end of speech and the
        # rest of the block is lost. Audiobookery patches that in the torch path
        # (oprav_predcasny_konec); this is the same fix for the MLX path.
        if next_token is not None and self.complete:
            self.generated_tokens.append(int(next_token))
            self.generated_tokens = self.generated_tokens[-8:]
        token_repetition = (
            self.complete
            and len(self.generated_tokens) >= 3
            and len(set(self.generated_tokens[-2:])) == 1
        )

        suppress_eos = cur_text_posn < S - 3 and S > 5
        force_eos = bool(long_tail or alignment_repetition or token_repetition)
        self.curr_frame_pos += 1
        return suppress_eos, force_eos


# ---------------------------------------------------------------------------
# Logits processors (match transformers' semantics)
# ---------------------------------------------------------------------------
def apply_repetition_penalty(logits, token_ids, penalty: float):
    if not token_ids or penalty == 1.0:
        return logits
    idx = mx.array(sorted(set(token_ids)))
    sel = logits[0, idx]
    logits[0, idx] = mx.where(sel > 0, sel / penalty, sel * penalty)
    return logits


def apply_min_p(logits, min_p: float):
    if min_p <= 0.0:
        return logits
    probs = mx.softmax(logits, axis=-1)
    threshold = min_p * probs.max(axis=-1, keepdims=True)
    return mx.where(probs < threshold, NEG_INF, logits)


def apply_top_p(logits, top_p: float):
    if top_p >= 1.0:
        return logits
    order = mx.argsort(logits, axis=-1)  # ascending, as in transformers
    sorted_logits = mx.take_along_axis(logits, order, axis=-1)
    cumulative = mx.cumsum(mx.softmax(sorted_logits, axis=-1), axis=-1)
    remove = cumulative <= (1.0 - top_p)
    remove[..., -1] = False  # min_tokens_to_keep=1
    mask = mx.zeros(logits.shape, dtype=mx.bool_)
    mask[0, order[0]] = remove[0]
    return mx.where(mask, NEG_INF, logits)


# ---------------------------------------------------------------------------
# T3
# ---------------------------------------------------------------------------
class T3MLX(nn.Module):
    def __init__(self, hp: Optional[T3Config] = None):
        super().__init__()
        hp = hp or T3Config.multilingual()
        cfg = dict(LLAMA_CONFIGS[hp.llama_config_name])
        dim = cfg["hidden_size"]

        self.tfmr = LlamaBackbone(cfg)
        self.cond_enc = T3CondEnc(hp)
        self.text_emb = nn.Embedding(hp.text_tokens_dict_size, dim)
        self.speech_emb = nn.Embedding(hp.speech_tokens_dict_size, dim)
        self.text_pos_emb = LearnedPositionEmbeddings(hp.max_text_tokens + 2, dim)
        self.speech_pos_emb = LearnedPositionEmbeddings(hp.max_speech_tokens + 4, dim)
        self.text_head = nn.Linear(dim, hp.text_tokens_dict_size, bias=False)
        self.speech_head = nn.Linear(dim, hp.speech_tokens_dict_size, bias=False)

        self._hp = hp
        self._cfg = cfg

    @property
    def hp(self):
        return self._hp

    # -- forward pieces ----------------------------------------------------
    def prepare_input_embeds(self, *, speaker_emb, cond_prompt_speech_tokens,
                             emotion_adv, text_tokens, cfg_weight: float):
        """Mirrors T3.prepare_input_embeds. `text_tokens` is (2, L) for CFG."""
        cond_prompt_speech_emb = None
        if cond_prompt_speech_tokens is not None:
            plen = cond_prompt_speech_tokens.shape[1]
            cond_prompt_speech_emb = (
                self.speech_emb(cond_prompt_speech_tokens)
                + self.speech_pos_emb(plen)
            )

        cond_emb = self.cond_enc(speaker_emb, cond_prompt_speech_emb, emotion_adv)

        text_emb = self.text_emb(text_tokens)
        if cfg_weight > 0.0:
            # CFG uncond branch: drop the text, keep the position embeddings.
            text_emb = mx.concatenate(
                [text_emb[0:1], mx.zeros_like(text_emb[1:2])], axis=0
            )
        text_emb = text_emb + self.text_pos_emb(text_tokens.shape[1])

        B = text_emb.shape[0]
        start = self._hp.start_speech_token
        initial_speech = mx.full((B, 1), start, dtype=mx.int32)
        speech_emb = self.speech_emb(initial_speech) + self.speech_pos_emb(1)

        if cond_emb.shape[0] != B:
            cond_emb = mx.broadcast_to(cond_emb, (B,) + cond_emb.shape[1:])

        embeds = mx.concatenate([cond_emb, text_emb, speech_emb], axis=1)
        return embeds, cond_emb.shape[1]

    def inference(self, *, speaker_emb, cond_prompt_speech_tokens, emotion_adv,
                  text_tokens, max_new_tokens: int = 1000, temperature: float = 0.8,
                  top_p: float = 1.0, min_p: float = 0.05,
                  repetition_penalty: float = 2.0, cfg_weight: float = 0.5,
                  align_analysis: bool = True, progress=None):
        """Generate S3 speech tokens. Returns a python list of ints (EOS included,
        as upstream does). All inputs are mx arrays; `text_tokens` is (2, L)."""
        hp = self._hp
        embeds, len_cond = self.prepare_input_embeds(
            speaker_emb=speaker_emb,
            cond_prompt_speech_tokens=cond_prompt_speech_tokens,
            emotion_adv=emotion_adv,
            text_tokens=text_tokens,
            cfg_weight=cfg_weight,
        )

        # Upstream embeds the BOS speech token a second time here; keeping the
        # duplicate is required to reproduce its output.
        bos = mx.array([[hp.start_speech_token]], dtype=mx.int32)
        bos_embed = self.speech_emb(bos) + self.speech_pos_emb.at(0)
        bos_embed = mx.concatenate([bos_embed, bos_embed], axis=0)
        h = mx.concatenate([embeds, bos_embed], axis=1)

        cache = [KVCache() for _ in self.tfmr.layers]
        spies = {layer: head for layer, head in ALIGNED_HEADS} if align_analysis else None
        analyzer = (
            AlignmentAnalyzer(
                text_tokens_slice=(len_cond, len_cond + text_tokens.shape[1]),
                eos_idx=hp.stop_speech_token,
            )
            if align_analysis
            else None
        )
        # Kept for the caller: audiobookery reads completed_at and alignment off
        # the last generation to tell a truncated block from a runaway one.
        self.last_analyzer = analyzer

        out, attns = self.tfmr(h, cache, spies)
        logits = self.speech_head(out[:, -1, :])

        generated = [hp.start_speech_token]
        predicted = []
        steps = range(max_new_tokens)
        if progress is not None:
            steps = progress(steps)

        for i in steps:
            cond, uncond = logits[0:1], logits[1:2]
            step_logits = cond + cfg_weight * (cond - uncond)

            if analyzer is not None:
                aligned = np.mean(
                    np.stack(
                        [np.array(attns[l], dtype=np.float32) for l, _ in ALIGNED_HEADS]
                    ),
                    axis=0,
                )
                suppress, force = analyzer.step(aligned, next_token=generated[-1])
                if force:
                    step_logits = mx.full(step_logits.shape, -(2**15), step_logits.dtype)
                    step_logits[..., hp.stop_speech_token] = 2**15
                elif suppress:
                    step_logits[..., hp.stop_speech_token] = -(2**15)

            step_logits = apply_repetition_penalty(
                step_logits, generated, float(repetition_penalty)
            )
            if temperature != 1.0:
                step_logits = step_logits / temperature
            step_logits = apply_min_p(step_logits, min_p)
            step_logits = apply_top_p(step_logits, top_p)

            next_token = int(mx.random.categorical(step_logits, axis=-1).item())
            predicted.append(next_token)
            generated.append(next_token)
            if next_token == hp.stop_speech_token:
                break

            token = mx.array([[next_token]], dtype=mx.int32)
            emb = self.speech_emb(token) + self.speech_pos_emb.at(i + 1)
            h = mx.concatenate([emb, emb], axis=0)
            out, attns = self.tfmr(h, cache, spies)
            logits = self.speech_head(out[:, -1, :])
            mx.eval(logits, *(c.state for c in cache))

        return predicted


def load_t3(ckpt_path, dtype=mx.float32, bits: Optional[int] = None,
            group_size: int = 64, hp: Optional[T3Config] = None) -> T3MLX:
    """Build T3MLX and load a chatterbox t3 safetensors checkpoint into it.

    `bits` quantises the backbone's linear layers only. The embedding tables,
    conditioning encoder and output head stay at full precision -- they are a
    small share of the weights and quantising the 8194-way speech head is where
    token quality degrades first.

    Use `dtype=mx.float32` with `bits=8`: that is the fastest and near-lossless
    combination. Unquantised float16/bfloat16 measures *slower* than float32 on
    M4 for these matmul shapes, so 16-bit floats are only worth it for memory.
    """
    model = T3MLX(hp)
    weights = mx.load(str(ckpt_path))
    float_types = (mx.float32, mx.float16, mx.bfloat16)
    weights = {
        k: (v.astype(dtype) if v.dtype in float_types else v)
        for k, v in weights.items()
    }
    model.load_weights(list(weights.items()))

    if bits is not None:
        backbone_linears = {
            f"tfmr.layers.{i}.{path}"
            for i in range(len(model.tfmr.layers))
            for path in (
                "self_attn.q_proj", "self_attn.k_proj", "self_attn.v_proj",
                "self_attn.o_proj", "mlp.gate_proj", "mlp.up_proj", "mlp.down_proj",
            )
        }
        nn.quantize(
            model,
            group_size=group_size,
            bits=bits,
            class_predicate=lambda path, _m: path in backbone_linears,
        )

    model.eval()
    mx.eval(model.parameters())
    return model
