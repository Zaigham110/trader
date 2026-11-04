# Local LLMs for a multi-agent gold-trading system — research notes

Date: 2026-11-04. Scope: non-time-critical agents (market analysis, news interpretation, strategy-research proposals, post-trade review, free-form chat). Execution is not done by the LLM.

Labels: **Verified** = read on the primary page on 2026-11-04 (link given). **Assumption** = my working estimate or derived arithmetic. **Unknown** = could not verify. **Community** = Reddit/blog measurement, lower confidence.

Headline: the open-weight landscape moved a lot in 2026 (Qwen3.5→3.8, Gemma 4, GLM-5.x, DeepSeek V4.x, Kimi K3, Mistral Small 4 / Large 4). Llama 4 (Apr 2025) is now the oldest family in the list and Meta has shipped nothing newer under the Llama name (**Verified**: https://huggingface.co/meta-llama shows nothing newer than Llama-4 Maverick/Scout, updated May 2025; accessed 2026-11-04).

---

## 1. Candidate open-weight models (late 2026)

### 1a. Small / fast (classification, extraction, JSON)

| Model | Licence | Params | Context | Thinking | Tools / JSON | Q4 size, VRAM | Notes |
|---|---|---|---|---|---|---|---|
| **Qwen3.5-9B** | Apache-2.0 **Verified** | 9B dense | 262,144 native (YaRN to ~1M) **Verified** | On by default; `enable_thinking: False` per request **Verified** | "excels in tool calling", `qwen3_coder` parser; no documented JSON mode (use server-side schema) **Verified** | Q4_K_M 5.68 GB **Verified** (unsloth GGUF) → ~8 GB VRAM with 16k ctx **Assumption** | Released Feb 2026. Best small all-rounder for our tier (a). |
| **Gemma 4 12B** | Apache-2.0 **Verified** | 11.95B dense | 256K **Verified** | `<|think|>` token toggles **Verified** | "native support for structured tool use" **Verified** | ~7–8 GB at Q4 **Assumption** (not read on card) | MMLU-Pro 77.2, GPQA 78.8 (vendor). Text+image+audio in. |
| **Gemma 4 E4B** | Apache-2.0 **Verified** | 4.5B effective / 8B incl. embeddings | 128K **Verified** | yes | native function calling **Verified** | ~3–5 GB **Assumption** | Edge-class; good as a draft model or for trivial routing. |
| **gpt-oss-20b** | Apache-2.0 **Verified** | 21B total / 3.6B active MoE | 128K (NVIDIA/Oracle docs; not on the HF card) **Verified**-secondary | reasoning low/med/high via system prompt **Verified** | function calling + "structured output" listed **Verified** | MXFP4 "runs within 16 GB" **Verified** | Fastest decode of this tier (190 t/s on a 4090, see §3). Aug 2025 vintage. |
| Nemotron 3.5 Lightning 30B-A3B | OpenMDW-1.1, "ready for commercial use" **Verified** | 30B / 3B active (Mamba-2 + MoE) | up to 1M **Verified** | `enable_thinking` **Verified** | tool calling (`qwen3_coder` parser) **Verified** | NVFP4 build recommended for production **Verified** | Released 2026-08-11. Behaves like a small model in speed. |

Sources: https://huggingface.co/Qwen/Qwen3.5-9B ; https://huggingface.co/unsloth/Qwen3.5-9B-GGUF ; https://huggingface.co/google/gemma-4-12b-it ; https://huggingface.co/google/gemma-4-E4B-it ; https://huggingface.co/openai/gpt-oss-20b ; https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16 (all accessed 2026-11-04).

### 1b. Mid (27–35B) — analysis / reasoning

| Model | Licence | Params | Context | Thinking | Tools / JSON | Q4 size | Notes |
|---|---|---|---|---|---|---|---|
| **Qwen3.8-27B** (Aug 2026) | Apache-2.0 **Verified** | 27B dense, hybrid Gated-DeltaNet (48 layers) + gated attention (16 layers) **Verified** | 262,144 native, YaRN to 1M **Verified** | On by default; `reasoning_effort` xhigh/medium/low; `preserve_thinking` **Verified** | tool calling not documented on this card (sibling Qwen3.6-27B documents `qwen3_coder` parser) **Verified**; no JSON mode | UD-Q4_K_M 16.5 GB, Q8_0 29 GB, BF16 54.7 GB **Verified** (unsloth); NVFP4 variants exist (community) | Vendor: GPQA-Diamond 89.2, LiveCodeBench 90.3, SWE-bench Pro 61.7. Needs recent llama.cpp (new arch `qwen35`). Linear-attention layers → cheap long context. |
| Qwen3.6-27B (Apr 2026) | Apache-2.0 **Verified** | 27B dense | 262,144 **Verified** | on by default **Verified** | "excels in tool calling", `qwen3_coder` parser **Verified** | ~16–17 GB **Assumption** | Previous generation; keep as fallback if 3.8 has engine bugs. |
| **Gemma 4 31B** (Apr 2026) | Apache-2.0 **Verified** | 30.7B dense | 256K **Verified** | `<|think|>` toggle **Verified** | structured tool use **Verified** | Q4_K_M 18.3 GB, Q8_0 32.6 GB **Verified** (unsloth) | Vendor: MMLU-Pro 85.2, GPQA 84.3, Tau2 76.9. Multimodal (charts as images possible). |
| Gemma 4 26B-A4B | Apache-2.0 **Verified** | 25.2B / 3.8B active **Verified** | 256K | yes | yes | ~17 GB **Community** | Slightly weaker than 31B (MMLU-Pro 82.6) but 3× faster decode. |
| Qwen3.6-35B-A3B | Apache-2.0 **Verified** | 35B / 3B active | 262,144 **Verified** | yes | yes | ~20 GB **Assumption** | Very fast; good for high-volume news triage when 9B is not enough. |

Sources: https://huggingface.co/Qwen/Qwen3.8-27B ; https://huggingface.co/unsloth/Qwen3.8-27B-GGUF ; https://huggingface.co/Qwen/Qwen3.6-27B ; https://huggingface.co/Qwen/Qwen3.6-35B-A3B ; https://huggingface.co/google/gemma-4-31b-it ; https://huggingface.co/unsloth/gemma-4-31B-it-GGUF ; https://huggingface.co/google/gemma-4-26b-a4b-it (accessed 2026-11-04).

### 1c. Large (70B+ / MoE) — strategy research, chat

| Model | Licence (commercial?) | Params | Context | Reasoning | Tools/JSON | Weights size / fits where | Notes |
|---|---|---|---|---|---|---|---|
| **gpt-oss-120b** | Apache-2.0 **Verified** | 117B / 5.1B active | 128K (secondary) | low/med/high **Verified** | function calling + structured output **Verified** | MXFP4 GGUF 63.4 GB **Verified**; "fits a single 80 GB GPU" **Verified** → 96 GB card, or 128 GB unified memory | Fastest large model per $ (210 t/s on RTX PRO 6000, §3). Aug 2025 vintage; weaker than 2026 Chinese MoEs on vendor benchmarks. |
| **Qwen3.5-122B-A10B** | Apache-2.0 **Verified** | 122B / 10B active | 262,144 **Verified** | on by default **Verified** | tool calling (`qwen3_coder`) **Verified**; official GPTQ-Int4 and FP8 repos in collection **Verified** | Q4 ≈ 65–70 GB **Assumption** → 96 GB card or 128 GB unified | Vendor: MMLU-Pro 86.7, TAU2 79.5, IFEval 93.4. Measured 98 t/s on RTX PRO 6000 (§3). My pick for tier (c) on a 96 GB card. |
| Mistral Small 4 119B-A6B (Mar 2026) | Apache-2.0 **Verified** | 119B / 6.5B active | 256K **Verified** | `reasoning_effort` none/high **Verified** | native function calling + JSON output **Verified** | FP8 ≈ 121 GB (community); card's vLLM example uses TP=2 **Verified** | Multimodal. Good Apache alternative; fewer GGUF reports. |
| Qwen3.8-Flash-Next 125B-A6B (+51B n-gram, 180B on disk) | **qwen-community-1.0** — NOT Apache. Mirrored text: separate licence needed if you run a Model-as-a-Service / "AI work assistant" business; internal use exempt **Verified**(mirror, lower confidence) | 125B / 6B active | 262,144 **Verified** | on by default **Verified** | tool-use benchmarks; parser not documented | Q4 ≈ 100 GB **Assumption** | Strongest "fits in 128 GB" option (GPQA 91.7 vendor). Licence is fine for an in-house trading system (internal use), but read the LICENSE file. |
| GLM-5.3-Flash | MIT **Verified** | 320B / 18B active **Verified** | ≥300K used in evals **Verified** | `reasoning_effort` low/high/max **Verified** | tool use in benchmarks only | Q4 ≈ 170–180 GB **Assumption** → 2×96 GB or 256 GB Mac | "One-tenth the price of GLM-5.2" per Z.ai; API $0.15/$0.50 at Together. |
| Qwen3.5-397B-A17B | Apache-2.0 **Verified** | 397B / 17B active | 262,144 | yes | yes; official GPTQ-Int4 **Verified** | Q4 ≈ 220 GB **Assumption** → 256 GB Mac Studio or 4× DGX Spark | Runs on 2× Strix Halo via RPC at 11.8 t/s (community). |
| GLM-5.2 | MIT **Verified** | 753B total **Verified** | 1M **Verified** | multi-level thinking **Verified** | agentic benchmarks | ~400 GB at 4-bit → 512 GB Mac only | Vendor: GPQA 91.2. M3 Ultra 4-bit: 17.7 t/s (community). |
| DeepSeek-V4.1-Flash-1017 | MIT **Verified** | 763B total (sources cite 552B backbone + engram) **Verified** | 1M **Verified** | thinking + effort **Verified** | tool calling **Verified** | ≥400 GB at 4-bit **Assumption** | API only ($0.15–0.30/M in). |
| Kimi K3 | "Kimi K3 License": MIT-like; MaaS >$20M/12-mo revenue needs an agreement; >100M MAU or >$20M monthly revenue must display "Kimi K3"; **internal use exempt** **Verified** | 2.8T / 104B active **Verified** | 1M **Verified** | always on; low/high/max **Verified** | yes **Verified** | 1.56 TB — not local | API only. |
| Mistral Large 4 1T-A52B (Oct 29 2026) | "Mistral Ecosystem Open-Weight License" = modified MIT; MaaS needs separate licence **Verified** | ~1T / 52B active **Verified** | 1M **Verified** | low/med/high **Verified** | function calling, JSON output, **strict JSON schema** **Verified** | vLLM TP=8 **Verified** | API only. |
| Llama 4 Scout/Maverick; MiniMax M3 | Llama 4 Community License (700M MAU cap) **Verified**-secondary; "minimax-community" (terms **Unknown**) | 17B / 23B active | — | — | — | — | Llama 4 (Apr 2025) is superseded; MiniMax M3 (428B) is API-only. Skip both. |

Sources: https://huggingface.co/openai/gpt-oss-120b ; https://huggingface.co/ggml-org/gpt-oss-120b-GGUF ; https://huggingface.co/Qwen/Qwen3.5-122B-A10B ; https://huggingface.co/collections/Qwen/qwen35 ; https://huggingface.co/mistralai/Mistral-Small-4-119B-2603 ; https://huggingface.co/Qwen/Qwen3.8-Flash-Next ; https://huggingface.co/zai-org/GLM-5.3-Flash ; https://huggingface.co/zai-org/GLM-5.2 ; https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017 ; https://huggingface.co/moonshotai/Kimi-K3 and its LICENSE ; https://huggingface.co/mistralai/Mistral-Large-4-1T-A52B ; https://huggingface.co/MiniMaxAI/MiniMax-M3 (accessed 2026-11-04).

### Finance-relevant benchmark evidence (sceptical view)

- No vendor card above reports a finance benchmark. Finance-specific evidence exists only for finance-tuned models: **Fin-R1** (7B on Qwen2.5-7B-Instruct, Apache-2.0, ~60k CoT samples distilled from DeepSeek-R1; vendor: FinQA 76.0, ConvFinQA 85.0, avg 75.2 vs DeepSeek-R1 78.2) **Verified** https://huggingface.co/SUFE-AIFLM-Lab/Fin-R1 ; **Agentar-Fin-R1** 8B/32B (Ant Group, Qwen3 base, paper claims avg 81.28 on their own Finova suite) — weights licence **Unknown** (https://arxiv.org/abs/2507.16802). FinGPT is a 2023 LoRA-on-Llama-2 project **Verified** https://arxiv.org/abs/2310.04793.
- **Assessment (Assumption):** not worth it. Their bases are 2024–25 models (a stock Qwen3.8-27B almost certainly beats Fin-R1-7B on reasoning); their benchmarks are CFA/FinQA-style numeric QA, not "headline → impact on XAUUSD"; fine-tunes often lose instruction-following and tool-calling. Build our own eval set (§7) and, if needed, a small LoRA on our labelled headlines later. A FinBERT-style encoder remains a cheap sentiment baseline to compare against.

---

## 2. Serving tools

| Tool | OpenAI-compatible API | JSON-schema-enforced decoding | Tool calling | Concurrency / batching | Quant formats | Multi-GPU | Windows | Docker |
|---|---|---|---|---|---|---|---|---|
| **llama.cpp `llama-server`** | Yes (chat/completions, responses, embeddings) + **Anthropic Messages API** **Verified** | **Yes**: `response_format: json_schema` → GBNF grammar; also raw `grammar`. Caveats on card: nested `$ref` broken, `prefixItems` broken, `properties`+`anyOf` fails, `patternProperties` unsupported, `minimum/maximum` only for integers; unsupported keywords are **silently skipped** **Verified** | "for ~any model", Jinja templates default; `--skip-chat-parsing` **Verified** | `-np` slots, continuous batching default **Verified** | GGUF (Q2–Q8, MXFP4, NVFP4 on Blackwell builds) | `--split-mode layer/row/tensor`, `--tensor-split` **Verified** | `llama-server.exe` **Verified** | `server-cuda` image **Verified** |
| **Ollama** | Yes (`response_format` on `/v1`) **Verified** | **Yes** `format: <JSON schema>` **Verified**. Mechanism: GBNF via llama.cpp for llama.cpp-backed models (community); for Ollama's own engine the grammar implementation is **Unknown** | Yes | `OLLAMA_NUM_PARALLEL` default **1**; `OLLAMA_MAX_LOADED_MODELS` default 3×GPUs; queue 512 **Verified** | GGUF | Spreads a model across GPUs only if it doesn't fit one **Verified** | Native app **Verified** | Yes (Linux / WSL2 GPU) **Verified** |
| **vLLM** | Yes | **Yes**: `structured_outputs` {json, regex, grammar(EBNF), choice, structural_tag}; `response_format json_schema`; backends xgrammar / guidance (auto), outlines; lm-format-enforcer deprecated **Verified** | Yes, per-model parsers (`--enable-auto-tool-choice --tool-call-parser qwen3_coder|mistral`); for Qwen3 with reasoning, `--structured-outputs-config.enable_in_reasoning=True` needed **Verified** | Continuous batching, paged attention (best throughput) | AWQ, GPTQ, FP8, INT8, GGUF, bitsandbytes, NVFP4 (Blackwell), MXFP4 **Verified** | TP / PP | **No native Windows** (WSL) **Verified** | `vllm/vllm-openai` **Verified**; ROCm image incl. Ryzen AI Max (ROCm 7.0.2+) **Verified** |
| **SGLang** | Yes | **Yes**: json_schema / regex / EBNF; xgrammar default, outlines, llguidance; **structural tags** to constrain tool-call bodies **Verified** | Yes (parsers) | Continuous batching, RadixAttention prefix cache | FP8, INT4 etc. | TP | Linux docs only **Verified** | `lmsysorg/sglang` (CUDA 13 images) **Verified** |
| **LM Studio (llmster daemon)** | Yes | **Yes** `response_format.json_schema`; grammar sampling via llama.cpp (GGUF) / Outlines (MLX); warns <7B models unreliable **Verified** | Yes | **Unknown** | GGUF, MLX | Not documented **Unknown** | Yes **Verified** | No (daemon install script) |
| **TGI** | Yes (chat/completions with tools) | Yes: grammar as logits processor, JSON schema / regex **Verified** | Yes (tools = grammars) **Verified** | Continuous batching | GPTQ/AWQ/FP8 | TP | No | Yes | Maintenance status **Unknown**; I would not start a new deployment on it. |
| **TabbyAPI (ExLlamaV3)** | Yes, OAI tool calling **Verified** | Yes: JSON schema, regex, EBNF **Verified** | Yes | Continuous batching + paged attention (Ampere+) **Verified** | EXL3, FP16 | TP via shared memory (Docker `--shm-size`) **Verified** | `start.bat` exists; not documented | CUDA 12.8/13, ROCm images **Verified** | README: "hobby project, not intended for production" **Verified** |
| **MLX (`mlx_lm.server`)** | "similar to" OpenAI chat API **Verified** | **No structured-output support documented** **Verified**; use LM Studio's MLX path (Outlines) or Outlines directly | Not documented | Single request at a time with quantised KV **Verified** | MLX 4/8-bit | n/a (Apple) | n/a | n/a | "Not recommended for production" **Verified** |

Sources: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md ; https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md ; https://docs.ollama.com/capabilities/structured-outputs ; https://docs.ollama.com/faq ; https://docs.vllm.ai/en/latest/features/structured_outputs.html ; https://docs.vllm.ai/en/latest/features/quantization/ ; https://docs.vllm.ai/en/latest/getting_started/installation/gpu.html ; https://docs.sglang.io/advanced_features/structured_outputs.html ; https://docs.sglang.io/get_started/install.html ; https://lmstudio.ai/docs/app/api/structured-output ; https://huggingface.co/docs/text-generation-inference/en/conceptual/guidance ; https://github.com/theroyallab/tabbyAPI ; https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md (accessed 2026-11-04).

Critical schema-enforcement facts for us:
- Every serious server (llama.cpp, Ollama, vLLM, SGLang, LM Studio, TGI, TabbyAPI) can enforce a JSON schema **at decode time** (token masking). Only raw mlx_lm lacks it.
- Grammar constrains *syntax*, not *semantics*: the schema is not shown to the model (llama.cpp docs say so explicitly) — always put the schema and an example in the prompt too. Keep schemas flat: enums, required fields, integers, no `anyOf`, no nested `$ref` (llama.cpp converter breaks on these).
- With thinking models, constrain only the final answer: vLLM/SGLang do this natively (structural tags / `enable_in_reasoning`); with llama.cpp, disable thinking for extraction calls (`enable_thinking: false` via `chat_template_kwargs`) or the grammar will fight the `<think>` block.
- Ollama's default context is **4,096 tokens** (**Verified** FAQ) — our 6k/8k prompts get silently truncated unless `num_ctx` / `OLLAMA_CONTEXT_LENGTH` is raised.

**Which is best?**
- (i) Single workstation, one GPU (Windows or Linux): **llama.cpp `llama-server`** in router mode (`--models-dir`, `--models-max`), or **Ollama** for zero-ops. llama.cpp gives finer control (slots, `--n-cpu-moe` offload, speculative decoding, GBNF); with Ollama set `OLLAMA_NUM_PARALLEL`, `OLLAMA_CONTEXT_LENGTH`, `OLLAMA_KEEP_ALIVE=-1`.
- (ii) Linux box with 2+ GPUs: **vLLM** (or SGLang) with FP8/AWQ/NVFP4 weights, xgrammar structured outputs, one instance per GPU (large on GPU0, small on GPU1); vLLM **sleep mode** only if one card must time-share two models (**Assumption**).

---

## 3. Hardware sizing

### 3a. Verified specs and prices

| Device | Memory | Bandwidth | Power | Price (source) |
|---|---|---|---|---|
| RTX 4090 | 24 GB GDDR6X **Verified** | 1,008 GB/s (third-party spec tables) | 450 W TGP **Verified** | EOL; 2026 street price **Unknown** (no reliable Nov-2026 data) |
| RTX 5090 | 32 GB GDDR7, 512-bit **Verified** | 1,792 GB/s (third-party) | 575 W TGP **Verified** | MSRP $1,999 (NVIDIA, per Guru3D); **street $4,200–5,100 Aug–Sep 2026** (Guru3D/trackers, **Community**) |
| RTX 6000 Ada | 48 GB GDDR6 ECC **Verified** | 960 GB/s (third-party) | 300 W **Verified** | US street low ~$7,499 (Aug 2026 tracker, **Community**) |
| RTX PRO 6000 Blackwell (Workstation) | 96 GB GDDR7 ECC **Verified** | 1,792 GB/s **Verified** | 600 W **Verified** | ~$8,000–9,400 (June 2026 comparisons, **Community**) |
| NVIDIA DGX Spark (GB10) | 128 GB LPDDR5x unified **Verified** | 273 GB/s **Verified** | 140 W TDP SoC, 240 W PSU **Verified** | MSRP raised $3,999 → **$4,699** (NVIDIA forum, Feb 2026) **Verified** |
| Framework Desktop, Ryzen AI Max+ 395 | 128 GB LPDDR5x-8000, 256-bit **Verified** (AMD) | 256 GB/s theoretical (derived, not published by AMD) | ~120 W configurable (community) | **$3,449** (Framework, out of stock at check) **Verified** |
| Mac Studio M5 Max | up to 128 GB unified | 614 GB/s (press) **Community** | — | from $2,499 (36 GB) **Verified**-press; 128 GB config price **Unknown** |
| Mac Studio M5 Ultra | 96 / 256 / 512 GB | 1.2 TB/s (Apple claim via press) | — | $5,499 (96 GB); 256 GB ≈ $10,799 (needs the $6,799 chip tier + $4,000); 512 GB $22,299 **Verified**-press (Oct 28 2026 availability) |

Sources: NVIDIA product pages for RTX 5090, RTX 4090, RTX 6000 Ada, RTX PRO 6000, DGX Spark; https://forums.developer.nvidia.com/t/2-23-2026-price-change-announcement/361713 ; https://frame.work/products/desktop-diy-amd-aimax300 ; https://www.amd.com/en/products/processors/laptop/ryzen/ai-300-series/amd-ryzen-ai-max-plus-395.html ; https://www.guru3d.com/story/geforce-rtx-5090-retail-prices-reach-as-standard-aircooled-cards-approach-halomodel-pricing/ ; https://www.techrepublic.com/article/news-apple-mac-studio-512gb-ram-price-specs/ (accessed 2026-11-04).

### 3b. Measured tokens/s (all **Community**, llama.cpp unless noted; TG = generation, PP = prompt processing)

| Device | 8–9B Q4 | 27–32B dense Q4 | 70B Q4 | 120B-class MoE | Source |
|---|---|---|---|---|---|
| RTX 4090 | Qwen3-8B: TG 141 / PP 9,250 (4k) | Qwen3-32B: TG 39.6 / PP 2,448 (4k); gpt-oss-20b TG 190 | "cannot run 70B at any quant" (page claim; IQ2 would fit, badly) | gpt-oss-120b only with CPU MoE offload | hardware-corner.net RTX 4090 page, Mar 2026 |
| RTX 5090 | Qwen3-8B: TG 186 / PP 10,406 (4k) | Qwen3-32B: TG 61 / PP 2,931 (4k); Qwen3.8-27B ~120–140 t/s with NVFP4+MTP (DataCamp, laptop/desktop mixed) | ~13 t/s partial offload (one run, low confidence) | gpt-oss-120b F16 GGUF with `--n-cpu-moe`: 8–10 t/s; 20 t/s with a second GPU (Reddit) | hardware-corner.net RTX 5090 pages; Reddit |
| RTX PRO 6000 Blackwell | — | Qwen3-32B: TG 54.9 / PP 3,137 (4k), 39.9 at 32k | Llama-3.3-70B: TG 32.0 / PP 1,689 (4k); 16.6 at 128k | gpt-oss-120b MXFP4: **TG 210 / PP 4,579** (4k), 99.8 at 128k; Qwen3.5-122B-A10B Q4: **TG 98 / PP 3,055**, 65.6 at 256k; Mistral-123B dense Q4: 19 t/s | hardware-corner.net PRO 6000 page, Mar 2026 |
| DGX Spark | — | — | 70B FP8 SGLang batch-1: 2.7 t/s (LMSYS) | gpt-oss-120b MXFP4: **TG 60.6 / PP 1,956** (b6922, Nov 2025); 40.5 TG at 32k depth | github.com/ggml-org/llama.cpp/discussions/16578 |
| Strix Halo 128 GB | Llama-2-7B Q4: TG 47 | Qwen2.5-32B Q4: **TG 11.3 / PP 242**; Qwen3-30B-A3B: TG 86 | Llama-3.1-70B Q4: **TG 5.1 / PP 82** | gpt-oss-120b Q4: **TG 53 / PP 120** (server-measured; HN user: PP 420, TG >40) | github.com/visorcraft/strix-halo-llm-perf (Feb–Mar 2026) |
| Mac Studio M3 Ultra 512 GB (MLX) | — | Qwen 32B 4-bit: TG 31 (1k ctx), 8.5 (128k); PP 345→154 | Llama-3.3-70B 4-bit: 6.5–15.5 | GLM-5.2 4-bit: 17.7 TG / 214 PP; DeepSeek-V3 4-bit ~20 TG | pinggy.io M5 Ultra article (M3 Ultra measured; M5 Ultra numbers are 1.46× **estimates**, no measurements yet) |

Rule of thumb (**Assumption**, bandwidth-bound decode): single-stream TG ≈ 0.5–0.7 × bandwidth / active-bytes-per-token: 27B dense Q4 (16.5 GB) ≈ 60–100 t/s at 1,792 GB/s, ≈ 10–15 t/s at 273 GB/s. MoE with 3–10B active is 3–10× faster at the same footprint — hence gpt-oss-120b / Qwen3.5-122B-A10B are the sweet spot for unified-memory boxes.

### 3c. Three concrete builds (prices are **Assumptions** built from the sources above)

Latency targets are for a 2k-token prompt → 500-token answer, **thinking off**. With thinking on, add 1–5k hidden tokens (3–10× longer) — budget it.

**Build A — Budget ≈ $2.5k: one 24 GB GPU workstation.**
- Used RTX 4090 or 3090 24 GB ≈ $1,000–1,800 (**Unknown**, volatile) + desktop, 64 GB DDR5, 2 TB NVMe ≈ $900.
- Runs: Qwen3.8-27B UD-Q4_K_M (16.5 GB) resident with 16–32k KV; Qwen3.5-9B Q4 (5.7 GB) swapped in via llama.cpp router / Ollama (both resident = 22.2 GB, too tight), or 9B on CPU for the 12 extraction calls/hour.
- Expected: 27B on 4090 ≈ TTFT ~0.9 s (PP 2,400 t/s); TG 40–60 t/s (Qwen3-32B measured 39.6; Qwen3.8-27B reported >100 t/s with MTP) → **~6–14 s total**; 9B ≈ 4 s. No local tier (c); API fallback for strategy research.

**Build B — Mid ≈ $6.5–7.5k: RTX 5090 32 GB + 128 GB DDR5 Linux/Windows box.**
- RTX 5090 at street ≈ $4,500 (MSRP $1,999 if found) + 16-core CPU, 128 GB DDR5, 2 TB NVMe, 1,000 W PSU ≈ $2,000–2,500.
- Runs: 27B Q4 + 9B Q4 **concurrently** with ~8 GB KV headroom (or Gemma 4 31B Q4, 18.3 GB). gpt-oss-120b MXFP4 (63 GB) via `--n-cpu-moe` at 8–20 t/s (**Community**) for the nightly review only.
- Expected: 27B: TTFT ~0.7 s, TG 55–100 t/s → **~6–10 s**; 9B **~3–4 s**; offloaded gpt-oss-120b: 25–60 s per 500 tokens (batch only).
- Alternatives at the same money: **Mac Studio M5 Ultra 96 GB ($5,499)** runs gpt-oss-120b in memory (27B dense ~45 t/s **estimate**; MLX/llama.cpp Metal only). **Framework Desktop 128 GB ($3,449)** runs 120B-class MoE at ~50 t/s but dense 27B at ~11 t/s — fine for background agents, poor for chat.

**Build C — High ≈ $11–13k: RTX PRO 6000 Blackwell 96 GB workstation (Linux, vLLM).**
- GPU ≈ $8,500 + Threadripper-class platform, 128–256 GB DDR5, 4 TB NVMe, 1,200 W PSU ≈ $3,000–4,000.
- Runs resident: Qwen3.5-122B-A10B GPTQ-Int4 (~65 GB) or gpt-oss-120b MXFP4 (63 GB) + Qwen3.5-9B FP8 (10 GB) + KV ≈ 90 GB. 122B serves analysis and chat, 9B extraction; a later second GPU takes the 27B.
- Expected: Qwen3.5-122B-A10B: TTFT ~0.7 s, TG 98 t/s → **~6 s**; gpt-oss-120b: **~3 s** (TG 210); 70B dense Q4: ~16 s. vLLM batched throughput ~1,700 t/s (**Community**) is far above our needs.
- Alternative: **Mac Studio M5 Ultra 256 GB (~$10,799)** for Qwen3.5-397B-A17B or GLM-5.3-Flash at 4-bit, 15–25 t/s (**estimate** = M3 Ultra GLM-5.2 17.7 t/s × ~1.46). The 512 GB config ($22,299) is only needed for GLM-5.2 / DeepSeek V4 class.

---

## 4. Latency and reliability

- **TTFT** ≈ prompt_tokens / PP rate. 2k prompt: 0.2–1 s on a desktop GPU; 1–2 s on DGX Spark; 5–15 s on Strix Halo/Mac for dense 27–32B (PP 120–350 t/s). The nightly 200k-token review (long-context PP degrades 3–10×, see §3b) takes **2–10 min** on a GPU, **20–60 min** on unified-memory boxes (**Assumption**).
- **Throughput:** continuous batching (vLLM/SGLang/llama.cpp `-np`) multiplies aggregate throughput 5–30× when several agents fire together, with modest per-stream loss for MoE models (**Assumption**).
- **Cold start:** 16–65 GB from NVMe ≈ **5–20 s** for GGUF (mmap), 30–90 s for vLLM (CUDA graphs, KV allocation) (**Assumption**; vLLM sleep-mode resume measured ~2.7× faster than reload, **Community**). Keep warm: Ollama `keep_alive: -1` (**Verified**), llama.cpp router `--models-max` with LRU unload (**Verified**), vLLM long-running process or sleep mode.
- **Two models concurrently:** one process per model with explicit VRAM budgets (`--gpu-memory-utilization` in vLLM; `-ngl`/`-c` in llama.cpp); Ollama / llama.cpp router do it automatically if both fit. Avoid load/unload churn — it causes **GPU memory fragmentation** and spurious OOMs; restart the server nightly in the review window (**Assumption**).
- **Failure modes → mitigations (Assumption, standard practice):**
  - *OOM / context overflow:* fixed `max-model-len`; count tokens before calling; truncate oldest news; map-reduce the 200k review in 32–64k windows; Ollama `num_ctx` set explicitly.
  - *Infinite generation / repetition:* always set `max_tokens`; vendor sampling presets (Qwen3.8 instruct: temp 0.7 / top_p 0.8 / presence_penalty 1.5 **Verified**); stop strings; grammar closes generation when the schema closes.
  - *Invalid JSON:* decode-time schema enforcement (§2) + Pydantic validation + one retry with the validator error; flat enum schemas; thinking off for extraction.
  - *Hallucinated tool calls:* structural tags (SGLang/vLLM) or llama.cpp tool grammar; validate name and arguments against the registry; tools are read-only — never order placement.
  - *Timeouts:* per-call deadlines (extraction 10 s, analysis 60 s, chat 30 s TTFT) → smaller model → rule-based default, with the degradation recorded.
  - *Drift after upgrades:* pin GGUF hashes / HF revisions; re-run §7 before promoting.

---

## 5. Running-cost comparison

### 5a. Workload model (**Assumptions**)
- 24/5 operation: 120 h/week ≈ 520 h/month → 1 analysis call per 5 min = **6,240 calls/mo × (6k in / 0.8k out)** = **37.4M in / 5.0M out**.
- Chat: 50 turns/day × 22 trading days = 1,100 turns × (8k/1k) = **8.8M in / 1.1M out**.
- Nightly review: 200k in + 5k out × 22 = **4.4M in / 0.11M out**.
- **Total ≈ 50.6M input + 6.2M output tokens/month.** Thinking/reasoning tokens are billed as output; if enabled this could add 3–5× output (not included below).

### 5b. API cost per month (list prices, no caching/batch discounts; all prices **Verified** on official pricing pages 2026-11-04 unless marked)

| Provider / model | $/M in | $/M out | Monthly |
|---|---|---|---|
| Anthropic Claude Haiku 5.5 (≤100k prompts; >100k tier $0.50/$2.50 applied to the nightly 4.4M) | 0.10 | 0.50 | 46.2×0.10 + 6.09×0.50 + 4.4×0.50 + 0.11×2.50 ≈ **$10** |
| Anthropic Claude Sonnet 5.5 | 2 | 10 | 101.3 + 62 = **$163** |
| Anthropic Claude Opus 5.5 | 4 | 20 | 202.6 + 124 = **$327** |
| Anthropic Claude Fable 5.1 | 10 | 50 | 506 + 310 = **$816** |
| OpenAI gpt-5.6-luna | 0.20 | 1.20 | 10.1 + 7.4 = **$18** |
| OpenAI gpt-5.6-terra | 2 | 12 | 101.3 + 74.4 = **$176** |
| OpenAI gpt-5.5 | 5 | 30 | 253 + 186 = **$439** |
| Google Gemini 3.5 Flash-Lite | 0.30 | 2.50 | 15.2 + 15.5 = **$31** |
| Google Gemini 3.8 Flash | 0.75 | 3.75 | 38.0 + 23.3 = **$61** |
| Google Gemini 4 Argon (doubles 2026-12-01) | 2 | 10 | **$163 → $326** |
| DeepSeek `deepseek-flash` (V4.1-Flash-1017), off-peak / peak | 0.15 / 0.30 | 0.60 / 1.20 | **$11 / $23** |
| DeepSeek `deepseek-v4-pro`, peak | 1.32 | 3.96 | 66.8 + 24.6 = **$91** |
| Together / Fireworks gpt-oss-120B | 0.15 | 0.60 | **$11** |
| Groq gpt-oss-120b (third-party trackers, **Community**) | 0.15 | 0.60 | **$11** |
| Together GLM-5.3-Flash | 0.15 | 0.50 | **$11** |
| Together Qwen3.8 Flash | 0.15 | 0.47 | **$10** |
| Together Kimi K3 | 3.00 | 15.00 | 152 + 93 = **$245** |

Prompt caching would cut the recurring 6k analysis prompt by 50–90% on Anthropic/OpenAI/DeepSeek (cache-hit input $0.25–0.50/M on Claude 5.x, $0.003/M on DeepSeek — **Verified** prices; savings **Assumption**).

### 5c. Local cost per month (**Assumptions** with cited inputs)

Electricity: box on 24/7 (730 h) to keep models warm. Idle draw with a loaded model ≈ 80 W → 58 kWh. Busy ≈ 1.5 h/day from §3 timings (6,240 × ~17 s + 1,100 × ~21 s + nightly ~10 min) ≈ 45 h/mo at ~500 W (5090 575 W / PRO 6000 600 W TGP **Verified**) → 22 kWh. **≈ 80 kWh/mo → $12 at $0.15/kWh, $24 at $0.30/kWh.** Unified-memory boxes (DGX Spark 140 W SoC, Strix Halo ~120 W, Mac) ≈ 30–45 kWh → $5–14.

| Build | Capex | Amortised /mo (36 mo) | Electricity /mo | **Total /mo** |
|---|---|---|---|---|
| A (24 GB GPU) | $2,500 | $69 | $12–24 | **$81–93** |
| B (RTX 5090 + 128 GB RAM) | $7,000 | $194 | $12–24 | **$206–218** |
| B-alt (Framework 128 GB) | $3,449 + $300 | $104 | $5–10 | **$109–114** |
| C (RTX PRO 6000 96 GB) | $12,000 | $333 | $14–26 | **$347–359** |
| C-alt (Mac Studio M5 Ultra 256 GB) | $10,799 | $300 | $6–12 | **$306–312** |

**Conclusion (Assumption):** at ~57M tokens/month the cheapest APIs ($10–25/mo: gpt-oss-120b hosts, DeepSeek Flash, Haiku 5.5) are 5–30× cheaper than any local build; local wins on cost only against frontier APIs ($160–800/mo) or if volume grows ~10× (more agents, reasoning on — plausible). The real case for local is **privacy of positions/strategy, no rate limits, no forced deprecations or repricing (Gemini 2.5 re-routing and Argon doubling on 2026-12-01), deterministic latency** — not raw cost.

---

## 6. Hybrid design: local-first, API as an option

Gating policy (**Assumption**, recommended):
1. **Tier routing:** sentiment / JSON extraction → local 9B; analysis & post-trade review → local 27B/31B; chat → local mid or large; weekly strategy research (long, rare, non-urgent) → local large if present, else API.
2. **API allowed only for:** (a) weekly strategy-research synthesis, (b) chat the user explicitly tags "use cloud", (c) degraded mode when local is down >N minutes and the task is not account-related. Never for post-trade review (fills, sizes, P&L).
3. **Data classes:** *public* (news, prices, indicators) may leave; *derived* (signals, regime labels, backtests) local by default; *account* (positions, balances, orders, broker IDs, credentials) never leaves — deterministic scrubber (regex on account/ticket numbers, balances) before any API call; log every outbound payload hash.
4. **Provider choice:** OpenAI — API data not used for training unless opted in; abuse-monitoring logs ≤30 days; ZDR by approval, forces `store=false` (**Verified** https://developers.openai.com/api/docs/guides/your-data). Anthropic — API inputs not used for training by default (**Verified** https://privacy.claude.com/en/articles/7996868); retention/ZDR terms live in the Commercial Terms / Trust Center (**Unknown** here; third-party sources conflict 7 vs 30 days). DeepSeek and other China-hosted APIs: terms **Unknown** — public data only. US open-model hosts (Together/Fireworks/Groq): check terms (**Unknown**).
5. **Compliance:** for a regulated entity, outbound trading data to a processor normally needs a DPA, processing-register entry and retention controls; client personal data never in prompts; audit-log model, version, prompt hash and output for every decision-influencing call (**Assumption**).

---

## 7. Evaluation for OUR tasks

Build three small, versioned eval sets (CSV/JSONL, 150–300 items each, labelled by us):
1. **Headline → JSON** `{sentiment: [-1,0,1] or enum, horizon: enum, impact_on_gold: enum(low/med/high), drivers: [enum...], confidence: 0–1}` — label ~300 historical gold-relevant headlines (Fed, CPI, USD, real yields, geopolitics, ETF flows).
2. **Chart/stat summary → regime label** (trend-up / trend-down / range / high-vol) from a text table of indicators; gold-standard from a rule-based labeller plus manual review.
3. **Trade-review rationale**: given entry/exit/context, model writes a review; scored by an LLM-judge rubric (did it identify the actual error class? no hallucinated facts?) with a 20-item human-graded calibration subset.

Metrics: JSON validity (with/without grammar), schema conformance, enum agreement and macro-F1 vs labels, run-to-run kappa at temp 0, p50/p95 latency, tokens/s, cost or GPU-seconds, empty-output rate, hidden-token count for thinking models.

Harness: **promptfoo** (config-driven; `is-json` with JSON Schema validation, `javascript`/`python` scorers, `llm-rubric`, `latency`, `cost`; any OpenAI-compatible endpoint) **Verified** https://www.promptfoo.dev/docs/configuration/expected-outputs/ — tasks 1–2 and CI gating. **Inspect AI** (UK AISI; dataset/solver/scorer, `model_graded_qa`, tool-use agents, vLLM/SGLang/HF providers, log viewer) **Verified** https://inspect.aisi.org.uk/ — task 3 and agentic evals. lm-eval-harness targets academic benchmarks, not our JSON tasks (**Assumption**). Point both at the production serving endpoint with production prompts so eval == prod.

---

## 8. Embeddings and retrieval for chat memory

| Model | Licence | Size | Dim | Max seq | Notes |
|---|---|---|---|---|---|
| **Qwen3-Embedding-0.6B / 4B / 8B** | Apache-2.0 **Verified** | 0.6–8B | 1024 / 2560 / up to 4096 (MRL 32–4096) **Verified** | 32K **Verified** | 8B: MTEB multilingual 70.58 (#1 June 2025), English v2 75.22 **Verified**; 0.6B is the practical local choice (GGUF, runs in llama.cpp `/v1/embeddings`). |
| **bge-m3** | MIT **Verified** | ~568M (community) | 1024 **Verified** | 8192 **Verified** | Dense + sparse + ColBERT multi-vector in one model **Verified** — enables hybrid retrieval without a second model. |
| nomic-embed-text-v2-moe | Apache-2.0 **Verified** | 475M / 305M active | 768 (MRL to 256) **Verified** | **512 tokens** **Verified** | Short context limits it to headline/chunk embeddings. |

Vector stores:
- **sqlite-vec** — single-file, pure C, float/int8/binary vectors, Python/Node/Go/Rust; **pre-v1, breaking changes expected** **Verified** https://github.com/asg017/sqlite-vec. Ideal if the trading DB is already SQLite and memory < ~1M chunks.
- **pgvector 0.8.7** — HNSW/IVFFlat, `halfvec` (4,000-dim indexable), `sparsevec`, iterative index scans for filtered queries **Verified** https://github.com/pgvector/pgvector. Best if trades/news already live in Postgres; one DB for everything.
- **LanceDB** — embedded, Lance columnar format, vector + full-text + SQL filters, automatic versioning, Python/JS/Rust **Verified** https://github.com/lancedb/lancedb. Good for time-versioned news archives.
- **Qdrant** — payload-indexed filtering during HNSW traversal, BM25 + sparse (SPLADE/miniCOIL) hybrid **Verified** https://qdrant.tech/documentation/overview/. Overkill unless multi-service.

Recommendation (**Assumption**): Qwen3-Embedding-0.6B (or bge-m3 for hybrid) served by llama.cpp/vLLM, stored in **pgvector** if Postgres is the system of record, else **sqlite-vec**. Chunk by news item / trade / daily summary, store timestamp + instrument + source as filterable metadata, and always time-filter (no look-ahead leakage into backtests).

---

## Recommended default stack

- **Tier (a) extraction/sentiment:** Qwen3.5-9B (Apache-2.0), thinking off, temp 0, JSON schema enforced; fallback gpt-oss-20b (faster, Apache) or Gemma 4 12B.
- **Tier (b) analysis/review/chat:** Qwen3.8-27B UD-Q4_K_M (Apache-2.0), `reasoning_effort: low/medium` for analysis, thinking off for chat unless asked; alternative Gemma 4 31B (multimodal, Apache).
- **Tier (c) strategy research:** locally Qwen3.5-122B-A10B GPTQ-Int4 (96 GB GPU) or gpt-oss-120b MXFP4 (any 80+ GB device, fastest); via API DeepSeek V4.1-Flash / GLM-5.3-Flash / gpt-oss-120b on a US host for public-data-only research; Claude Sonnet 5.5 or Opus 5.5 when quality matters and data is redacted.
- **Serving:** single-GPU workstation → llama.cpp `llama-server` router mode (GBNF JSON schema, slots, keep-warm) behind a thin OpenAI-compatible gateway; multi-GPU Linux → vLLM (xgrammar structured outputs, FP8/Int4, one process per GPU).
- **Hardware:** start with Build B (RTX 5090 + 128 GB RAM) if a 5090 is obtainable near MSRP; otherwise Build C (RTX PRO 6000 96 GB) is the only single-card option that runs tier (c) at chat speed. Build A is adequate for tiers (a)+(b) only.
- **Embeddings/memory:** Qwen3-Embedding-0.6B + pgvector.
- **Eval:** promptfoo in CI with the three eval sets; Inspect AI for agentic reviews.

## Fallback ladder

1. Primary local model times out or server unreachable → retry once (new request, same model, lower `max_tokens`).
2. Still failing → next-smaller local model (27B → 9B; 122B → 27B) with the same schema.
3. Invalid/unparseable JSON → re-ask with validator error appended (1 retry) → if still invalid, **rule-based default** (sentiment neutral, impact low, regime "unknown") and flag `degraded=true` in the record.
4. Local tier (c) unavailable for the weekly research job → queue it; if >24 h late and data is public/redacted → API (DeepSeek Flash / gpt-oss-120b host / Claude Sonnet 5.5), logged.
5. Chat assistant: if local is slow (>10 s TTFT) → inform the user and offer cloud mode only for non-account questions.
6. Never: auto-escalate account-data prompts to an API; never let an LLM failure block or alter execution logic.

## Key takeaways

1. The 2026 open-weight field is Apache/MIT-heavy (Qwen3.5/3.6/3.8 except Flash-Next, Gemma 4, gpt-oss, Mistral Small 4, GLM-5.x, DeepSeek V4.x); Kimi K3 and Mistral Large 4 restrict only MaaS resale; Llama 4 is obsolete.
2. Qwen3.8-27B (dense, hybrid linear attention, 262k context, 16.5 GB at Q4) is the best mid-tier model for a 24–32 GB GPU; Gemma 4 31B is the multimodal Apache alternative.
3. For tier (c), MoE models with ≤10B active parameters (gpt-oss-120b, Qwen3.5-122B-A10B, Mistral Small 4) are the only affordable fast local options: 98–210 t/s on a 96 GB card, 50–60 t/s on 128 GB unified-memory boxes.
4. Dense 70B is a poor deal now (32 t/s on an $8.5k card, 5 t/s on Strix Halo); skip.
5. Every mainstream server enforces JSON schemas at decode time; llama.cpp silently drops unsupported schema keywords — keep schemas flat and validate with Pydantic anyway.
6. Thinking is on by default in Qwen3.x and GLM; turn it off for extraction and cap `reasoning_effort` for analysis, or latency and cost multiply 3–10×.
7. Ollama's 4,096-token default context silently truncates our 6–8k prompts; set it explicitly.
8. 2026 GPU prices are distorted (RTX 5090 street ≈ 2.3× MSRP; DGX Spark now $4,699; Mac Studio 512 GB $22,299); the RTX PRO 6000 96 GB at ~$8.5k is the best single-card buy.
9. At ~57M tokens/month, cheap APIs cost $10–25/mo versus $80–360/mo local; local is a privacy/control decision, not a saving, unless volume grows ~10×.
10. Frontier APIs cost $160–800/mo here and reprice or re-route on short notice (Gemini 4 Argon doubles 2026-12-01).
11. Keep models warm (keep_alive −1, router mode); cold starts are 5–90 s and load/unload churn fragments VRAM.
12. Finance-tuned models (Fin-R1, Agentar-Fin-R1, FinGPT) sit on 2024–25 bases and target exam QA; build our own headline/regime/review evals.
13. Gate every model or quant upgrade in CI with promptfoo `is-json` schema, agreement and latency assertions.
14. Embeddings: Qwen3-Embedding-0.6B or bge-m3 into pgvector/sqlite-vec; always time-filter retrieval to avoid look-ahead leakage.
15. All tokens/s figures are community measurements with wide variance; run `llama-bench` on the actual box with our prompt sizes before buying.

## Sources (all accessed 2026-11-04)

Models: https://huggingface.co/Qwen ; https://huggingface.co/collections/Qwen/qwen38 ; https://huggingface.co/collections/Qwen/qwen36 ; https://huggingface.co/collections/Qwen/qwen35 ; https://huggingface.co/Qwen/Qwen3.8-27B ; https://huggingface.co/Qwen/Qwen3.8-Flash-Next ; https://huggingface.co/Qwen/Qwen3.6-27B ; https://huggingface.co/Qwen/Qwen3.6-35B-A3B ; https://huggingface.co/Qwen/Qwen3.5-9B ; https://huggingface.co/Qwen/Qwen3.5-122B-A10B ; https://huggingface.co/unsloth/Qwen3.8-27B-GGUF ; https://huggingface.co/unsloth/Qwen3.5-9B-GGUF ; https://huggingface.co/google/gemma-4-31b-it ; https://huggingface.co/google/gemma-4-26b-a4b-it ; https://huggingface.co/google/gemma-4-12b-it ; https://huggingface.co/google/gemma-4-E4B-it ; https://huggingface.co/unsloth/gemma-4-31B-it-GGUF ; https://huggingface.co/openai/gpt-oss-120b ; https://huggingface.co/openai/gpt-oss-20b ; https://huggingface.co/ggml-org/gpt-oss-120b-GGUF ; https://huggingface.co/zai-org/GLM-5.2 ; https://huggingface.co/zai-org/GLM-5.3-Flash ; https://huggingface.co/deepseek-ai ; https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017 ; https://huggingface.co/moonshotai/Kimi-K3 ; https://huggingface.co/moonshotai/Kimi-K3/blob/main/LICENSE ; https://huggingface.co/mistralai/Mistral-Small-4-119B-2603 ; https://huggingface.co/mistralai/Mistral-Large-4-1T-A52B ; https://huggingface.co/meta-llama ; https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16 ; https://huggingface.co/MiniMaxAI/MiniMax-M3 ; https://huggingface.co/SUFE-AIFLM-Lab/Fin-R1 ; https://arxiv.org/abs/2507.16802 ; https://arxiv.org/abs/2310.04793 ; https://arxiv.org/abs/2508.10925.

Serving: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md ; https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md ; https://docs.ollama.com/capabilities/structured-outputs ; https://docs.ollama.com/faq ; https://docs.vllm.ai/en/latest/features/structured_outputs.html ; https://docs.vllm.ai/en/latest/features/quantization/ ; https://docs.vllm.ai/en/latest/getting_started/installation/gpu.html ; https://docs.vllm.ai/en/stable/features/sleep_mode/ ; https://docs.sglang.io/advanced_features/structured_outputs.html ; https://docs.sglang.io/get_started/install.html ; https://lmstudio.ai/docs/app/api/structured-output ; https://lmstudio.ai/docs/developer/core/headless ; https://huggingface.co/docs/text-generation-inference/en/conceptual/guidance ; https://github.com/theroyallab/tabbyAPI ; https://github.com/ml-explore/mlx-lm ; https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md.

Hardware and benchmarks: https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5090/ ; https://www.nvidia.com/en-us/geforce/graphics-cards/40-series/rtx-4090/ ; https://www.nvidia.com/en-us/design-visualization/rtx-6000/ ; https://www.nvidia.com/en-us/products/workstations/professional-desktop-gpus/rtx-pro-6000/ ; https://www.nvidia.com/en-us/products/workstations/dgx-spark/ ; https://forums.developer.nvidia.com/t/2-23-2026-price-change-announcement/361713 ; https://frame.work/products/desktop-diy-amd-aimax300 ; https://www.amd.com/en/products/processors/laptop/ryzen/ai-300-series/amd-ryzen-ai-max-plus-395.html ; https://www.apple.com/shop/buy-mac/mac-studio ; https://www.techrepublic.com/article/news-apple-mac-studio-512gb-ram-price-specs/ ; https://www.guru3d.com/story/geforce-rtx-5090-retail-prices-reach-as-standard-aircooled-cards-approach-halomodel-pricing/ ; https://www.hardware-corner.net/rtx-5090-llm-benchmarks/ ; https://www.hardware-corner.net/gpu-llm-benchmarks/rtx-4090/ ; https://www.hardware-corner.net/gpu-llm-benchmarks/rtx-pro-6000-blackwell/ ; https://www.hardware-corner.net/guides/gpt-oss-offloading-moe-layers/ ; https://github.com/ggml-org/llama.cpp/discussions/16578 ; https://github.com/visorcraft/strix-halo-llm-perf ; https://pinggy.io/blog/self_hosting_llms_on_512gb_m5_ultra_mac_studio/ ; https://www.pcworld.com/article/2916928 ; https://www.databasemart.com/blog/vllm-gpu-benchmark-pro6000.

Pricing and data policy: https://platform.claude.com/docs/en/about-claude/pricing ; https://developers.openai.com/api/docs/pricing ; https://aistudio.google.com/docs/pricing ; https://api-docs.deepseek.com/quick_start/pricing ; https://www.together.ai/pricing ; https://fireworks.ai/pricing ; https://www.morphllm.com/groq-pricing (third-party; groq.com/pricing did not expose a table) ; https://developers.openai.com/api/docs/guides/your-data ; https://privacy.claude.com/en/articles/7996868-is-my-data-used-for-model-training.

Eval and retrieval: https://www.promptfoo.dev/docs/intro/ ; https://www.promptfoo.dev/docs/configuration/expected-outputs/ ; https://inspect.aisi.org.uk/ ; https://huggingface.co/Qwen/Qwen3-Embedding-8B ; https://huggingface.co/BAAI/bge-m3 ; https://huggingface.co/nomic-ai/nomic-embed-text-v2-moe ; https://github.com/asg017/sqlite-vec ; https://github.com/pgvector/pgvector ; https://github.com/lancedb/lancedb ; https://qdrant.tech/documentation/overview/.
