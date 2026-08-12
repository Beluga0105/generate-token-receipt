---
name: generate-token-receipt
description: Generate auditable token usage receipts as self-contained HTML with a JSON sidecar and USD token-cost estimate. Query the current Codex conversation, previous completed turns, one exact prior turn, or all locally logged sessions in a project folder. Price mixed-model Codex usage per model and provide a clearly labeled reference-model approximation when an internal model lacks an exact rate. Supports a monochrome luxury-retail-inspired 80 mm receipt and a restrained Apple-inspired A4 Codex invoice. Use when a user asks for a token bill, token cost breakdown, current Codex task receipt, OpenAI API usage receipt, 账单, token 小票, 本次用了多少 token, 上一轮或前两轮用了多少 token, 当前对话总计, 项目文件夹 token 总量, 折算美元, 费用明细, Apple-like invoice styling, a Codex-branded receipt, or an electronic receipt that may later be printed.
---

# Generate Token Receipt

Create an itemized electronic receipt from exact usage telemetry when available. Keep token facts, price assumptions, and actual billing status separate.

## Choose the paper

- Use `--paper 80mm` for the monochrome boutique-style Codex receipt. This is the default when no paper option is passed.
- Use `--paper a4` for the restrained Apple-inspired electronic invoice.
- Treat both layouts as locally generated usage documents, never as official Apple, OpenAI, luxury-brand, or tax invoices.
- Keep the brand lockup to the bundled Codex logo and the Codex text label only. Do not add a subtitle beneath the label or explanatory copy beneath the receipt title; keep billing-status disclosures in the Important information section and footer.
- `--style codex-invoice` remains as a compatibility flag. No other style is supported.
- When generating both paper sizes for the same usage, create the audit record once, then use `--receipt-json` for the additional view. Paper must not change the receipt ID, token facts, pricing, or checksum.

## Choose the usage source

Use the first matching source:

1. For the active Codex conversation through the issuance cutoff, run with `--codex-current`.
2. For the previous `N` completed user turns combined, run with `--codex-last-turns N`. Interpret “上一轮” as `N=1` and “前两轮” or “最近两轮” as `N=2`.
3. For exactly one earlier completed turn, run with `--codex-turn N`. `--codex-turn 1` means the immediately previous completed turn; `--codex-turn 2` means only the turn two rounds back.
4. For all locally logged Codex model calls whose recorded working directory is a project folder or one of its descendants, run with `--codex-project /absolute/path/to/project`.
5. For an OpenAI API response, run with `--usage-json /absolute/path/response.json`. Accept Responses and Chat Completions usage shapes.
6. For known manual counts, pass `--model`, `--input-tokens`, and `--output-tokens`; add cached, cache-write, and reasoning counts when known. Add `--manual-exact` only when the user confirms the counts were copied exactly from an authoritative usage record.
7. If exact usage is unavailable, do not estimate token counts from prose. State what is missing and ask for a usage object or manual counts.

Codex queries read local JSONL telemetry fields and boundary metadata only; they do not copy prompts or responses into the receipt. Raw session and turn UUIDs are redacted by default. Keep them redacted unless the user explicitly needs local forensic correlation. API request IDs and the supplied usage filename are also redacted by default; pass `--include-source-metadata` only when the user explicitly requests those identifiers.

Turn queries use completed root-turn boundaries, so the active turn is excluded. Add `--include-subagents` only when a conversation or turn receipt should cover delegated work. Discover descendants across all locally retained session and archived-session logs. For selected turns, descendant usage is attributed by token-event timestamps inside the selected root-turn window because local child logs do not carry a parent-turn ID.

Project queries include matching calls from root and subagent sessions automatically. They reflect only local logs still present on this device; remote, deleted, or unlogged activity is outside the result. The receipt stores the project folder name and a path fingerprint, never the absolute project path.

Issue any live Codex telemetry receipt as the last substantive tool action. State that its cutoff is the printed `USAGE THROUGH` timestamp, so the invocation call and short delivery message after telemetry collection can be excluded. For completed-turn scopes, also state that the active turn is excluded.

Do not invent or display a task label. Omit `--task-label` unless the user explicitly asks for one on the receipt.

## Apply pricing safely

- Treat Codex or ChatGPT subscription telemetry as an **API-equivalent token estimate**, never as an invoice or actual deduction.
- Use the embedded exact-model snapshots for `gpt-5.6-luna`, `gpt-5.6-terra`, and `gpt-5.6-sol`. Their 2026-08-13 official text rates are respectively `$0.20/$0.02/$1.20`, `$2.00/$0.20/$12.00`, and `$5.00/$0.50/$30.00` per million fresh-input/cached-input/output tokens; cache writes are 1.25x fresh input.
- Price mixed-model telemetry call by call with each exact model's rate card. Display effective blended rates in the compact category table, and retain every exact model rate card in `pricing.model_rate_cards`.
- For a locally queried Codex model without an embedded exact rate, use `gpt-5.6-terra` only as a reference-model approximation. Mark the status `approximate_api_equivalent_estimate`, name every unmatched model and the reference model in the audit JSON and visible warning, and still keep actual subscription charge unavailable.
- Do not apply that automatic reference estimate to API-response or manual sources. For those unknown models, verify the exact current model page and pass all four rates plus `--pricing-as-of` and `--pricing-source`.
- Do not silently map aliases, service tiers, Batch/Flex/Priority traffic, regions, or unknown modalities to a convenient rate.
- When passing explicit rates, always include both `--pricing-as-of` and an HTTP(S) `--pricing-source`; the generator rejects incomplete provenance.
- Subtract cached reads and cache writes from total input before pricing fresh input.
- Treat reasoning tokens as a subset of output tokens. Show the subset, but never bill it twice.
- Preserve unknown values as unknown. Do not turn unavailable usage or actual charges into zero.
- Keep full Decimal precision in the audit JSON and sum before rounding. Display rates, USD amounts, and percentages with two decimal places; render a nonzero sub-cent value as `<$0.01` instead of `$0.00`.
- Label tool fees, taxes, credits, subscriptions, and other non-token charges as excluded unless exact values were supplied.
- Keep session identifiers and API source metadata redacted for ordinary delivery. Use `--include-session-ids` or `--include-source-metadata` only when the user explicitly needs local forensic correlation and understands the privacy tradeoff.
- Treat the HTML and JSON as equally sensitive: every HTML file embeds the full normalized audit record, including per-call metadata, for portable inspection.

