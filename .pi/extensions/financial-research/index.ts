import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { ApiClient, BackendToolError } from "./client.ts";
import { ToolSchemas, type ToolName } from "./schemas.ts";
import { resetVerificationState, resolveAccession, updateCompanyState, type ResearchState } from "./state.ts";

const IMPLEMENTED = [
	"search_companies", "list_filings", "get_filing_section", "search_filing_chunks",
	"calculate_financial_metric", "compare_periods", "verify_citations", "export_research_report",
] as const satisfies readonly ToolName[];
const ACCESSION_TOOLS = new Set<ToolName>([
	"get_filing_section", "search_filing_chunks", "calculate_financial_metric", "compare_periods",
]);

const DESCRIPTIONS: Record<ToolName, string> = {
	search_companies: "Find SEC companies by ticker, CIK, or name using the raw cache; it does not fetch or authorize unpinned companies, and unmatched queries return an explicit error.",
	list_filings: "List SEC filings and exclusions for a company and date range; this does not pin or ingest filings, and unavailable ranges return a typed error.",
	get_filing_section: "Read a paginated section from the pinned filing's verified normalized text; it does not summarize or silently truncate, and missing build output points to scripts/build_chunks.py.",
	search_filing_chunks: "Retrieve ranked evidence from the pinned filing; results are evidence, not answers, and all numbers must come from calculate_financial_metric or compare_periods.",
	calculate_financial_metric: "Compute a supported structured metric from verified companyfacts for the pinned accession; numbers must come from this tool rather than retrieved text, and unavailable facts return typed errors.",
	compare_periods: "Compare structured metrics across fiscal years within one pinned accession; values are calculated by calculate_financial_metric, never retrieved text, and missing years return typed errors without partial values.",
	verify_citations: "Verifies structured claims against ledger evidence. Returns final_answer_markdown, retry_allowed, and bounded retry guidance; unsupported claims must never be presented as factual statements.",
	export_research_report: "Preview and write a report containing supported claims and labelled partial claims. Unsupported claims are omitted unless explicitly acknowledged in the Removed appendix. The Pi extension must obtain ctx.ui.confirm() between preview and write.",
};

type JsonObject = Record<string, any>;
type ToolContext = { hasUI?: boolean; ui: { confirm(title: string, message: string): Promise<boolean> } };
type Execute = (toolCallId: string, params: JsonObject, signal: AbortSignal, onUpdate: unknown, ctx: ToolContext) => Promise<any>;

export interface ExtensionTestOptions {
	client?: ApiClient;
	state?: ResearchState;
}

const QUERY_PLAN_GUIDELINES = [
	"Resolve a company name, ticker, or CIK with search_companies; use list_filings for filing metadata and get_filing_section for a known section.",
	"Use calculate_financial_metric for a single financial value or ratio; use compare_periods for period comparisons.",
	"Numbers come only from calculate_financial_metric or compare_periods.",
	"Use search_filing_chunks for filing explanations, causes, risks, and trends.",
	"For mixed numeric and explanatory questions, calculate the numbers and retrieve relevant filing chunks before synthesis.",
	"Always submit structured claims to verify_citations before answering.",
	"If verification has unsupported or partially supported claims, retrieve or calculate more evidence and retry verification at most once.",
	"The final answer is final_answer_markdown; present it without adding unverified statements.",
	"For a requested report, assemble only verified claims, preview it, obtain Pi confirmation, then export it.",
];

