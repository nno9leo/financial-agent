import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiClient, BackendNotRunningError, BackendToolError } from "../client.ts";
import { registerFinancialResearch } from "../index.ts";
import { setup, backendError, jsonResponse, outputFor, paramsFor } from "./helpers.ts";

const implemented = ["search_companies", "list_filings", "get_filing_section", "search_filing_chunks", "calculate_financial_metric", "compare_periods", "verify_citations"];
const signal = new AbortController().signal;

test("each implemented tool POSTs to its matching backend route and returns rendered content plus full details", async () => {
	for (const name of implemented) {
		const calls: Array<{ url: string; body: any }> = [];
		const { tools } = setup({ fetchImpl: async (input: any, init: any) => {
			if (init.method === "GET") return jsonResponse({ max_verify_attempts: 2, derived_metric_render: "v1" });
			calls.push({ url: String(input), body: JSON.parse(init.body) });
			return jsonResponse(outputFor(name));
		} });
		const args = paramsFor(name);
		const result = await tools.get(name).execute("call", args, signal, undefined, { hasUI: false, ui: {} });
		assert.equal(calls.length, 1, name);
		assert.ok(calls[0].url.endsWith(`/tools/${name}`), name);
		assert.deepEqual(calls[0].body, name === "verify_citations" ? { ...args, attempt: 1 } : args, name);
		assert.deepEqual(Object.fromEntries(Object.entries(result.details).filter(([key]) => key !== "_financialResearch")), outputFor(name), name);
		assert.equal(result.content[0].type, "text", name);
		assert.ok(result.content[0].text.length > 0, name);
	}
});

test("backend error envelope becomes a thrown typed code/message/fallback error", async () => {
	const { tools } = setup({ fetchImpl: async () => backendError("company_not_found", "No company", "Check its ticker") });
	await assert.rejects(tools.get("search_companies").execute("c", paramsFor("search_companies"), signal, undefined, {}),
		(error: any) => error instanceof BackendToolError && error.code === "company_not_found" && error.message === "company_not_found: No company (Check its ticker)");
});

test("verify_citations posts the structured request to the backend", async () => {
	const calls: any[] = [];
	const { tools } = setup({ fetchImpl: async (input: any, init: any) => {
		if (init.method === "GET") return jsonResponse({ max_verify_attempts: 2, derived_metric_render: "v1" });
		calls.push({ url: String(input), body: JSON.parse(init.body) });
		return jsonResponse({ report: { supported: 1 }, report_draft_id: "draft-x", final_answer_markdown: "Verified answer", retry_allowed: false, is_final: true });
	} });
	const args = { ticker: "MSFT", claims: [{ claim_id: "c1", claim_text: "Revenue grew", claim_type: "narrative", ticker: "MSFT", evidence_ids: ["ev-1"] }] };
	const result = await tools.get("verify_citations").execute("c", args, signal, undefined, {});
	assert.ok(calls[0].url.endsWith("/tools/verify_citations"));
	assert.deepEqual(calls[0].body, { ...args, attempt: 1 });
	assert.ok(result.content[0].text.startsWith("Verified answer"));
	assert.ok(result.content[0].text.endsWith("Present final_answer_markdown to the user as the answer; do not add unverified statements."));
	assert.equal(result.details._financialResearch.attempt, 1);
});

