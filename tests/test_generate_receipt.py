from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "generate_receipt.py"
RENDERER = ROOT / "scripts" / "render_receipt.py"


def run_generator(
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GENERATOR), *args],
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def embedded_record(path: Path) -> dict[str, object]:
    content = path.read_text(encoding="utf-8")
    match = re.search(
        r'<script type="application/json" id="receipt-data">(.*?)</script>',
        content,
        flags=re.DOTALL,
    )
    if not match:
        raise AssertionError(f"embedded audit record missing from {path}")
    return json.loads(match.group(1))


class ReceiptGeneratorTests(unittest.TestCase):
    def test_manual_record_and_rerender_are_identical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            receipt_80mm = target / "sample-80mm.html"
            receipt_a4 = target / "sample-a4.html"
            run_generator(
                "--model",
                "demo-model-001",
                "--input-tokens",
                "128400",
                "--cached-input-tokens",
                "86400",
                "--cache-write-input-tokens",
                "12000",
                "--output-tokens",
                "9400",
                "--reasoning-tokens",
                "3600",
                "--input-rate",
                "2",
                "--cached-input-rate",
                "0.2",
                "--cache-write-input-rate",
                "2.5",
                "--output-rate",
                "12",
                "--pricing-as-of",
                "sample-only",
                "--pricing-source",
                "https://example.invalid/sample-rate-card",
                "--served-at",
                "2026-01-01T08:00:00+08:00",
                "--paper",
                "80mm",
                "--output",
                str(receipt_80mm),
            )
            audit_path = receipt_80mm.with_suffix(".json")
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(audit["usage"]["total_tokens"], 137800)
            self.assertFalse(audit["source"]["usage_is_exact"])
            self.assertIsNone(audit["source"]["session_id"])
            self.assertIsNone(audit["source"]["request_id"])
            self.assertEqual(audit["computed"]["known_token_subtotal_usd"], "0.22008")
            output_amount = sum(
                float(audit["computed"]["category_amounts_usd"][key])
                for key in ("visible_output", "reasoning_output")
            )
            self.assertAlmostEqual(output_amount, 0.1128)

            run_generator(
                "--receipt-json",
                str(audit_path),
                "--paper",
                "a4",
                "--output",
                str(receipt_a4),
                "--no-json",
            )
            self.assertEqual(embedded_record(receipt_80mm), embedded_record(receipt_a4))
            for html_path in (receipt_80mm, receipt_a4):
                content = html_path.read_text(encoding="utf-8")
                self.assertIn("data:image/svg+xml;base64,", content)
                self.assertNotRegex(content, r'<(?:script|img|link)[^>]+(?:src|href)="https?://')

    def test_subcent_values_keep_full_decimal_precision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "subcent.html"
            run_generator(
                "--model",
                "gpt-5.6-luna",
                "--input-tokens",
                "1",
                "--output-tokens",
                "0",
                "--manual-exact",
                "--served-at",
                "2026-01-01T00:00:00+00:00",
                "--output",
                str(receipt),
            )
            audit = json.loads(receipt.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(audit["computed"]["category_amounts_usd"]["fresh_input"], "0.0000002")
            self.assertEqual(audit["computed"]["known_token_subtotal_usd"], "0.0000002")
            self.assertIn("&lt;$0.01", receipt.read_text(encoding="utf-8"))

    def test_invalid_token_relationship_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_generator(
                "--model",
                "demo-model-001",
                "--input-tokens",
                "10",
                "--cached-input-tokens",
                "11",
                "--output",
                str(Path(directory) / "invalid.html"),
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("cannot exceed input_tokens", result.stderr)

    def test_usage_json_requires_integer_tokens_and_redacts_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            invalid_usage = target / "invalid-private-usage.json"
            invalid_usage.write_text(
                json.dumps(
                    {
                        "id": "resp_private_invalid",
                        "model": "gpt-5.6-sol",
                        "usage": {"input_tokens": 1.5, "output_tokens": 1},
                    }
                ),
                encoding="utf-8",
            )
            invalid = run_generator(
                "--usage-json",
                str(invalid_usage),
                "--output",
                str(target / "invalid.html"),
                check=False,
            )
            self.assertEqual(invalid.returncode, 1)
            self.assertIn("must be a nonnegative integer", invalid.stderr)

            usage_path = target / "private-client-usage.json"
            usage_path.write_text(
                json.dumps(
                    {
                        "id": "resp_private_123",
                        "model": "gpt-5.6-sol",
                        "service_tier": "priority",
                        "usage": {
                            "input_tokens": 12500,
                            "input_tokens_details": {"cached_tokens": 8000},
                            "output_tokens": 1250,
                            "output_tokens_details": {"reasoning_tokens": 450},
                            "total_tokens": 13750,
                        },
                    }
                ),
                encoding="utf-8",
            )
            redacted_html = target / "redacted.html"
            run_generator(
                "--usage-json",
                str(usage_path),
                "--output",
                str(redacted_html),
            )
            redacted = json.loads(redacted_html.with_suffix(".json").read_text())
            self.assertEqual(redacted["source"]["kind"], "responses")
            self.assertIsNone(redacted["source"]["request_id"])
            self.assertTrue(redacted["source"]["source_metadata_redacted"])
            self.assertNotIn("input_file", redacted["source"])
            self.assertEqual(redacted["computed"]["visible_output_tokens"], 800)
            self.assertEqual(redacted["computed"]["known_token_subtotal_usd"], "0.0640")
            self.assertTrue(
                any("Service tier priority" in item for item in redacted["computed"]["warnings"])
            )
            redacted_text = redacted_html.read_text(encoding="utf-8")
            self.assertNotIn("resp_private_123", redacted_text)
            self.assertNotIn(usage_path.name, redacted_text)

            retained_html = target / "retained.html"
            run_generator(
                "--usage-json",
                str(usage_path),
                "--include-source-metadata",
                "--output",
                str(retained_html),
            )
            retained = json.loads(retained_html.with_suffix(".json").read_text())
            self.assertEqual(retained["source"]["request_id"], "resp_private_123")
            self.assertFalse(retained["source"]["source_metadata_redacted"])
            self.assertEqual(retained["source"]["input_file"], usage_path.name)

    def test_chat_completions_usage_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            usage_path = target / "chat.json"
            usage_path.write_text(
                json.dumps(
                    {
                        "model": "gpt-5.6-terra",
                        "usage": {
                            "prompt_tokens": 1000,
                            "prompt_tokens_details": {"cached_tokens": 600},
                            "completion_tokens": 120,
                            "completion_tokens_details": {"reasoning_tokens": 20},
                            "total_tokens": 1120,
                        },
                    }
                ),
                encoding="utf-8",
            )
            receipt = target / "chat.html"
            run_generator("--usage-json", str(usage_path), "--output", str(receipt))
            audit = json.loads(receipt.with_suffix(".json").read_text())
            self.assertEqual(audit["source"]["kind"], "chat_completions")
            self.assertEqual(audit["computed"]["fresh_input_tokens"], 400)
            self.assertEqual(audit["computed"]["visible_output_tokens"], 100)

    def test_nonfinite_rates_and_missing_provenance_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            common = (
                "--model",
                "demo-model-001",
                "--input-tokens",
                "10",
                "--cached-input-rate",
                "0",
                "--cache-write-input-rate",
                "0",
                "--output-rate",
                "0",
                "--pricing-as-of",
                "test",
                "--pricing-source",
                "https://example.invalid/rates",
            )
            for value in ("NaN", "Infinity", "-Infinity"):
                with self.subTest(value=value):
                    result = run_generator(
                        *common,
                        f"--input-rate={value}",
                        "--output",
                        str(target / f"{value}.html"),
                        check=False,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("finite decimal", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)

            missing = run_generator(
                "--model",
                "demo-model-001",
                "--input-tokens",
                "10",
                "--input-rate",
                "1",
                "--cached-input-rate",
                "0.1",
                "--cache-write-input-rate",
                "1.25",
                "--output-rate",
                "2",
                "--output",
                str(target / "missing-provenance.html"),
                check=False,
            )
            self.assertEqual(missing.returncode, 1)
            self.assertIn("requires --pricing-as-of and --pricing-source", missing.stderr)

    def test_custom_title_and_task_label_are_visible_and_escaped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "custom.html"
            run_generator(
                "--model",
                "gpt-5.6-luna",
                "--input-tokens",
                "100",
                "--manual-exact",
                "--title",
                "<Usage & Cost>",
                "--task-label",
                "Client <demo>",
                "--output",
                str(receipt),
            )
            content = receipt.read_text(encoding="utf-8")
            visible_content = content.split(
                '<script type="application/json" id="receipt-data">', 1
            )[0]
            self.assertIn("&lt;Usage &amp; Cost&gt;", content)
            self.assertIn("Client &lt;demo&gt;", content)
            self.assertNotIn("<Usage & Cost>", visible_content)
            self.assertNotIn("Client <demo>", visible_content)

    def test_long_context_modifier_starts_above_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            records = []
            for input_tokens in (272000, 272001):
                receipt = target / f"long-{input_tokens}.html"
                run_generator(
                    "--model",
                    "gpt-5.6-sol",
                    "--input-tokens",
                    str(input_tokens),
                    "--output-tokens",
                    "1000",
                    "--manual-exact",
                    "--output",
                    str(receipt),
                )
                records.append(json.loads(receipt.with_suffix(".json").read_text()))
            self.assertEqual(records[0]["computed"]["long_context_calls"], 0)
            self.assertEqual(records[0]["computed"]["known_token_subtotal_usd"], "1.39")
            self.assertEqual(records[1]["computed"]["long_context_calls"], 1)
            self.assertEqual(records[1]["computed"]["known_token_subtotal_usd"], "2.76501")

    def test_cross_date_subagent_discovery_mixed_pricing_and_project_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            codex_home = target / "codex-home"
            project = target / "private-project"
            project.mkdir()
            root_id = "11111111-1111-4111-8111-111111111111"
            child_id = "22222222-2222-4222-8222-222222222222"
            root_log = codex_home / "sessions/2026/01/01" / f"rollout-2026-01-01T23-59-00-{root_id}.jsonl"
            child_log = codex_home / "sessions/2026/01/02" / f"rollout-2026-01-02T00-01-00-{child_id}.jsonl"
            root_log.parent.mkdir(parents=True)
            child_log.parent.mkdir(parents=True)
            root_records = [
                {
                    "timestamp": "2026-01-01T23:59:00Z",
                    "type": "session_meta",
                    "payload": {"id": root_id, "cwd": str(project), "source": "cli"},
                },
                {
                    "timestamp": "2026-01-01T23:59:01Z",
                    "type": "turn_context",
                    "payload": {
                        "turn_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "model": "gpt-5.6-sol",
                        "effort": "high",
                        "cwd": str(project),
                    },
                },
                {
                    "timestamp": "2026-01-01T23:59:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 1000,
                                "cached_input_tokens": 0,
                                "cache_write_input_tokens": 0,
                                "output_tokens": 100,
                                "reasoning_output_tokens": 25,
                            }
                        },
                    },
                },
            ]
            child_records = [
                {
                    "timestamp": "2026-01-02T00:01:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": child_id,
                        "cwd": str(project),
                        "source": {
                            "subagent": {
                                "thread_spawn": {"parent_thread_id": root_id}
                            }
                        },
                    },
                },
                {
                    "timestamp": "2026-01-02T00:01:01Z",
                    "type": "inter_agent_communication_metadata",
                    "payload": {},
                },
                {
                    "timestamp": "2026-01-02T00:01:02Z",
                    "type": "turn_context",
                    "payload": {
                        "turn_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                        "model": "internal-demo-model",
                        "effort": "medium",
                        "cwd": str(project),
                    },
                },
                {
                    "timestamp": "2026-01-02T00:01:03Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 500,
                                "cached_input_tokens": 100,
                                "cache_write_input_tokens": 0,
                                "output_tokens": 50,
                                "reasoning_output_tokens": 10,
                            }
                        },
                    },
                },
            ]
            for path, records in ((root_log, root_records), (child_log, child_records)):
                path.write_text(
                    "".join(json.dumps(record) + "\n" for record in records),
                    encoding="utf-8",
                )
            env = {"CODEX_HOME": str(codex_home), "CODEX_THREAD_ID": root_id}
            conversation = target / "conversation.html"
            run_generator(
                "--codex-current",
                "--include-subagents",
                "--output",
                str(conversation),
                env=env,
            )
            audit = json.loads(conversation.with_suffix(".json").read_text())
            self.assertEqual(audit["source"]["scope_session_count"], 2)
            self.assertEqual(audit["source"]["subagent_sessions"], 1)
            self.assertEqual(audit["source"]["session_id"], "main")
            self.assertEqual(
                audit["pricing"]["status"], "approximate_api_equivalent_estimate"
            )
            cards = {item["observed_model"]: item for item in audit["pricing"]["model_rate_cards"]}
            self.assertTrue(cards["gpt-5.6-sol"]["exact_match"])
            self.assertFalse(cards["internal-demo-model"]["exact_match"])
            self.assertEqual(cards["internal-demo-model"]["pricing_model"], "gpt-5.6-terra")
            conversation_text = conversation.read_text(encoding="utf-8")
            self.assertNotIn(root_id, conversation_text)
            self.assertNotIn(child_id, conversation_text)

            project_receipt = target / "project.html"
            run_generator(
                "--codex-project",
                str(project),
                "--output",
                str(project_receipt),
                env=env,
            )
            project_text = project_receipt.read_text(encoding="utf-8")
            project_audit = json.loads(project_receipt.with_suffix(".json").read_text())
            self.assertNotIn(str(project), project_text)
            self.assertEqual(project_audit["source"]["project_name"], project.name)
            self.assertEqual(len(project_audit["source"]["project_path_hash"]), 16)

    def test_tampered_sidecar_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            receipt = target / "original.html"
            run_generator(
                "--model",
                "gpt-5.6-sol",
                "--input-tokens",
                "1000",
                "--output-tokens",
                "100",
                "--manual-exact",
                "--output",
                str(receipt),
            )
            audit_path = receipt.with_suffix(".json")
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            audit["computed"]["known_token_subtotal_usd"] = "999"
            audit_path.write_text(json.dumps(audit), encoding="utf-8")
            result = run_generator(
                "--receipt-json",
                str(audit_path),
                "--output",
                str(target / "tampered.html"),
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("checksum does not match", result.stderr)


class ReleaseIntegrityTests(unittest.TestCase):
    def test_renderer_documents_safe_80mm_viewport(self) -> None:
        result = subprocess.run(
            [sys.executable, str(RENDERER), "--help"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertRegex(
            result.stdout,
            r"at least 500 for\s+complete 80 mm previews",
        )

    def test_skill_metadata_and_release_assets(self) -> None:
        skill_text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill_text.startswith("---\n"))
        frontmatter = skill_text.split("---\n", 2)[1]
        self.assertRegex(frontmatter, r"(?m)^name: generate-token-receipt$")
        self.assertRegex(frontmatter, r"(?m)^description: .+")

        english = (ROOT / "README.md").read_text(encoding="utf-8")
        chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        expected_sizes = {
            "docs/images/sample-receipt-80mm.png": (500, 940),
            "docs/images/sample-receipt-a4.png": (900, 1220),
            "docs/images/sample-receipt-80mm-readme.png": (900, 1220),
        }
        for relative, expected_size in expected_sizes.items():
            self.assertIn(relative, english)
            self.assertIn(relative, chinese)
            image = ROOT / relative
            self.assertTrue(image.is_file())
            image_bytes = image.read_bytes()
            self.assertEqual(image_bytes[:8], b"\x89PNG\r\n\x1a\n")
            actual_size = (
                int.from_bytes(image_bytes[16:20], "big"),
                int.from_bytes(image_bytes[20:24], "big"),
            )
            self.assertEqual(actual_size, expected_size)

        self.assertIn('width="50%"', english)
        self.assertEqual(english.count('width="285"'), 2)
        self.assertIn('width="50%"', chinese)
        self.assertEqual(chinese.count('width="285"'), 2)


if __name__ == "__main__":
    unittest.main()