Read [references/receipt-contract.md](references/receipt-contract.md) when adding a new usage shape, pricing modifier, output renderer, or printer integration.

## Generate the receipt

Resolve this Skill's directory as `SKILL_DIR`, then use one of these patterns.

Current Codex task, including delegated agents:

```bash
python3 "$SKILL_DIR/scripts/generate_receipt.py" \
  --codex-current \
  --include-subagents \
  --output "/absolute/path/token-receipt.html"
```

Previous two completed turns combined:

```bash
python3 "$SKILL_DIR/scripts/generate_receipt.py" \
  --codex-last-turns 2 \
  --include-subagents \
  --output "/absolute/path/previous-two-turns.html"
```

Only the turn two rounds back:

```bash
python3 "$SKILL_DIR/scripts/generate_receipt.py" \
  --codex-turn 2 \
  --output "/absolute/path/turn-two-back.html"
```

All locally logged usage for a project folder:

```bash
python3 "$SKILL_DIR/scripts/generate_receipt.py" \
  --codex-project "/absolute/path/to/project" \
  --output "/absolute/path/project-total.html"
```

Responses or Chat Completions JSON:

```bash
python3 "$SKILL_DIR/scripts/generate_receipt.py" \
  --usage-json "/absolute/path/response.json" \
  --output "/absolute/path/token-receipt.html"
```

Manual counts with a verified external rate card:

```bash
python3 "$SKILL_DIR/scripts/generate_receipt.py" \
  --model "exact-model-id" \
  --input-tokens 125000 \
  --cached-input-tokens 80000 \
  --output-tokens 4200 \
  --reasoning-tokens 1800 \
  --manual-exact \
  --input-rate 5 \
  --cached-input-rate 0.5 \
  --cache-write-input-rate 6.25 \
  --output-rate 30 \
  --pricing-as-of "YYYY-MM-DD" \
  --pricing-source "https://example.com/exact-model-rate-card" \
  --output "/absolute/path/token-receipt.html"
```

The command writes a self-contained HTML receipt and, unless `--no-json` is used, a same-name `.json` audit sidecar. Use absolute output paths and keep both files together.

Render alternate views from an existing audit sidecar without recollecting or repricing usage:

```bash
python3 "$SKILL_DIR/scripts/generate_receipt.py" \
  --receipt-json "/absolute/path/token-receipt.json" \
  --paper 80mm \
  --output "/absolute/path/codex-invoice-80mm.html"

python3 "$SKILL_DIR/scripts/generate_receipt.py" \
  --receipt-json "/absolute/path/token-receipt.json" \
  --paper a4 \
  --output "/absolute/path/codex-invoice-a4.html"
```

## Create PDF and preview files

When the user wants a durable electronic copy, render the HTML with:

```bash
python3 "$SKILL_DIR/scripts/render_receipt.py" \
  "/absolute/path/token-receipt.html" \
  --pdf "/absolute/path/token-receipt.pdf" \
  --png "/absolute/path/token-receipt-preview.png"
```

Do not send anything to a physical printer. A future printer renderer must consume the JSON sidecar and require an explicit print action.

## Verify before delivery

1. Reopen the JSON and verify `input_tokens >= cached_input_tokens + cache_write_input_tokens`.
2. Verify `total_tokens = input_tokens + output_tokens` when all three values are known.
3. Confirm reasoning is informational and not an extra charged line.
4. Confirm the model, rates, rate date, source URL, estimate label, and excluded charges are visible.
5. For mixed-model usage, confirm every observed model has a `pricing.model_rate_cards` entry and that exact matches remain distinct. If any fallback is used, confirm the visible status says `Approx.` and the warning names both the unmatched model and `gpt-5.6-terra`.
6. Render the latest PDF to PNG and inspect it for clipping, overflow, broken Chinese glyphs, weak contrast, or extra blank pages.
7. Confirm every HTML layout embeds exactly one local Codex logo and does not load remote assets.
8. For multiple layouts, compare their embedded JSON and confirm it is identical.
9. Confirm `source.query_scope`, `source.scope_label`, matched turn count, and matched session count describe the user's requested range. For a project query, confirm the absolute project path and raw UUIDs do not appear in HTML or JSON. For an API usage object, confirm the request ID and input filename are absent unless explicitly requested.
10. Report the requested scope and receipt cutoff time, then deliver the HTML, JSON, and requested PDF exactly once each.