test("verification attempts are state-filled, cannot be lowered, reset per input, and stop before a third fetch", async () => {
	const calls: any[] = [];
	let fetchCount = 0;
	const { tools, state, handlers } = setup({ fetchImpl: async (input: any, init: any) => {
		fetchCount += 1;
		if (init.method === "GET") return jsonResponse({ max_verify_attempts: 2, derived_metric_render: "v1" });
		const body = JSON.parse(init.body);
		calls.push(body);
		const id = body.attempt === 1 ? "draft_aaaaaaaaaaaaaaaa" : "draft_bbbbbbbbbbbbbbbb";
		return jsonResponse({ report: { supported: 0, unsupported: 1 }, report_draft_id: id,
			final_answer_markdown: "The pinned filing evidence did not support an answer to this question.",
			retry_allowed: body.attempt === 1, retry_guidance: body.attempt === 1 ? "Retry once." : null, is_final: body.attempt === 2 });
	} });
	const params = { ticker: "MSFT", claims: [{ claim_id: "c1", claim_text: "Revenue grew", claim_type: "narrative", ticker: "MSFT", evidence_ids: ["ev-1"] }] };
	await tools.get("verify_citations").execute("c1", params, signal, undefined, {});
	const second = await tools.get("verify_citations").execute("c2", { ...params, attempt: 1, parent_draft_id: "model-selected-parent" }, signal, undefined, {});
	assert.deepEqual(calls, [
		{ ...params, attempt: 1 },
		{ ...params, attempt: 2, parent_draft_id: "draft_aaaaaaaaaaaaaaaa" },
	]);
	assert.equal(second.details._financialResearch.requested_attempt_overridden, true);
	assert.ok(second.content[0].text.endsWith("Present final_answer_markdown to the user as the answer; do not add unverified statements."));
	const beforeThird = fetchCount;
	await assert.rejects(tools.get("verify_citations").execute("c3", params, signal, undefined, {}),
		(error: any) => error instanceof BackendToolError && error.code === "retry_budget_exhausted");
	assert.equal(fetchCount, beforeThird);
	for (const handler of handlers.get("input") ?? []) handler({ text: "new user prompt", source: "interactive" });
	assert.equal(state.verifyAttempts, 0);
	assert.equal(state.lastDraftId, undefined);
	assert.equal(state.maxVerifyAttempts, undefined);
});

test("before_agent_start adds the concise query plan guidelines once", () => {
	const { handlers } = setup();
	const event = { systemPromptOptions: { promptGuidelines: ["existing guideline"] } };
	for (const handler of handlers.get("before_agent_start") ?? []) handler(event);
	const guidelines = event.systemPromptOptions.promptGuidelines;
	assert.ok(guidelines.includes("existing guideline"));
	assert.ok(guidelines.some((line) => /calculate_financial_metric|compare_periods/.test(line)));
	assert.ok(guidelines.some((line) => /verify_citations/.test(line)));
	assert.ok(guidelines.some((line) => /at most once/.test(line)));
	assert.ok(guidelines.some((line) => /final_answer_markdown/.test(line)));
	assert.ok(guidelines.length <= 12);
	for (const handler of handlers.get("before_agent_start") ?? []) handler(event);
	assert.equal(event.systemPromptOptions.promptGuidelines.length, guidelines.length);
});

test("unreachable backend produces actionable typed backend-not-running error", async () => {
	const { tools } = setup({ fetchImpl: async () => { throw new TypeError("fetch failed"); } });
	await assert.rejects(tools.get("search_companies").execute("c", paramsFor("search_companies"), signal, undefined, {}),
		(error: any) => error instanceof BackendNotRunningError && /scripts\/serve\.py/.test(error.message));
});

test("non-loopback FRA_API_URL is refused", () => {
	assert.throws(() => new ApiClient({ baseUrl: "http://example.com:8765" }),
		(error: any) => error instanceof BackendToolError && error.code === "invalid_backend_url");
	const previous = process.env.FRA_API_URL;
	process.env.FRA_API_URL = "https://127.0.0.1:8765";
	try {
		assert.throws(() => new ApiClient(), BackendToolError);
	} finally {
		if (previous === undefined) delete process.env.FRA_API_URL;
		else process.env.FRA_API_URL = previous;
	}
});