export function registerFinancialResearch(pi: Pick<ExtensionAPI, "registerTool" | "on">, options: ExtensionTestOptions = {}): ResearchState {
	const state = options.state ?? {};
	let client = options.client;
	const getClient = () => client ??= new ApiClient();
	pi.on("input", () => resetVerificationState(state));
	pi.on("before_agent_start", (event) => {
		const existing = event.systemPromptOptions.promptGuidelines;
		event.systemPromptOptions.promptGuidelines = [
			...existing.filter((line) => !QUERY_PLAN_GUIDELINES.includes(line)),
			...QUERY_PLAN_GUIDELINES,
		];
	});

	for (const name of IMPLEMENTED.filter((item) => item !== "export_research_report")) {
		pi.registerTool({
			name,
			label: toolLabel(name),
			description: DESCRIPTIONS[name],
			parameters: ToolSchemas[name],
			execute: async (_id: string, params: JsonObject, signal: AbortSignal) => {
				await ensureRuntimeConfig(state, getClient, signal);
				const { args, resolution } = ACCESSION_TOOLS.has(name)
					? resolveAccession(params, state)
					: { args: { ...params }, resolution: { accession_from_state: false } };
				const result = await getClient().post<JsonObject>(`/tools/${name}`, args, signal);
				updateFromResult(name, result, state, args);
				return {
					content: [{ type: "text", text: renderToolResult(name, result, state.derivedMetricRender) }],
					details: { ...result, _financialResearch: resolution },
				};
			},
		});
	}

	pi.registerTool({
		name: "verify_citations",
		label: "Verify citations",
		description: DESCRIPTIONS.verify_citations,
		parameters: ToolSchemas.verify_citations,
		execute: async (_id: string, params: JsonObject, signal: AbortSignal) => {
			await ensureRuntimeConfig(state, getClient, signal);
			if (state.maxVerifyAttempts === undefined) {
				const runtime = await getClient().get<JsonObject>("/config/runtime", signal);
				if (!Number.isInteger(runtime.max_verify_attempts) || runtime.max_verify_attempts < 1) {
					throw new BackendToolError("invalid_runtime_config", "Backend max_verify_attempts must be a positive integer", undefined, 500);
				}
				state.maxVerifyAttempts = runtime.max_verify_attempts;
			}
			const attempt = (state.verifyAttempts ?? 0) + 1;
			if (attempt > state.maxVerifyAttempts) {
				throw new BackendToolError("retry_budget_exhausted", `Verification attempt ${attempt} exceeds the configured maximum of ${state.maxVerifyAttempts}`, "Present the latest final_answer_markdown without another evidence retry.", 422);
			}
			const parentDraftId = attempt > 1 ? state.lastDraftId : undefined;
			if (attempt > 1 && !parentDraftId) {
				throw new BackendToolError("retry_parent_invalid", "The prior verification did not return a parent draft ID", "Retry from the previous successful verification.", 422);
			}
			const requestedAttempt = params.attempt;
			const args = { ...params, attempt, parent_draft_id: parentDraftId };
			if (parentDraftId === undefined) delete args.parent_draft_id;
			state.verifyAttempts = attempt;
			const result = await getClient().post<JsonObject>("/tools/verify_citations", args, signal);
			if (typeof result.report_draft_id === "string") {
				state.lastDraftId = result.report_draft_id;
				state.reportDraftId = result.report_draft_id;
			}
			const finalText = typeof result.final_answer_markdown === "string" ? result.final_answer_markdown : "";
			const content = [finalText, renderToolResult("verify_citations", result)];
			if (typeof result.retry_guidance === "string" && result.retry_guidance) content.push(result.retry_guidance);
			if (result.is_final === true) content.push("Present final_answer_markdown to the user as the answer; do not add unverified statements.");
			return { content: [{ type: "text", text: content.filter(Boolean).join("\n\n") }],
				details: { ...result, _financialResearch: { attempt,
				parent_draft_id: parentDraftId ?? null,
				requested_attempt: requestedAttempt ?? null,
				requested_attempt_overridden: requestedAttempt !== undefined && requestedAttempt !== attempt } } };
		},
	});

	pi.registerTool({
		name: "export_research_report",
		label: "Export research report",
		description: DESCRIPTIONS.export_research_report,
		parameters: ToolSchemas.export_research_report,
		execute: async (_id: string, params: JsonObject, signal: AbortSignal, _onUpdate: unknown, ctx: ToolContext) => {
			await ensureRuntimeConfig(state, getClient, signal);
			if (!ctx?.hasUI) {
				throw new BackendToolError("confirmation_unavailable", "Report export requires an interactive confirmation", undefined, 0);
			}
			const previewParams = { ...params };
			delete previewParams.acknowledge_unsupported;
			const preview = await getClient().post<JsonObject>("/tools/export_research_report/preview", previewParams, signal);
			if (preview.report_draft_id) state.reportDraftId = preview.report_draft_id;
			const targetPath = String(preview.output_path ?? preview.path ?? params.output_path);
			const format = String(preview.format ?? params.format);
			const unsupported = countFrom(preview, ["unsupported_claim_count"]);
			const partial = countFrom(preview, ["partially_supported_claim_count"]);
			const supported = countFrom(preview, ["supported_claim_count"]);
			const confirmed = await ctx.ui.confirm("Export research report?",
				`Path: ${targetPath}\nFormat: ${format}\nSupported claims: ${supported}\nPartially supported claims: ${partial}\nUnsupported claims: ${unsupported}\nContent SHA-256: ${String(preview.content_sha256 ?? "").slice(0, 12)}`);
			if (!confirmed) {
				return { content: [{ type: "text", text: "Report export cancelled; no file was written." }],
					details: { preview, confirmed: false, written: false } };
			}
			let acknowledgeUnsupported = false;
			if (unsupported > 0) {
				acknowledgeUnsupported = await ctx.ui.confirm("Include removed claims appendix?",
					`The appendix "Removed — not supported by evidence" will list ${unsupported} unsupported claims and their reasons.`);
				if (!acknowledgeUnsupported) return { content: [{ type: "text", text: "Report export cancelled; no file was written." }], details: { preview, confirmed: true, written: false, unsupported_acknowledged: false } };
			}
			const writePreview = acknowledgeUnsupported
				? await getClient().post<JsonObject>("/tools/export_research_report/preview", { ...previewParams, acknowledge_unsupported: true }, signal)
				: preview;
			const writeParams = { report_draft_id: preview.report_draft_id, format: preview.format,
				output_path: preview.output_path, acknowledge_unsupported: acknowledgeUnsupported,
				preview_content_sha256: writePreview.content_sha256 };
			const written = await getClient().post<JsonObject>("/tools/export_research_report/write", writeParams, signal);
			if (written.report_draft_id) state.reportDraftId = written.report_draft_id;
			return { content: [{ type: "text", text: renderToolResult("export_research_report", written) }],
					details: { preview, write_preview: writePreview, written, confirmed: true } };
		},
	});
	return state;
}

