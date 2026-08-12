# Receipt contract

Read this file only when extending ingestion, pricing, rendering, or printing.

## Normalized record

Keep a device-independent JSON record with these sections:

```json
{
  "schema_version": "1.0",
  "receipt": {
    "id": "RCP-...",
    "generated_at": "ISO-8601",
    "title": "Token Usage Receipt",
    "task_label": "optional, escaped in HTML"
  },
  "source": {
    "kind": "codex|responses|chat_completions|manual",
    "session_id": "redacted reference or null by default",
    "request_id": null,
    "model": "exact model ID",
    "reasoning_effort": null,
    "service_tier": null,
    "usage_cutoff": "ISO-8601 timestamp of last included telemetry event",
    "usage_is_exact": true,
    "query_scope": "current_conversation|previous_turns|turn_offset|project|supplied_usage|manual",
    "scope_label": "human-readable selected range",
    "scope_turn_count": 0,
    "scope_session_count": 0,
    "coverage": "plain-language local coverage boundary"
  },
  "usage": {
    "input_tokens": 0,
    "cached_input_tokens": 0,
    "cache_write_input_tokens": 0,
    "output_tokens": 0,
    "reasoning_output_tokens": 0,
    "total_tokens": 0,
    "model_calls": 0,
    "subagent_sessions": 0
  },
  "pricing": {
    "status": "api_equivalent_estimate|approximate_api_equivalent_estimate|estimated|approximate_estimate|unavailable|actual",
    "currency": "USD",
    "unit_tokens": 1000000,
    "as_of": "YYYY-MM-DD",
    "source_url": "https://...",
    "rates_usd_per_million": {
      "fresh_input": null,
      "cached_input": null,
      "cache_write_input": null,
      "output": null
    },
    "model_rate_cards": [
      {
        "observed_model": "exact telemetry model",
        "pricing_model": "rate-card model",
        "exact_match": true,
        "source_url": "https://...",
        "rates_usd_per_million": {}
      }
    ],
    "approximation": null,
    "modifiers": []
  },
  "computed": {
    "fresh_input_tokens": 0,
    "visible_output_tokens": 0,
    "known_token_subtotal_usd": null,
    "cache_savings_usd": null,
    "warnings": []
  },
  "calls": []
}
```

Use `null` for unknown values. Retain per-call normalized usage in `calls` when request-level modifiers may apply.

## Ingestion mappings

- Responses: `usage.input_tokens`, `usage.input_tokens_details.cached_tokens`, optional cache-write detail, `usage.output_tokens`, and `usage.output_tokens_details.reasoning_tokens`.
- Chat Completions: `usage.prompt_tokens`, `usage.prompt_tokens_details.cached_tokens`, `usage.completion_tokens`, and `usage.completion_tokens_details.reasoning_tokens`.
- Codex rollout: use each `event_msg.token_count.info.last_token_usage` after the target session's own `session_meta`. Never sum forked `total_token_usage` records across subagents.
- Manual: require an exact model ID and nonnegative integers. Reject cached plus cache-write counts larger than input. Set `usage_is_exact` only when the caller explicitly attests this with `--manual-exact`.

Reasoning is an output detail. Compute visible output as `output - reasoning`, but price output only once.

For mixed-model usage, select a rate profile for every call before aggregation. Sum full-precision per-call category amounts, then derive the displayed effective blended rate as `category amount * 1,000,000 / category tokens`. Keep the original per-model cards in `pricing.model_rate_cards`; a blended display rate must not replace the audit inputs.

## Codex query scopes

- `current_conversation`: include normalized model-call deltas in the active root session through the collection cutoff. The active turn can be partially represented because only already-emitted token events exist at the cutoff.
- `previous_turns`: select the last `N` root turns that have both a `turn_context` start and `task_complete` boundary, then combine them. Exclude the active or aborted turn.
- `turn_offset`: select exactly one completed root turn counted backward, where offset 1 is the immediately previous completed turn.
- `project`: include every locally available model-call delta whose active `session_meta.cwd` or `turn_context.cwd` is the chosen folder or a descendant. Include matching root and subagent sessions without summing the same call twice.