test("state fills only omitted accession_number and records that it did", async () => {
	const calls: any[] = [];
	const state = { currentCompany: "MSFT", selectedAccession: "state-acc" };
	const { tools } = setup({ state, fetchImpl: async (input: any, init: any) => {
		calls.push({ url: String(input), body: JSON.parse(init.body) });
		return jsonResponse(outputFor("get_filing_section"));
	} });
	const result = await tools.get("get_filing_section").execute("c", paramsFor("get_filing_section"), signal, undefined, {});
	assert.equal(calls[0].body.accession_number, "state-acc");
	assert.equal(result.details._financialResearch.accession_from_state, true);
	assert.equal(result.details._financialResearch.accession_number, "state-acc");
});

test("explicit accession is never overridden by state", async () => {
	const calls: any[] = [];
	const { tools } = setup({ state: { currentCompany: "MSFT", selectedAccession: "state-acc" }, fetchImpl: async (_input: any, init: any) => {
		calls.push(JSON.parse(init.body));
		return jsonResponse(outputFor("get_filing_section"));
	} });
	const args = { ...paramsFor("get_filing_section"), accession_number: "explicit-acc" };
	const result = await tools.get("get_filing_section").execute("c", args, signal, undefined, {});
	assert.equal(calls[0].accession_number, "explicit-acc");
	assert.equal(result.details._financialResearch.accession_from_state, false);
});

test("derived metric v1 snapshots preserve current calculate and compare strings", async () => {
	for (const [name, expected] of [
		["calculate_financial_metric", "revenue: $1 million\nFormula: revenue\nEvidence IDs: fact-1"],
		["compare_periods", "revenue: +$1 million (+1.0%) (A=$1 million; B=$2 million)"],
	] as const) {
		const { tools } = setup({ fetchImpl: async () => jsonResponse(outputFor(name)) });
		const result = await tools.get(name).execute("c", paramsFor(name), signal, undefined, {});
		assert.equal(result.content[0].text, expected);
	}
});

test("derived metric v2 renders citation ids, exact periods and accepted ratio unit forms", async () => {
	const ratio = { result: { metric: "current_ratio", display_value: "1.50x", formula: "current assets / current liabilities",
		result: "1.5", result_unit: "ratio", period_start: "2025-01-01", period_end: "2025-12-31", evidence_ids: ["fact-a"] },
		metric_evidence_id: "met_ratio", fiscal_year: 2025, ticker: "MSFT", accession_number: "acc-ratio" };
	const usd = { result: { metric: "revenue", display_value: "$1 million", formula: "revenue", result: "1000000",
		result_unit: "USD", period_start: "2025-01-01", period_end: "2025-12-31", evidence_ids: ["fact-usd"] },
		metric_evidence_id: "met_usd", fiscal_year: 2025, ticker: "MSFT", accession_number: "acc-usd" };
	const render = async (body: any) => {
		const { tools } = setup({ runtimeConfig: { max_verify_attempts: 2, derived_metric_render: "v2" },
			fetchImpl: async () => jsonResponse(body) });
		return (await tools.get("calculate_financial_metric").execute("c", paramsFor("calculate_financial_metric"), signal, undefined, {})).content[0].text;
	};
	const ratioText = await render(ratio);
	assert.match(ratioText, /met_ratio/);
	assert.match(ratioText, /period_end: "2025-12-31"/);
	assert.match(ratioText, /unit: "ratio" \(or value ×100 with unit: "percent"\)/);
	const usdText = await render(usd);
	assert.match(usdText, /met_usd/);
	assert.match(usdText, /unit: "USD"/);
	assert.doesNotMatch(usdText, /percent/);
});