export default function (pi: ExtensionAPI): void {
	registerFinancialResearch(pi);
}

function toolLabel(name: string): string {
	return name.split("_").map((part) => part[0].toUpperCase() + part.slice(1)).join(" ");
}

function updateFromResult(name: string, result: JsonObject, state: ResearchState, args: JsonObject): void {
	if (name === "search_companies" && result.candidates?.length === 1 && result.candidates[0].pinned) {
		updateCompanyState(state, result.candidates[0].ticker, result.candidates[0].pinned_accession);
		return;
	}
	const company = result.ticker ?? result.company?.ticker ?? result.evidence?.[0]?.ticker ?? result.company ?? args.company;
	const accession = result.accession_number;
	if (typeof company === "string") updateCompanyState(state, company, accession);
	if (typeof result.report_draft_id === "string") state.reportDraftId = result.report_draft_id;
}

async function ensureRuntimeConfig(state: ResearchState, getClient: () => ApiClient, signal: AbortSignal): Promise<void> {
	if (state.derivedMetricRender !== undefined) return;
	const runtime = await getClient().get<JsonObject>("/config/runtime", signal);
	if (runtime.derived_metric_render !== "v1" && runtime.derived_metric_render !== "v2") {
		throw new BackendToolError("invalid_runtime_config", "Backend derived_metric_render must be v1 or v2", undefined, 500);
	}
	state.derivedMetricRender = runtime.derived_metric_render;
}

