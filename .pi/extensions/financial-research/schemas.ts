import { Type } from "typebox";

const noExtras = { additionalProperties: false } as const;
const NullableString = Type.Union([Type.String(), Type.Null()]);
const DateString = Type.String({ format: "date" });
const Metric = Type.Union([
	Type.Literal("revenue"), Type.Literal("revenue_growth_yoy"), Type.Literal("operating_margin"),
	Type.Literal("net_margin"), Type.Literal("free_cash_flow"), Type.Literal("current_ratio"),
]);
const SectionName = Type.Union([Type.Literal("risk_factors"), Type.Literal("mdna"), Type.Literal("financial_statements")]);
const ClaimType = Type.Union([Type.Literal("numeric"), Type.Literal("metadata"), Type.Literal("narrative")]);
const NumericAssertion = Type.Object({ value: Type.Union([Type.Number(), Type.String({ pattern: "^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$" })]), scale: Type.Optional(Type.Union([Type.Literal("units"), Type.Literal("thousands"), Type.Literal("millions"), Type.Literal("billions")])), unit: Type.Union([Type.Literal("USD"), Type.Literal("percent"), Type.Literal("ratio")]), period_end: DateString, period_start: Type.Optional(Type.Union([DateString, Type.Null()])) });
const MetadataAssertion = Type.Object({ field: Type.Union([Type.Literal("accession_number"), Type.Literal("filing_date"), Type.Literal("report_date"), Type.Literal("form"), Type.Literal("ticker"), Type.Literal("cik")]), value: Type.String() });
const Claim = Type.Object({ claim_id: Type.String(), claim_text: Type.String(), claim_type: ClaimType, ticker: Type.String(), evidence_ids: Type.Array(Type.String()), numeric: Type.Optional(Type.Union([NumericAssertion, Type.Null()])), metadata: Type.Optional(Type.Union([MetadataAssertion, Type.Null()])) });

export const ToolSchemas = {
	search_companies: Type.Object({ query: Type.String({ minLength: 1 }) }, noExtras),
	list_filings: Type.Object({
		company: Type.String(), form: Type.Union([Type.Literal("10-K"), Type.Literal("10-Q"), Type.Literal("8-K")]),
		date_from: Type.Optional(Type.Union([DateString, Type.Null()])),
		date_to: Type.Optional(Type.Union([DateString, Type.Null()])),
	}),
	get_filing_section: Type.Object({
		company: Type.String({ minLength: 1 }), section: SectionName, accession_number: Type.Optional(NullableString),
		offset: Type.Optional(Type.Integer({ minimum: 0 })), max_chars: Type.Optional(Type.Integer({ minimum: 1, maximum: 40000 })),
	}, noExtras),
	search_filing_chunks: Type.Object({
		question: Type.String({ minLength: 1 }), company: Type.String({ minLength: 1 }),
		accession_number: Type.Optional(NullableString),
		sections: Type.Optional(Type.Union([Type.Array(SectionName), Type.Null()])),
		top_k: Type.Optional(Type.Integer({ minimum: 1, maximum: 20 })),
	}, noExtras),
	calculate_financial_metric: Type.Object({ metric: Metric, company: Type.String({ minLength: 1 }),
		fiscal_year: Type.Integer(), accession_number: Type.Optional(NullableString) }, noExtras),
	compare_periods: Type.Object({ company: Type.String({ minLength: 1 }), fiscal_year_a: Type.Integer(),
		fiscal_year_b: Type.Integer(), metrics: Type.Array(Metric, { minItems: 1, maxItems: 6 }),
		accession_number: Type.Optional(NullableString) }, noExtras),
	verify_citations: Type.Object({ claims: Type.Array(Claim, { minItems: 1, maxItems: 50 }), ticker: Type.String({ minLength: 1 }),
		attempt: Type.Optional(Type.Integer({ minimum: 1 })), parent_draft_id: Type.Optional(NullableString) }, noExtras),
	export_research_report: Type.Object({ report_draft_id: Type.String({ minLength: 1 }),
		format: Type.Union([Type.Literal("markdown"), Type.Literal("html")]), output_path: Type.String({ minLength: 1 }), acknowledge_unsupported: Type.Optional(Type.Boolean()) }, noExtras),
} as const;

export type ToolName = keyof typeof ToolSchemas;
