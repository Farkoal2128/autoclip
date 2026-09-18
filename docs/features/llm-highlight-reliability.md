# Feature: reliable highlight detection for long videos

Status: proposed

## Summary

AutoClip should distinguish a video with genuinely no usable highlights from a job where the LLM path failed, truncated input, timed out, returned malformed data, or was called with an incompatible model configuration.

The current user-facing error is:

> No clips were found. This can mean the video genuinely has no self-contained highlights, or that the model struggled with the transcript — try a larger model or a different provider.

That message collapses several materially different conditions into one outcome. This feature adds per-window diagnostics, safer local-context handling, model-capability-aware hosted requests, and a reproducible model evaluation path for videos up to roughly six hours.

The feature must support both:

- local Ollama models, and
- official OpenAI API models through AutoClip's OpenAI provider.

The goal is not to solve the problem by blindly choosing a larger model. The system should first prove whether the transcript produced valid empty results or whether the analysis path failed.

## Why this matters

Highlight detection is the only pipeline stage where AutoClip asks an LLM to make an editorial judgement. A failure here can waste the cost of transcription and every earlier stage.

Long videos make the ambiguity worse because many LLM calls are involved. Under the current windowing strategy, a six-hour continuous transcript is analyzed as many overlapping requests rather than one full-transcript request. A job can therefore fail in dozens of different windows and still surface only the same final "No clips were found" message.

A user should be able to tell the difference between:

- "the model examined every window successfully and found no strong clips", and
- "the model did not successfully analyze most of the transcript".

## Verified current behavior

The following behavior is present in the current `main` implementation and should be re-verified whenever this feature is implemented.

### Windowing

`backend/autoclip/pipeline/highlights.py` currently:

- uses 8-minute transcript windows,
- overlaps adjacent windows by 60 seconds,
- analyzes Ollama windows with local concurrency 1,
- analyzes hosted-provider windows with concurrency 3,
- catches any exception raised by a window and substitutes an empty candidate list,
- raises the generic "No clips were found" error when the combined raw candidate list is empty.

For approximately six hours of continuous speech, the current 8-minute window / 1-minute overlap scheme is roughly 52 LLM requests. The exact count depends on transcript timing and must be measured from the generated windows rather than hard-coded.

### Ollama provider

`backend/autoclip/providers/ollama_provider.py` currently:

- calls `/api/generate`,
- requests JSON output with `format: "json"`,
- sends `temperature`,
- uses a 600-second request timeout,
- does not explicitly set `num_ctx`,
- returns only the response text and discards Ollama timing/token metadata,
- lists `llama3.1:8b`, `qwen2.5:14b`, `mistral-nemo:12b`, and `gemma2:9b` as recommended models.

This is important because AutoClip expands every word into an indexed form such as `[1042]word`. Indexed text is substantially more token-expensive than a normal transcript, so an eight-minute request may require more runtime context than expected from transcript duration alone.

### OpenAI provider

`backend/autoclip/providers/openai_provider.py` currently:

- uses `AsyncOpenAI`,
- calls `chat.completions.create`,
- always sends `temperature`,
- requests `response_format={"type": "json_object"}` when the endpoint accepts it,
- retries without `response_format` when an OpenAI-compatible endpoint rejects that parameter,
- lists `gpt-4o`, `gpt-4o-mini`, and `o4-mini` as suggested models.

The provider does not currently maintain a model-capability map. Newer official OpenAI models may differ in supported request parameters, structured-output behavior, and preferred endpoints. An unsupported parameter can therefore become a per-window exception which the highlight pipeline later turns into an empty result.

### Prompt behavior

`backend/autoclip/prompts/highlight_v1.txt` intentionally favors precision over recall. It tells the model to:

- omit clips scoring below 50,
- return nothing rather than pad the result,
- treat many ordinary transcript sections as having no worthwhile clips.

That behavior is reasonable for editorial quality, but it makes valid-empty windows indistinguishable from failed windows unless the pipeline records diagnostics.

## Goals

1. Preserve the existing editorial standard while eliminating ambiguous failure reporting.
2. Make long-video analysis observable at the per-window and aggregate levels.
3. Prevent local-context truncation from masquerading as poor model judgement.
4. Prevent known hosted-model API incompatibilities from masquerading as no highlights.
5. Provide evidence-based model guidance for local and OpenAI users processing videos up to approximately six hours.
6. Keep the default architecture resumable and suitable for unattended batch processing.
7. Avoid exposing transcript text, secrets, or API keys in diagnostics.

## Non-goals

- Sending every six-hour transcript to one giant context window by default.
- Guaranteeing that a clip will go viral.
- Replacing human editorial judgement with a single benchmark score.
- Making every OpenAI-compatible third-party provider behave identically to OpenAI.
- Requiring a large local model on hardware that cannot run it reliably.

