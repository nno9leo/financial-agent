import { ApiClient } from "../client.ts";
import { registerFinancialResearch } from "../index.ts";

export function setup(options: { fetchImpl?: typeof fetch; baseUrl?: string; state?: Record<string, unknown>;
	runtimeConfig?: Record<string, unknown> } = {}) {
	const tools = new Map<string, any>();
	const handlers = new Map<string, Array<(event: any) => any>>();
	const pi = {
		registerTool(tool: any) { tools.set(tool.name, tool); },
		on(event: string, handler: (event: any) => any) { handlers.set(event, [...(handlers.get(event) ?? []), handler]); return () => {}; },
	};
	const fetchImpl: typeof fetch = async (input, init) => {
		if (new URL(String(input)).pathname === "/config/runtime") {
			return jsonResponse(options.runtimeConfig ?? { max_verify_attempts: 2, derived_metric_render: "v1" });
		}
		return options.fetchImpl ? options.fetchImpl(input, init) : jsonResponse({});
	};
	const client = new ApiClient({ baseUrl: options.baseUrl, fetchImpl });
	const state = registerFinancialResearch(pi as any, { client, state: options.state as any });
	return { tools, state, handlers };
}

export function jsonResponse(body: unknown, status = 200): Response {
	return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

export function backendError(code: string, message: string, fallback: string | null = null) {
	return jsonResponse({ error: { code, message, fallback, tool: "test" } }, 422);
}

export function paramsFor(name: string): Record<string, unknown> {
	switch (name) {
		case "search_companies": return { query: "MSFT" };
		case "list_filings": return { company: "MSFT", form: "10-K" };
		case "get_filing_section": return { company: "MSFT", section: "mdna", offset: 0, max_chars: 100 };
		case "search_filing_chunks": return { company: "MSFT", question: "revenue" };
		case "calculate_financial_metric": return { company: "MSFT", metric: "revenue", fiscal_year: 2026 };
		case "compare_periods": return { company: "MSFT", fiscal_year_a: 2025, fiscal_year_b: 2026, metrics: ["revenue"] };
		case "verify_citations": return { claims: [] };
		case "export_research_report": return { report_draft_id: "draft-1", format: "markdown", output_path: "/tmp/report.md" };
		default: throw new Error(`unknown test tool ${name}`);
	}
}

export function outputFor(name: string): Record<string, unknown> {
	switch (name) {
		case "search_companies": return { candidates: [{ ticker: "MSFT", cik: "0000789019", company_name: "Microsoft", match_type: "ticker", pinned: true, pinned_accession: "acc-1" }], ambiguous: false };
		case "list_filings": return { filings: [], excluded: [], coverage_start: null, older_pages_not_loaded: false, company: { ticker: "MSFT", cik: "0000789019", company_name: "Microsoft" } };
		case "get_filing_section": return { text: "page text", total_chars: 20, offset: 0, returned_chars: 9, next_offset: 9, truncated: true, heading_text: "ITEM 7", section_sha256: "sha", start_offset: 0, end_offset: 20, ticker: "MSFT", cik: "0000789019", accession_number: "acc-1", form: "10-K", filing_date: "2026-01-01", report_date: "2025-12-31", source_url: "https://example.test" };
		case "search_filing_chunks": return { evidence: [{ evidence_id: "ev-1", section: "mdna", text: "Revenue evidence", source_url: "https://example.test" }], accession_number: "acc-1" };
		case "calculate_financial_metric": return { result: { metric: "revenue", display_value: "$1 million", formula: "revenue", evidence_ids: ["fact-1"] }, ticker: "MSFT", accession_number: "acc-1" };
		case "compare_periods": return { fiscal_year_a: 2025, fiscal_year_b: 2026, rows: [{ metric: "revenue", change_display: "+$1 million (+1.0%)", value_a: { display_value: "$1 million" }, value_b: { display_value: "$2 million" }, error: null }], accession_number: "acc-1" };
		default: return {};
	}
}
