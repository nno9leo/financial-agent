import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { registerFinancialResearch } from "../index.ts";

const root = new URL("../../../../", import.meta.url);
const fixtureUrl = new URL("../../../../tests/fixtures/api/tools.json", import.meta.url);
const implemented = ["search_companies", "list_filings", "get_filing_section", "search_filing_chunks", "calculate_financial_metric", "compare_periods", "verify_citations", "export_research_report"];
const expected = [...implemented];

test("registers exactly eight implemented backend tool names", async () => {
	const registered = new Map<string, any>();
	registerFinancialResearch({ registerTool(tool: any) { registered.set(tool.name, tool); }, on() { return () => {}; } } as any);
	assert.deepEqual([...registered.keys()].sort(), [...expected].sort());
	assert.doesNotMatch(registered.get("verify_citations").description, /not yet available/i);
	assert.doesNotMatch(registered.get("export_research_report").description, /not yet available/i);
});

test("TypeBox input schemas match backend registry properties, requirements, types, enums, bounds, and extra policy", async () => {
	const fixture = JSON.parse(await readFile(fileURLToPath(fixtureUrl), "utf8"));
	const backend = new Map(fixture.tools.map((tool: any) => [tool.name, tool]));
	const registered = new Map<string, any>();
	registerFinancialResearch({ registerTool(tool: any) { registered.set(tool.name, tool); }, on() { return () => {}; } } as any);
	for (const name of implemented) {
		const expectedSchema = (backend.get(name) as any).input_schema;
		const actualSchema = registered.get(name).parameters;
		assert.deepEqual(Object.keys(actualSchema.properties).sort(), Object.keys(expectedSchema.properties).sort(), `${name}: property names`);
		assert.deepEqual([...(actualSchema.required ?? [])].sort(), [...(expectedSchema.required ?? [])].sort(), `${name}: required fields`);
		for (const property of Object.keys(expectedSchema.properties)) {
			const expectedProperty = expectedSchema.properties[property];
			const actualProperty = actualSchema.properties[property];
			assert.deepEqual(schemaShape(actualProperty), schemaShape(expectedProperty), `${name}.${property}: schema mismatch\nactual=${JSON.stringify(actualProperty)}\nexpected=${JSON.stringify(expectedProperty)}`);
		}
		if (expectedSchema.additionalProperties === false) assert.equal(actualSchema.additionalProperties, false, `${name}: additionalProperties`);
		assert.equal(actualSchema.additionalProperties === false, expectedSchema.additionalProperties === false, `${name}: additionalProperties policy`);
	}
});

function schemaShape(schema: any): any {
	const alternatives = schema.anyOf ?? schema.oneOf;
	if (alternatives) {
		const shapes = alternatives.map(schemaShape);
		const enums = [...new Set(shapes.flatMap((shape: any) => shape.enum ?? []))].sort();
		const types = [...new Set(shapes.flatMap((shape: any) => shape.types ?? [shape.type]).filter(Boolean))].sort();
		const shape: any = { types };
		if (enums.length) shape.enum = enums;
		const itemShapes = shapes.map((item: any) => item.items).filter(Boolean);
		if (itemShapes.length) shape.items = itemShapes[0];
		return shape;
	}
	const shape: any = { types: [schema.type ?? (schema.const !== undefined ? typeof schema.const : undefined)].filter(Boolean) };
	const enumValues = schema.enum ?? (schema.const !== undefined ? [schema.const] : undefined);
	if (enumValues) shape.enum = [...enumValues].sort();
	if (schema.minimum !== undefined) shape.minimum = schema.minimum;
	if (schema.maximum !== undefined) shape.maximum = schema.maximum;
	if (schema.minItems !== undefined) shape.minItems = schema.minItems;
	if (schema.maxItems !== undefined) shape.maxItems = schema.maxItems;
	if (schema.items) shape.items = schemaShape(schema.items);
	return shape;
}