function renderToolResult(name: string, result: JsonObject, renderMode: "v1" | "v2" = "v1"): string {
	if (name === "verify_citations") {
		const report = result.report ?? {};
		return `Verification complete: ${report.supported ?? 0} supported, ${report.partially_supported ?? 0} partially supported, ${report.unsupported ?? 0} unsupported. Draft: ${result.report_draft_id ?? "unavailable"}`;
	}
	if (name === "get_filing_section") {
		const next = result.next_offset === null || result.next_offset === undefined
			? "This section is complete."
			: `Call get_filing_section again with offset=${result.next_offset} for the next page.`;
		return `${result.text ?? ""}\n\nSection page ${result.offset ?? 0}; returned ${result.returned_chars ?? 0} of ${result.total_chars ?? 0} characters. ${next}`;
	}
	if (name === "calculate_financial_metric") {
		const metric = result.result ?? {};
		if (renderMode === "v2") return renderCalculatedMetric(metric, result.metric_evidence_id, result.fiscal_year, result.ticker, result.accession_number);
		return `${metric.metric ?? "metric"}: ${metric.display_value ?? metric.result}\nFormula: ${metric.formula ?? "unknown"}\nEvidence IDs: ${(metric.evidence_ids ?? []).join(", ")}`;
	}
	if (name === "compare_periods") {
		if (renderMode === "v2") return (result.rows ?? []).map((row: JsonObject) => {
			if (row.error) return `${row.metric}: ${row.error.code}: ${row.error.message}`;
			const sideA = renderCompareSide(row, "a", result.fiscal_year_a);
			const sideB = renderCompareSide(row, "b", result.fiscal_year_b);
			return [sideA, sideB, `Change: ${row.change_display ?? "no display"}`].join("\n");
		}).join("\n");
		return (result.rows ?? []).map((row: JsonObject) => row.error
			? `${row.metric}: ${row.error.code}: ${row.error.message}`
			: `${row.metric}: ${row.change_display ?? "no display"} (A=${row.value_a?.display_value ?? "n/a"}; B=${row.value_b?.display_value ?? "n/a"})`).join("\n");
	}
	if (name === "search_filing_chunks") {
		return (result.evidence ?? []).map((item: JsonObject) => `[${item.evidence_id}] ${item.section}: ${item.text}\nSource: ${item.source_url}`).join("\n\n") || "No evidence found.";
	}
	if (name === "search_companies") {
		return (result.candidates ?? []).map((item: JsonObject) => `${item.ticker} (${item.cik}) — ${item.company_name}; ${item.match_type} match; ${item.pinned ? `pinned ${item.pinned_accession}` : "not pinned"}`).join("\n");
	}
	if (name === "list_filings") {
		return (result.filings ?? []).map((item: JsonObject) => `${item.form} ${item.accession_number} filed ${item.filing_date}; report date ${item.report_date}`).join("\n") || "No filings found.";
	}
	return JSON.stringify(result);
}

function renderCalculatedMetric(metric: JsonObject, metricEvidenceId: string, fiscalYear: number, ticker: string, accession: string): string {
	const unit = metric.result_unit ?? "ratio";
	const citeUnit = unit === "USD" ? '"USD"' : '"ratio" (or value ×100 with unit: "percent")';
	const result = metric.result ?? "unknown";
	const value = result;
	return [
		`${metric.metric ?? "metric"}: ${metric.display_value ?? result}  (exact result: ${result}, result_unit: ${unit})`,
		`Period: ${metric.period_start ?? "unknown"} to ${metric.period_end ?? "unknown"}  (fiscal year ${fiscalYear ?? "unknown"}; ticker ${ticker ?? "unknown"}; accession ${accession ?? "unknown"})`,
		`Formula: ${metric.formula ?? "unknown"}`,
		`To cite this value in verify_citations: evidence_ids ["${metricEvidenceId ?? "missing"}"], numeric {value: ${value}, unit: ${citeUnit}, period_end: "${metric.period_end ?? "unknown"}"}.`,
		`Input facts (supporting detail; cite the metric id above for this value): ${(metric.evidence_ids ?? []).join(", ")}`,
	].join("\n");
}

function renderCompareSide(row: JsonObject, side: "a" | "b", fiscalYear: number): string {
	const value = row[`value_${side}`] ?? {};
	const evidenceId = row[`metric_evidence_id_${side}`] ?? "missing";
	const unit = row.result_unit ?? value.result_unit ?? "ratio";
	const citeUnit = unit === "USD" ? '"USD"' : '"ratio" (or value ×100 with unit: "percent")';
	return `${row.metric} FY${fiscalYear ?? "unknown"}: ${value.display_value ?? value.result ?? "n/a"} (exact result: ${value.result ?? "unknown"}, result_unit: ${unit}). To cite this value in verify_citations: evidence_ids ["${evidenceId}"], numeric {value: ${value.result ?? "unknown"}, unit: ${citeUnit}, period_end: "${value.period_end ?? "unknown"}"}.`;
}

function countFrom(value: JsonObject, keys: string[]): number {
	for (const key of keys) {
		const candidate = value[key];
		if (typeof candidate === "number") return candidate;
		if (Array.isArray(candidate)) return candidate.length;
	}
	return 0;
}