For selected-turn queries, filter root calls by `turn_id`. Local child-session metadata identifies its parent session but does not identify a parent turn, so include descendant token events only when their timestamps lie inside a selected root-turn window and disclose this rule.

Project totals are local-log totals, not organization-wide metering. State that remote, deleted, expired, corrupt, or unlogged sessions are unavailable. Do not crawl project contents or infer tokens from files.

Do not serialize prompts, responses, API keys, or absolute project paths. Replace raw session and turn UUIDs with stable receipt-local aliases by default. Store only a project basename and a short SHA-256 path fingerprint; the fingerprint is stable and can link receipts generated for the same local path. Retain raw session IDs only after an explicit `--include-session-ids` request. Redact API request IDs and the supplied input filename unless `--include-source-metadata` is explicitly requested.

The self-contained HTML embeds the complete normalized record in an `application/json` script element. Apply the same sharing review to HTML and JSON because both contain the per-call audit metadata.

The official usage object documents input, cached-input, output, reasoning-output, and total fields: <https://developers.openai.com/api/reference/resources/batches>.

## Pricing rules

Calculate with decimal arithmetic:

```text
fresh input = input - cached read - cache write
token subtotal =
  fresh input * fresh input rate / 1,000,000
  + cached read * cached rate / 1,000,000
  + cache write * cache-write rate / 1,000,000
  + output * output rate / 1,000,000
```

Apply request-level modifiers before aggregation. The bundled snapshots verified on 2026-08-13 are:

- `gpt-5.6-luna`: USD 0.20 input, 0.02 cached input, and 1.20 output per million tokens. Source: <https://developers.openai.com/api/docs/models/gpt-5.6-luna>.
- `gpt-5.6-terra`: USD 2.00 input, 0.20 cached input, and 12.00 output per million tokens. Source: <https://developers.openai.com/api/docs/models/gpt-5.6-terra>.
- `gpt-5.6-sol`: USD 5.00 input, 0.50 cached input, and 30.00 output per million tokens. Source: <https://developers.openai.com/api/docs/models/gpt-5.6-sol>.

All three pages state that prompts above 272,000 input tokens use 2x input and 1.5x output for the full request, and cache writes use 1.25x the uncached input rate.

For local Codex telemetry only, if an observed model has no exact bundled card, calculate a rough reference estimate with the `gpt-5.6-terra` snapshot instead of returning an unavailable subtotal. Set `exact_match` to false, retain both observed and pricing model IDs, mark the entire subtotal approximate, and show a visible warning. Never describe the fallback subtotal as an exact rate, actual charge, or invoice. Keep unknown API-response and manual sources unavailable until explicit rates are supplied.

Call this a **known token subtotal** when tools or other fee dimensions are unknown. Keep actual charged amount unavailable for Codex subscription tasks.

Retain full Decimal calculation strings in JSON. Round only presentation values with `ROUND_HALF_UP`: rates, USD amounts, and percentages use two decimal places. Display a nonzero amount below USD 0.01 as `<$0.01` rather than `$0.00`.

## Output and print boundary

Keep HTML self-contained with no remote fonts, scripts, or images. Use print CSS for 80 mm paper, black ink, zero page margin, tabular numerals, and `break-inside: avoid` on logical sections.

Keep presentation choices outside the normalized record. The renderer produces a monochrome luxury-retail-inspired 80 mm slip or an Apple-inspired A4 document and embeds the repository's original receipt mark as a data URI. Re-render an existing sidecar rather than rebuilding the record so every paper size retains the same receipt ID, usage, pricing, and checksum. Label both layouts as locally generated usage documents, not official OpenAI, Apple, luxury-brand, or tax invoices.

Treat the JSON as the future printer interface. Add an ESC/POS renderer later with an explicit profile containing `paper_mm`, `chars_per_line`, `dpi`, `codepage`, `feed`, and `cut`. Prefer a one-bit raster path for reliable Chinese output. Require an explicit print command and device selection; receipt generation alone must never print.