## Feature requirements

### 1. Record the outcome of every highlight window

Each window should produce a structured diagnostic record in addition to its clip candidates.

Suggested categories:

- `success_with_candidates`
- `success_empty`
- `empty_response`
- `json_repaired`
- `malformed_response`
- `schema_failure`
- `invalid_indices`
- `context_limit`
- `timeout`
- `rate_limit`
- `quota`
- `unsupported_parameter`
- `model_not_found`
- `network_error`
- `provider_error`
- `unknown_error`

A successful empty response must be counted separately from a failed request.

Diagnostics should include only safe metadata, for example:

- provider,
- model,
- window index,
- transcript word range,
- window duration,
- outcome category,
- retry count,
- elapsed time,
- input/output token counts where available,
- effective context size where available,
- sanitized error summary.

Do not store API keys or full transcript text in these records.

### 2. Replace the ambiguous zero-candidate failure with an aggregate diagnosis

If a job finishes with zero raw candidates, AutoClip should summarize what actually happened.

Example:

> 52 windows analyzed: 0 returned candidates, 4 returned valid empty results, 48 failed. Main failures: context limit 36, timeout 8, malformed response 4. The transcript was not successfully analyzed end-to-end.

If every window completed successfully and all returned valid empty results, the message can instead state that no clips met the configured editorial threshold.

The frontend/API should expose enough structured data to render a concise message and an expandable technical detail section.

### 3. Preserve provider response metadata

Provider adapters should be able to return response metadata alongside raw text.

For Ollama, capture useful fields when present, including:

- `prompt_eval_count`
- `prompt_eval_duration`
- `eval_count`
- `eval_duration`
- `load_duration`
- `total_duration`

For OpenAI, capture usage counts and request identifiers when available.

This data is needed to answer whether a request actually fit in context, how much work a six-hour job performs, and where latency is spent.

### 4. Make Ollama context explicit and measurable

AutoClip must not rely blindly on Ollama's runtime default context size.

Add an explicit context strategy with these properties:

- determine the rendered request size from the actual indexed transcript format,
- reserve output headroom,
- configure `num_ctx` large enough for one AutoClip window,
- avoid setting a model's maximum context when a smaller safe value saves memory,
- surface a clear error when the selected local model cannot fit the required context on the available hardware.

The implementation should prefer measured `prompt_eval_count` and runtime metadata over token-count guesses whenever possible.

The effective context value should be visible in diagnostics.

### 5. Add model-capability-aware OpenAI requests

The OpenAI provider should stop assuming every model accepts the same request shape.

Introduce capability handling for official OpenAI models so AutoClip can determine, per selected model:

- supported endpoint/API surface,
- structured-output method,
- whether `temperature` is accepted,
- context limits,
- output limits,
- other materially relevant request constraints.

Prefer schema-constrained structured output where supported rather than relying only on generic JSON-object mode.

Unsupported-parameter failures must be classified explicitly instead of disappearing into a zero-candidate result.

OpenAI-compatible third-party endpoints should retain a conservative fallback path and should not inherit OpenAI-specific assumptions unless verified.

### 6. Strengthen semantic validation

JSON validity is not enough. AutoClip should validate that proposed clips are usable.

At minimum validate:

- `start_word_index` exists in the requested window,
- `end_word_index` exists in the requested window,
- end is greater than start,
- the referenced text is non-empty,
- the quoted hook is reasonably consistent with the selected opening text,
- candidate boundaries can produce a clip within configured duration constraints after refinement.

Do not silently clamp grossly incorrect indices into apparently valid clips without recording that the model returned invalid coordinates.

A repair attempt may still be used, but repaired output and raw output quality should be measurable separately.

### 7. Keep current windowed analysis as the baseline architecture

A six-hour video does not inherently require a six-hour model context. The default implementation should continue to analyze bounded transcript windows unless testing shows a better architecture.

The feature must measure the real six-hour workload under the current design:

- total transcript words,
- exact number of windows,
- words per window,
- indexed tokens per window,
- system-prompt tokens,
- total input tokens per window,
- output tokens per window,
- duplication caused by overlap,
- total input/output tokens for the full job,
- local sequential runtime,
- hosted runtime at configured concurrency,
- peak local memory,
- hosted API cost,
- request-per-minute and token-per-minute demand.

### 8. Evaluate a two-pass highlight architecture

As a follow-up experiment, compare the existing strict per-window decision with a two-pass design:

1. high-recall candidate discovery on short windows,
2. global reranking of candidate excerpts with a stronger model.

