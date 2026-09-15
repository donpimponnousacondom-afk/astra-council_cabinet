"""Build a self-contained HTML report from private Featherless tester evidence."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import html
import json
from pathlib import Path
import statistics


def esc(value):
    # Preserve malformed provider code units visibly without breaking UTF-8 HTML.
    return html.escape(str(value).encode("utf-8", "backslashreplace").decode("utf-8"), quote=True)


def median(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return f"{statistics.median(values):.1f}" if values else "—"


def load(path):
    return json.loads(path.read_text())


def findings_html(text):
    """Render plain report paragraphs/headings, never arbitrary embedded HTML."""
    blocks = []
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        if len(lines) == 1 and lines[0].startswith("#"):
            level = min(4, len(lines[0]) - len(lines[0].lstrip("#")) + 1)
            blocks.append(f"<h{level}>{esc(lines[0].lstrip('# ').strip())}</h{level}>")
        elif block.startswith("```") or any(" | " in line for line in lines):
            # Keep literal tables/commands available without claiming full Markdown support.
            blocks.append(f'<pre class="source-block">{esc(block)}</pre>')
        else:
            blocks.append(f"<p>{esc(block).replace(chr(10), '<br>')}</p>")
    return "".join(blocks)


def build_report(root):
    root = Path(root).resolve()
    comparison_path = root / "comparison.json"
    comparison = load(comparison_path) if comparison_path.exists() else None
    comparison_runs = set(comparison["runs"]) if comparison else None
    comparison_description = (
        comparison.get("description", "Selected matched runs")
        if comparison
        else "All non-excluded variants; this aggregate is not a controlled ranking."
    )
    cases = [load(p) | {"_path": p} for p in sorted(root.rglob("case-result-*.json"))]
    exclusions = []
    for case in cases:
        marker = case["_path"].parent / "exclude-from-comparison.json"
        case["_excluded"] = (
            load(marker).get("reason", "Excluded exploratory run") if marker.exists() else None
        )
        if case["_excluded"]:
            item = (case["_path"].parent.name, case["_excluded"])
            if item not in exclusions:
                exclusions.append(item)
        for metric, request_path in zip(case["metrics"], case.get("requests", [])):
            if not request_path:
                continue
            request_path = Path(request_path).resolve()
            if request_path.is_relative_to(root) and request_path.exists():
                request = load(request_path)
                metric.update(
                    {
                        k: request.get(k)
                        for k in (
                            "http_status",
                            "finish_reason",
                            "error",
                            "error_origin",
                            "upstream_error_code",
                        )
                    }
                )
                if request.get("status") != "completed":
                    case["partial_content"] = request.get("content", "")
        # Old tester versions used a broad category for this explicit parser
        # rejection. Keep the original category in evidence and label its cause.
        if case["failure"] == "http_or_local" and any(
            "finish_reason" in (m.get("error") or "") for m in case["metrics"]
        ):
            case["failure"] = "response_format (missing finish_reason; original: http_or_local)"
    grouped = defaultdict(list)
    for case in cases:
        grouped[case["model"]].append(case)
    rows, detail_rows, operation_rows, variants = [], [], [], defaultdict(list)
    for model, group in sorted(grouped.items()):
        compared = [
            c
            for c in group
            if not c["_excluded"]
            and (comparison_runs is None or c["_path"].parent.relative_to(root).as_posix() in comparison_runs)
        ]
        attempts = [r for c in compared for r in c["metrics"]]
        metrics = [r for r in attempts if r.get("status") == "completed"]
        failures = Counter(c["failure"] for c in compared if not c["success"])
        reasons = [r["reasoning_tokens"] for r in attempts if r.get("reasoning_tokens") is not None]
        completions = [r["output_tokens"] for r in attempts if r.get("output_tokens") is not None]
        tool_cells = []
        for name in (
            "web_fetch",
            "web_search",
            "send_email",
            "global_memory",
            "memory",
            "remember",
            "notes",
            "annotations",
        ):
            tested = [c for c in compared if c["case"] == name]
            good = sum(c["success"] for c in tested)
            tool_cells.append(
                f'<td class="{"good" if tested and good == len(tested) else "bad" if tested and not good else "mixed"}">{good}/{len(tested)}</td>'
                if tested
                else "<td>—</td>"
            )
        rows.append(
            f"<tr><th>{esc(model)}</th>{''.join(tool_cells)}<td>{median(r.get('ttft_ms') for r in metrics)}</td><td>{median(r.get('stream_tps') for r in metrics)}</td><td>{median(r.get('e2e_tps') for r in metrics)}</td><td>{sum(reasons) if reasons else 'none'}</td><td>{sum(completions) if completions else '—'}</td><td>{sum(r.get('visible_tokens_estimate') or 0 for r in attempts)}</td><td>{esc(dict(failures))}</td></tr>"
        )
        for name in ("global_memory", "memory", "remember", "notes", "annotations"):
            selected = [c for c in compared if c["case"] == name]
            if selected:
                cells = "".join(
                    f"<td>{sum(bool(c['checks'].get(k)) for c in selected)}/{len(selected)}</td>"
                    for k in ("read", "write", "edit", "delete", "read_back", "state", "answer")
                )
                operation_rows.append(f"<tr><th>{esc(model)}</th><td>{esc(name)}</td>{cells}</tr>")
        for case in group:
            path = case["_path"]
            input_path = path.parent / f"case-input-{case['case_id']}.json"
            evidence = load(input_path) if input_path.exists() else {}
            # Early exploratory files predate the explicit schema-variant flag.
            # Identify their actual wire schema instead of trusting an old label.
            schema_label = case["schema"]
            if (
                schema_label == "runtime"
                and evidence.get("tools")
                and "anyOf" not in evidence["tools"][0]["function"]["parameters"]
            ):
                schema_label = "conditional (no usage wrapper)"
            system_layout = case.get("system_layout") or (
                "separate"
                if sum(m["role"] == "system" for m in evidence.get("messages", [])) > 1
                else "merged"
            )
            schema_label += f" / system={system_layout}"
            if not case["_excluded"]:
                key = (
                    model,
                    path.parent.name,
                    case["mode"],
                    case["guidance"],
                    schema_label,
                    json.dumps(case.get("parameters", evidence.get("parameters", {})), sort_keys=True),
                )
                variants[key].append(case)
            detail_rows.append(
                f'<tr class="case"><td>{esc(model)}<br><small>{esc(path.parent.name)}</small></td><td>{esc(case["case"])}</td><td>{esc(case["mode"])} / {esc(case["guidance"])} / {esc(schema_label)}</td>'
                f'<td class="{"good" if case["success"] else "bad"}">{"PASS" if case["success"] else esc(case["failure"])}{("<br>EXCLUDED: " + esc(case["_excluded"])) if case["_excluded"] else ""}</td>'
                f"<td>{case['argument_errors']} / {case['usage_calls']}</td>"
                f"<td><details><summary>Checks, prompts, parameters and tool trace</summary>"
                f"<h4>Checks</h4><pre>{esc(json.dumps(case['checks'], indent=2))}</pre>"
                f"<h4>Exact request inputs</h4><pre>{esc(json.dumps(evidence, ensure_ascii=False, indent=2))}</pre>"
                f"<h4>Simulated calls and results</h4><pre>{esc(json.dumps(case['calls'], ensure_ascii=False, indent=2))}</pre>"
                f"<h4>Final answer</h4><pre>{esc(case['final'])}</pre>"
                f"<h4>Unconfirmed partial visible output (never accepted as a final answer)</h4><pre>{esc(case.get('partial_content', ''))}</pre>"
                f"<h4>Per-request performance</h4><pre>{esc(json.dumps(case['metrics'], indent=2))}</pre>"
                f"<p>Raw provider responses and dedicated private reasoning fields remain in the adjacent request JSON files. This HTML omits dedicated reasoning fields. Malformed ordinary assistant content is shown as received and may itself contain reasoning markers.</p></details></td></tr>"
            )
    variant_rows = []
    for key, group in sorted(variants.items()):
        model, run, mode, guidance, schema, params = key
        attempts = [r for c in group for r in c["metrics"]]
        metrics = [r for r in attempts if r.get("status") == "completed"]
        reasons = [r["reasoning_tokens"] for r in attempts if r.get("reasoning_tokens") is not None]
        variant_rows.append(
            f"<tr><th>{esc(model)}</th><td>{esc(run)}</td><td>{esc(mode)} / {esc(guidance)} / {esc(schema)}</td>"
            f"<td>{esc(params)}</td><td>{sum(c['success'] for c in group)}/{len(group)}</td>"
            f"<td>{sum(c['argument_errors'] for c in group)}</td><td>{median(r.get('ttft_ms') for r in metrics)}</td>"
            f"<td>{median(r.get('stream_tps') for r in metrics)}</td><td>{median(r.get('e2e_tps') for r in metrics)}</td>"
            f"<td>{sum(reasons) if reasons else 'none'} ({len(reasons)}/{len(attempts)} requests measured)</td>"
            f"<td>{median(reasons) if reasons else 'none'}</td><td>{esc(dict(Counter(c['failure'] for c in group if not c['success'])))}</td></tr>"
        )
    excluded_html = (
        (
            "<section><h2>Disclosed exclusions</h2><ul>"
            + "".join(
                f"<li>{esc(run)}: {esc(reason)}. Cases remain below.</li>" for run, reason in exclusions
            )
            + "</ul></section>"
        )
        if exclusions
        else ""
    )
    manifests = []
    for path in sorted(root.rglob("run.json")):
        manifests.append(
            f"<details><summary>{esc(path.parent.name)}: exact CLI settings</summary><pre>{esc(json.dumps(load(path), ensure_ascii=False, indent=2))}</pre></details>"
        )
    notes_path = root / "findings.md"
    findings = (
        f"<section><h2>Findings</h2>{findings_html(notes_path.read_text())}</section>"
        if notes_path.exists()
        else ""
    )
    metadata_path = root / "selection.json"
    selection = (
        f"<details><summary>Model selection, popularity and availability evidence</summary><pre>{esc(json.dumps(load(metadata_path), ensure_ascii=False, indent=2))}</pre></details>"
        if metadata_path.exists()
        else ""
    )
    linked_requests = {str(Path(p).resolve()) for c in cases for p in c.get("requests", []) if p}
    standalone_rows = []
    for path in sorted(root.rglob("request-*.json")):
        if str(path.resolve()) in linked_requests:
            continue
        request = load(path)
        if "model" not in request or "status" not in request:
            continue
        evidence = {
            k: request.get(k)
            for k in (
                "model",
                "mode",
                "purpose",
                "status",
                "http_status",
                "finish_reason",
                "error",
                "error_origin",
                "elapsed_s",
                "ttft_ms",
                "stream_tps",
                "e2e_tps",
                "input_tokens",
                "output_tokens",
                "output_count_source",
                "reasoning_tokens",
                "reasoning_count_source",
                "visible_tokens_estimate",
                "content",
            )
        }
        standalone_rows.append(
            f"<tr><th>{esc(request['model'])}</th><td>{esc(path.parent.name)}</td>"
            f"<td>{esc(request.get('mode'))} / {esc(request.get('purpose'))}</td>"
            f"<td>{esc(request['status'])} / HTTP {esc(request.get('http_status'))} / finish {esc(request.get('finish_reason'))}</td>"
            f"<td><details><summary>Response metrics and visible output</summary><pre>{esc(json.dumps(evidence, ensure_ascii=False, indent=2))}</pre></details></td></tr>"
        )
    standalone = (
        "<section><h2>Standalone controls and unlinked partial attempts</h2>"
        "<p>These requests are not scored as tool workflows. They include no-tool controls and interrupted attempts retained before a case could link its response. Exact settings remain in the run manifests and private request files.</p>"
        '<details><summary>Open request records</summary><div class="scroll"><table><thead><tr><th>Model</th><th>Run</th><th>Mode / purpose</th><th>Outcome</th><th>Evidence</th></tr></thead><tbody>'
        + "".join(standalone_rows)
        + "</tbody></table></div></details></section>"
        if standalone_rows
        else ""
    )
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Featherless tool-calling benchmark</title><style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{margin:0;background:#15191f;color:#e2e8ef;font:15px/1.55 system-ui,sans-serif}}main{{max-width:1800px;margin:auto;padding:24px}}h1,h2{{color:#b5dcff}}.muted,small{{color:#b0bccb}}.panel,section{{background:#20262f;border:1px solid #364252;border-radius:8px;padding:18px;margin:18px 0}}.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;font:13px/1.45 ui-monospace,monospace}}th,td{{border:1px solid #364252;padding:8px;text-align:left;vertical-align:top}}thead{{background:#263346}}th:first-child{{min-width:260px}}.good{{color:#a5e7a4}}.bad{{color:#ffa8a8}}.mixed{{color:#f3d794}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.5 ui-monospace,monospace;max-height:700px;overflow:auto}}pre.prose{{font:inherit;max-height:none}}details{{margin:8px 0}}summary{{cursor:pointer;color:#9ed1ff}}input{{width:100%;padding:12px;background:#141b24;color:inherit;border:1px solid #60748d}}a{{color:#91caff}}@media(max-width:700px){{main{{padding:10px}}h1{{font-size:24px}}section{{padding:12px}}}}@media print{{body{{background:white;color:black}}details{{display:block}}}}
</style><main><h1>Featherless: native tool-calling study</h1><p class="muted">Generated {esc(datetime.now().astimezone().isoformat(timespec="seconds"))} · {len(grouped)} models · {len(cases)} cases · {sum(c["success"] for c in cases)} passes</p>
<div class="panel"><b>All tools are simulated.</b> No email was sent, no URL fetched by a model tool, and no council memory changed. Every memory case starts from the same JSON baseline. PASS requires native structured calls, the requested operations/state and a final answer. Text resembling a call never executes.</div>{findings}{excluded_html}
<section><h2>How to read these results</h2><ul><li>Matrix and memory-check scope: {esc(comparison_description)} Each cell is passed cases / attempted cases. All prompt, reasoning and transport variants remain in the separate variant and individual-case tables.</li><li>Memory tasks: read → write blue → replace with green → delete obsolete → read back → report green. All five aliases use identical schemas/descriptions apart from the function name.</li><li>HTTP, capacity and upstream-envelope failures are request failures, not proof the model cannot call a tool. Text-only pseudo-calls, argument errors, output-length stops and incorrect state are distinct.</li><li>TTFT is milliseconds; TPS is streaming tokens/second. E2E TPS includes full request latency and works without SSE. Timing medians use completed requests only. Token totals and reasoning medians include recorded usage/partial text from failed attempts too. Completion includes reasoning; visible text is a fixed cl100k estimate. These are not fixed-length speed tests.</li><li>Reasoning counts prefer reported usage; missing counts use returned text with fixed cl100k. none means unavailable, not zero. Native tokenizer counts and local estimates must not be treated as billing-equivalent. Per-request sources are shown in details.</li><li>Short, bounded smoke tests cannot prove universal reliability or that a failure is irreparable. Availability, warm-up, prompt wording, output cap and native thinking settings can affect results. A server-rendered template is evidence of rendering, not proof its inference/parser path is correct.</li></ul></section>
<label for="filter">Filter by model, tool, prompt variant or result</label><input id="filter" placeholder="e.g. gemma, annotations, upstream_error">
<section><h2>Failure labels</h2><ul><li><b>text_instead_of_tool:</b> no native call executed; ordinary text may contain plausible JSON arguments.</li><li><b>task_incorrect:</b> calls executed, but an explicitly requested step, exact argument, final state or answer check was missing. This does not mean the tool syntax was invalid.</li><li><b>response_format:</b> the received response did not establish a valid complete message, including missing finish_reason. Partial text and successful earlier operations remain visible.</li><li><b>upstream_http / upstream_error:</b> the server rejected the request, including error envelopes carried inside HTTP 200. These do not measure the model's ability to call tools.</li><li><b>output_limit / tool_round_budget / local_deadline:</b> the configured experiment bound was reached; a larger bound is a different experiment, not evidence that the original task completed.</li></ul></section>
<section><h2>Model/tool matrix and performance</h2><p>{esc(comparison_description)}</p><div class="scroll"><table id="matrix"><thead><tr><th>Model</th><th>Fetch</th><th>Search</th><th>Email</th><th>Global memory</th><th>Memory</th><th>Remember</th><th>Notes</th><th>Annotations</th><th>Median TTFT ms</th><th>Median TPS</th><th>Median E2E TPS</th><th>Reasoning tokens Σ</th><th>Completion tokens Σ</th><th>Visible estimate Σ</th><th>Failures</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>
<section><h2>Individual cases: exact prompts and results</h2><details id="case-details"><summary>Open all {len(cases)} case records (also opens when filtering)</summary><div class="scroll"><table id="cases"><thead><tr><th>Model/run</th><th>Tool</th><th>Mode / guidance / schema</th><th>Outcome</th><th>Errors / help calls</th><th>Evidence</th></tr></thead><tbody>{"".join(detail_rows)}</tbody></table></div></details></section>
<section><h2>Controlled variants: compare matching settings</h2><div class="scroll"><table><thead><tr><th>Model</th><th>Run</th><th>Mode / guidance / schema</th><th>Native parameters</th><th>Passes / cases</th><th>Rejected calls</th><th>TTFT ms</th><th>TPS</th><th>E2E TPS</th><th>Reasoning Σ / availability</th><th>Median reasoning / request</th><th>Failures</th></tr></thead><tbody>{"".join(variant_rows)}</tbody></table></div></section>
<section><h2>Memory workflow checks independently of final delivery</h2><p>A model can complete every note operation and still fail final-response validation. Counts use the same selected scope as the matrix and show steps satisfied in the requested order / attempted cases. An operation performed out of order may fail its step check even when the final notebook is correct; inspect the native call trace. Unconfirmed final text is not accepted.</p><div class="scroll"><table><thead><tr><th>Model</th><th>Alias</th><th>Read</th><th>Write</th><th>Replace</th><th>Delete</th><th>Read back</th><th>Exact state</th><th>Final answer</th></tr></thead><tbody>{"".join(operation_rows)}</tbody></table></div></section>
{standalone}<section><h2>Reproduction and selection</h2>{selection}{"".join(manifests)}</section>
<section><h2>Documentation consulted</h2><p><a href="https://featherless.ai/docs/api-reference-models">Models, popularity and availability</a> · <a href="https://featherless.ai/docs/chat-template-kwargs">Thinking and template controls</a> · <a href="https://featherless.ai/docs/tool-calling">Tool calling</a> · <a href="https://featherless.ai/docs/api-reference-error-codes">Error codes</a>. Live observations in this report are backed by the adjacent recorded responses.</p></section></main>
<script>document.querySelector('#filter').addEventListener('input',e=>{{const q=e.target.value.toLowerCase();document.querySelector('#case-details').open=Boolean(q);document.querySelectorAll('tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q));}});</script></html>"""
    path = root / "report.html"
    path.write_text(document)
    path.chmod(0o600)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    print(build_report(parser.parse_args().directory))