test("compare v2 renders both side metric evidence IDs and periods", async () => {
	const body = { fiscal_year_a: 2024, fiscal_year_b: 2025, rows: [{ metric: "revenue", result_unit: "USD",
		metric_evidence_id_a: "met_a", metric_evidence_id_b: "met_b", change_display: "+$1 million",
		value_a: { result: "1000000", display_value: "$1 million", period_end: "2024-12-31" },
		value_b: { result: "2000000", display_value: "$2 million", period_end: "2025-12-31" } }] };
	const { tools } = setup({ runtimeConfig: { max_verify_attempts: 2, derived_metric_render: "v2" },
		fetchImpl: async () => jsonResponse(body) });
	const result = await tools.get("compare_periods").execute("c", paramsFor("compare_periods"), signal, undefined, {});
	assert.match(result.content[0].text, /met_a/);
	assert.match(result.content[0].text, /met_b/);
	assert.match(result.content[0].text, /period_end: "2024-12-31"/);
	assert.match(result.content[0].text, /period_end: "2025-12-31"/);
});

test("unknown renderer switch fails with typed error before any tool POST", async () => {
	let postCount = 0;
	const { tools } = setup({ runtimeConfig: { derived_metric_render: "future", max_verify_attempts: 2 },
		fetchImpl: async (_input: any, init: any) => {
		if (init.method === "POST") postCount += 1;
		return jsonResponse({ derived_metric_render: "future", max_verify_attempts: 2 });
	} });
	await assert.rejects(tools.get("calculate_financial_metric").execute("c", paramsFor("calculate_financial_metric"), signal, undefined, {}),
		(error: any) => error instanceof BackendToolError && error.code === "invalid_runtime_config");
	assert.equal(postCount, 0);
});

test("unsupported export uses two confirms and sends preview-derived write parameters", async () => {
	const events: string[] = [];
	const calls: Array<{ path: string; body: any }> = [];
	const previewHashes: string[] = [];
	const { tools } = setup({ fetchImpl: async (input: any, init: any) => {
		const path = new URL(String(input)).pathname;
		const body = JSON.parse(init.body);
		calls.push({ path, body });
		events.push(path.endsWith("/preview") ? "preview" : "write");
		const response = path.endsWith("/preview")
			? { report_draft_id: "draft-1", output_path: "/tmp/report.md", format: "markdown", unsupported_claim_count: 2, supported_claim_count: 1, partially_supported_claim_count: 1, content_sha256: body.acknowledge_unsupported ? "removed-hash" : "abcdef1234567890" }
			: { path: "/tmp/report.md", content_sha256: "removed-hash" };
		if (path.endsWith("/preview")) previewHashes.push(response.content_sha256);
		return jsonResponse(response);
	} });
	const answers = [true, true];
	const ctx = { hasUI: true, ui: { async confirm() { events.push("confirm"); return answers.shift()!; } } };
	const result = await tools.get("export_research_report").execute("c", paramsFor("export_research_report"), signal, undefined, ctx);
	assert.deepEqual(events, ["preview", "confirm", "confirm", "preview", "write"]);
	assert.deepEqual(calls[1].body, { report_draft_id: "draft-1", format: "markdown", output_path: "/tmp/report.md", acknowledge_unsupported: true });
	assert.deepEqual(calls[2].body, { report_draft_id: "draft-1", format: "markdown", output_path: "/tmp/report.md", acknowledge_unsupported: true, preview_content_sha256: previewHashes[1] });
	assert.equal(result.details.confirmed, true);
	assert.equal(result.details.written.path, "/tmp/report.md");
});

test("declining first export confirmation skips the write", async () => {
	const calls: string[] = [];
	const { tools } = setup({ fetchImpl: async (input: any) => { const path = new URL(String(input)).pathname; calls.push(path); return jsonResponse({ report_draft_id: "draft-x", output_path: "report.md", format: "markdown", unsupported_claim_count: 1, content_sha256: "hash" }); } });
	const ctx = { hasUI: true, ui: { async confirm() { return false; } } };
	await tools.get("export_research_report").execute("c", paramsFor("export_research_report"), signal, undefined, ctx);
	assert.deepEqual(calls, ["/tools/export_research_report/preview"]);
});