The second pass should operate on candidate excerpts plus enough surrounding context rather than the entire raw six-hour transcript.

Measure whether this approach improves:

- false-empty rate,
- ranking consistency across the full video,
- duplicate removal,
- editorial quality,
- cost,
- latency.

Do not make two-pass processing the default until the benchmark demonstrates a material benefit.

## Six-hour model research

Model recommendations are date-sensitive and must be generated from current documentation and reproducible testing. The repository should not permanently encode a "best model" conclusion without evidence.

The benchmark should answer two separate questions:

1. What is the smallest/reasonable model that can reliably analyze one AutoClip window?
2. What is the total cost and runtime of processing all windows in a roughly six-hour video?

### Local Ollama candidates

Re-evaluate the repository's existing baselines:

- `llama3.1:8b`
- `qwen2.5:14b`
- `mistral-nemo:12b`
- `gemma2:9b`

Compare them with current Ollama-compatible candidates across approximately these classes:

- 4B
- 7B/8B
- 12B/14B
- 20B-32B
- larger models only where hardware and runtime are practical

Research seeds should include current strong Qwen, Gemma, Mistral, Meta/Llama, DeepSeek, and other credible open-weight families available through Ollama at implementation time.

For every candidate record:

- exact Ollama tag,
- parameter count,
- quantization,
- model file size,
- practical resident memory,
- KV/context memory at the AutoClip context target,
- advertised context,
- tested context,
- JSON/schema reliability,
- exact-index reliability,
- false-empty rate,
- editorial quality,
- prompt-ingestion speed,
- generation speed,
- per-window latency,
- estimated/observed six-hour runtime,
- whether the model is fully GPU-resident or offloaded.

Quantization comparisons should include verified available variants such as Q4, Q5, Q6, Q8, or higher precision when relevant. The benchmark should determine whether spending memory on a larger Q4/Q5 model produces better AutoClip results than a smaller Q8 model.

### Local hardware tiers

Provide guidance for at least:

| Environment | Required result |
| --- | --- |
| CPU-only / 16 GB system RAM | Feasibility, expected slowness, recommended model |
| Apple Silicon / 16 GB unified memory | Recommended model and safe context |
| Apple Silicon / 24 GB unified memory | Recommended model and safe context |
| Apple Silicon / 32 GB unified memory | Recommended model and safe context |
| 48 GB unified/system memory | Architecture-specific recommendation |
| 64 GB+ memory | Concrete tested capacity, not an unlimited assumption |
| NVIDIA 8 GB VRAM | Full-load vs offload recommendation |
| NVIDIA 12 GB VRAM | Full-load vs offload recommendation |
| NVIDIA 16 GB VRAM | Full-load vs offload recommendation |
| NVIDIA 24 GB VRAM | Full-load vs offload recommendation |

For NVIDIA results, state host-RAM assumptions. For Apple Silicon, treat unified memory as shared memory and leave headroom for the OS and other AutoClip stages.

### Official OpenAI candidates

Evaluate current official OpenAI models that are suitable for structured transcript analysis at implementation time. Do not assume the repository's current suggestions remain optimal.

For each model record:

- exact API model name,
- supported endpoint,
- context limit,
- output limit,
- structured-output support,
- relevant parameter restrictions,
- input price,
- output price,
- long-context pricing rules if any,
- per-window cost,
- estimated six-hour cost,
- rate-limit requirements,
- JSON/schema reliability,
- exact-index reliability,
- false-empty rate,
- editorial quality,
- latency.

The benchmark should include at least:

- a low-cost option suitable for routine jobs,
- a quality/performance default,
- the strongest practical quality option.

Do not recommend a one-shot full-transcript request solely because the model's context window is large enough. Compare it against windowed analysis on cost, reliability, exact-index accuracy, and lost-in-the-middle behavior.

### Required six-hour comparison table

The implementation/research report should produce a table with this shape:

| Provider/model | Architecture | Context/call | Calls for ~6h | Concurrency | Total input tokens | Total output tokens | Runtime | Peak local memory | API cost | Main risk |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |

Every value must be labeled as measured, officially documented, estimated, or not tested.

## Benchmark design

Use AutoClip's real request-building path and `highlight_v1.txt` prompt first. Prompt changes are separate experiments.

The benchmark set should include transcript windows containing:

- obvious strong clips,
- subtle strong clips,
- intentionally boring sections,
- context-dependent moments that should be rejected,
- strong hooks with weak payoff,
- strong payoff with weak openings,
- near-duplicate candidate moments,
- conversational filler,
- interviews/podcasts,
- educational content,
- business content,
- representative and near-maximum request sizes.

Measure at least:

- valid-response rate,
- valid-empty rate,
- false-empty rate,
- malformed-response rate,
- repair-success rate,
- invalid-index rate,
- hallucination rate,
- boundary quality,
- hook quality,
- self-containment,
- payoff detection,
- ranking quality,
- score calibration,
- latency,
- memory where local,
- token usage and cost where hosted.

Human editorial review should be used for clip quality where possible. General-purpose benchmark scores may help shortlist models but must not substitute for AutoClip-specific testing.

## Suggested implementation shape

One possible internal structure is:

```python
@dataclass
class CompletionMeta:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_duration_s: float | None = None
    effective_context: int | None = None
    request_id: str = ""


@dataclass
class WindowDiagnostic:
    first_word: int
    last_word: int
    outcome: str
    provider: str
    model: str
    attempts: int = 1
    error: str = ""
    meta: CompletionMeta | None = None


@dataclass
class DetectionSummary:
    total_windows: int
    successful_windows: int
    valid_empty_windows: int
    failed_windows: int
    failures_by_category: dict[str, int]
```

This is illustrative, not a required API. Prefer the smallest change that cleanly separates provider output, validation, and aggregate diagnostics.

## UI behavior

The default user-facing message should remain concise.

### All windows valid, zero candidates

> No clips met the current highlight criteria. All 52 transcript windows were analyzed successfully. Try lowering the editorial threshold or using a different model if you expected more candidates.

### Some or all windows failed

> Highlight analysis did not complete successfully. 18 of 52 transcript windows failed, so AutoClip cannot conclude that the video contains no usable clips.

The UI should offer technical details showing provider/model and categorized counts, without dumping transcript content.

## Logging

Add structured logs for:

- job/provider/model,
- window ordinal and word range,
- outcome category,
- retry attempt,
- elapsed time,
- safe token/timing metrics,
- sanitized error.

A six-hour job should be diagnosable from logs without rerunning transcription.

## Testing

Add tests covering at least:

1. all windows return valid empty results,
2. all windows fail,
3. mixed valid-empty and failed windows,
4. some windows return candidates while others fail,
5. malformed JSON succeeds on repair,
6. malformed JSON fails twice,
7. invalid indices are recorded as semantic failures,
8. Ollama context metadata is preserved,
9. explicit Ollama context is sent,
10. hosted unsupported-parameter failures are classified,
11. rate-limit and timeout failures are classified,
12. error summaries do not expose transcript text or keys,
13. a synthetic six-hour transcript produces the expected window count from the real window builder.

Provider tests should use mocked HTTP/API responses. Model-quality benchmarks should remain opt-in integration tests or a separate benchmark harness.

## Acceptance criteria

This feature is complete when:

- [ ] a zero-candidate job distinguishes valid-empty windows from failed windows,
- [ ] every failed window has a normalized failure category,
- [ ] the final error includes total/success/empty/failure counts,
- [ ] Ollama requests use an explicit, safe context strategy,
- [ ] Ollama token/timing metadata can be captured for benchmarking,
- [ ] official OpenAI requests are model-capability-aware for materially incompatible parameters,
- [ ] structured output is used where the selected model/provider supports it,
- [ ] invalid transcript indices are measurable instead of silently hidden,
- [ ] a reproducible benchmark can estimate total runtime/cost for a ~6-hour video,
- [ ] local recommendations exist for the requested memory/VRAM tiers,
- [ ] OpenAI recommendations include cost, context, compatibility, and expected six-hour workload,
- [ ] existing repository model recommendations are reviewed and updated only from documented evidence,
- [ ] tests cover all-empty, all-failed, mixed, context, timeout, malformed-output, and unsupported-parameter paths.

## Rollout plan

### Phase 1: diagnostics and correctness

- preserve per-window outcomes,
- aggregate failures,
- improve the final error,
- classify provider errors,
- stop hiding invalid indices.

### Phase 2: provider compatibility

- explicit Ollama context sizing,
- Ollama response metrics,
- OpenAI model capability handling,
- stronger structured output.

### Phase 3: model research and defaults

- run the AutoClip-specific benchmark,
- publish six-hour local/OpenAI workload results,
- update recommended models and hardware guidance.

### Phase 4: optional quality architecture

- benchmark the two-pass discovery + reranking design,
- adopt it only if it materially improves recall/ranking at acceptable cost and latency.

## Definition of success

After this feature ships, "No clips were found" should mean something precise.

AutoClip should be able to say either:

- the model successfully analyzed the transcript and no clips met the editorial criteria, or
- analysis was incomplete or unreliable, with enough evidence to explain whether the likely cause was context, model capability, malformed output, timeout, rate limit, or another provider failure.

The model recommendation then becomes an evidence-based optimization choice instead of the first debugging step.