test("declining Removed appendix confirmation skips the write", async () => {
	const calls: string[] = [];
	const answers = [true, false];
	const { tools } = setup({ fetchImpl: async (input: any) => { const path = new URL(String(input)).pathname; calls.push(path); return jsonResponse({ report_draft_id: "draft-x", output_path: "report.md", format: "markdown", unsupported_claim_count: 1, content_sha256: "hash" }); } });
	const ctx = { hasUI: true, ui: { async confirm() { return answers.shift()!; } } };
	const result = await tools.get("export_research_report").execute("c", paramsFor("export_research_report"), signal, undefined, ctx);
	assert.deepEqual(calls, ["/tools/export_research_report/preview"]);
	assert.equal(result.details.written, false);
});

test("no unsupported claims needs one confirmation then writes false acknowledgement", async () => {
	const calls: Array<{ path: string; body: any }> = [];
	let previewHash = "";
	const { tools } = setup({ fetchImpl: async (input: any, init: any) => { const path = new URL(String(input)).pathname; calls.push({ path, body: JSON.parse(init.body) }); if (path.endsWith("/preview")) { previewHash = "hash"; return jsonResponse({ report_draft_id: "draft-x", output_path: "report.md", format: "markdown", unsupported_claim_count: 0, content_sha256: previewHash }); } return jsonResponse({ path: "report.md" }); } });
	let confirms = 0;
	const ctx = { hasUI: true, ui: { async confirm() { confirms += 1; return true; } } };
	await tools.get("export_research_report").execute("c", paramsFor("export_research_report"), signal, undefined, ctx);
	assert.equal(confirms, 1);
	assert.deepEqual(calls[1].body, { report_draft_id: "draft-x", format: "markdown", output_path: "report.md", acknowledge_unsupported: false, preview_content_sha256: previewHash });
});

test("declined export makes no write request", async () => {
	const events: string[] = [];
	const { tools } = setup({ fetchImpl: async (input: any) => {
		events.push(new URL(String(input)).pathname.endsWith("/preview") ? "preview" : "write");
		return jsonResponse({ output_path: "/tmp/report.md", format: "markdown", unsupported_claim_count: 0, ambiguous_claim_count: 0 });
	} });
	const ctx = { hasUI: true, ui: { async confirm() { events.push("confirm"); return false; } } };
	const result = await tools.get("export_research_report").execute("c", paramsFor("export_research_report"), signal, undefined, ctx);
	assert.deepEqual(events, ["preview", "confirm"]);
	assert.equal(result.details.written, false);
});

test("export refuses without UI and makes no request", async () => {
	let called = false;
	const { tools } = setup({ fetchImpl: async () => { called = true; return jsonResponse({}); } });
	await assert.rejects(tools.get("export_research_report").execute("c", paramsFor("export_research_report"), signal, undefined,
		{ hasUI: false, ui: { async confirm() { return true; } } }),
		(error: any) => error instanceof BackendToolError && error.code === "confirmation_unavailable");
	assert.equal(called, false);
});

test("export confirmation includes all claim counts and preview hash prefix", async () => {
	let confirmation = "";
	const { tools } = setup({ fetchImpl: async () => jsonResponse({ report_draft_id: "d", output_path: "out.md", format: "markdown", unsupported_claim_count: 4, supported_claim_count: 3, partially_supported_claim_count: 2, content_sha256: "abcdef1234567890" }) });
	const ctx = { hasUI: true, ui: { async confirm(_title: string, message: string) { confirmation = message; return false; } } };
	await tools.get("export_research_report").execute("c", paramsFor("export_research_report"), signal, undefined, ctx);
	assert.match(confirmation, /Path: out\.md/);
	assert.match(confirmation, /Format: markdown/);
	assert.match(confirmation, /Supported claims: 3/);
	assert.match(confirmation, /Partially supported claims: 2/);
	assert.match(confirmation, /Unsupported claims: 4/);
	assert.match(confirmation, /Content SHA-256: abcdef123456/);
});
