import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { buildPiArgv, PiIsolationError, readRuntimeConfig, validatePiArgv } from "./lib/pi_args.mjs";
import { assertNoAssistantError, assertActiveTools, activeToolsFromState, assertExportSmoke, assertExportTraceOrder, recordConfirmRequest } from "./lib/pi_smoke_events.mjs";

const config = await readRuntimeConfig();
const sessionDir = "/tmp/pi-smoke-session";

test("Pi argv exactly follows configured model and mandatory isolation", () => {
	assert.deepEqual(buildPiArgv(config, sessionDir), [
		"--mode", "rpc", ...config.pi_cli_args, ...config.pi_isolation_args, "--session-dir", sessionDir,
	]);
});

test("missing or altered isolation values and approval flags fail closed", () => {
	const expected = buildPiArgv(config, sessionDir);
	const cases = [
		expected.filter((_, i) => i !== expected.indexOf("--tools")),
		mutateAfter(expected, "--tools", "read,bash"),
		expected.filter((arg) => arg !== "--no-extensions"),
		mutateAfter(expected, "--extension", "./other.ts"),
		expected.filter((arg) => arg !== "-na"),
		expected.filter((arg) => arg !== "--no-skills"),
		expected.filter((arg) => arg !== "--no-prompt-templates"),
		[...expected, "-a"],
		[...expected, "--approve"],
	];
	for (const args of cases) assert.throws(() => validatePiArgv(args, config), PiIsolationError);
});

test("recorded Anthropic assistant error is surfaced verbatim", async () => {
	const fixture = "tests/fixtures/pi/assistant_error.jsonl"; // synthetic record in the shape Pi emits
	const records = (await readFile(fixture, "utf8")).trim().split("\n").map((line) => JSON.parse(line));
	const event = records.find((record) => record.type === "message" && record.message?.stopReason === "error");
	assert.ok(event);
	assert.throws(() => assertNoAssistantError([event]), (error) => error.message === event.message.errorMessage);
});

test("active tool names are checked when exposed and otherwise explicitly reported", () => {
	assert.equal(activeToolsFromState({ data: {} }), "not_reported_by_runtime");
	assert.equal(assertActiveTools({ activeTools: config.pi_tool_allowlist }, config.pi_tool_allowlist).length, 8);
	assert.throws(() => assertActiveTools({ tools: ["bash"] }, config.pi_tool_allowlist), /differ from allowlist/);
});

test("confirm recorder captures confirm requests and rejects other dialogs", () => {
	const confirmations = [];
	const responses = [];
	assert.equal(recordConfirmRequest({ type: "other_event" }, confirmations, (response) => responses.push(response)), false);
	assert.equal(recordConfirmRequest({ type: "extension_ui_request", method: "confirm", id: "c1", title: "Preview", message: "Supported claims: 2; partially supported claims: 0; unsupported claims: 1" }, confirmations, (response) => responses.push(response)), true);
	assert.deepEqual(confirmations, [{ title: "Preview", message: "Supported claims: 2; partially supported claims: 0; unsupported claims: 1" }]);
	assert.deepEqual(responses, [{ type: "extension_ui_response", id: "c1", confirmed: true }]);
	assert.throws(() => recordConfirmRequest({ type: "extension_ui_request", method: "select", id: "c2", title: "Pick", message: "Choose" }, confirmations, () => {}), /Unsupported Pi dialog type/);
});

test("export assertions reject missing tools, wrong confirm count, hash mismatch, and tools outside allowlist", () => {
	const allowlist = ["calculate_financial_metric", "export_research_report"];
	const events = allowlist.map((toolName) => ({ type: "tool_execution_start", toolName }));
	const confirmations = [
		{ title: "Export preview", message: "Supported claims: 2; partially supported claims: 0; unsupported claims: 1" },
		{ title: "Removed appendix", message: "A Removed appendix will be included." },
	];
	const valid = { events, confirmations, reportText: "## Findings\nSupported finding.\n\n## Removed — not supported by evidence\nUnsupported claim.", reportHash: "hash", expectedReportHash: "hash", unsupportedClaimText: "Unsupported claim.", allowlist };
	assert.deepEqual(assertExportSmoke(valid), allowlist);
	assert.throws(() => assertExportSmoke({ ...valid, events: events.slice(1) }), /Required tool execution missing/);
	assert.throws(() => assertExportSmoke({ ...valid, confirmations: confirmations.slice(0, 1) }), /exactly 2 confirm requests/);
	assert.throws(() => assertExportSmoke({ ...valid, reportHash: "wrong" }), /Report hash mismatch/);
	assert.throws(() => assertExportSmoke({ ...valid, events: [...events, { type: "tool_execution_start", toolName: "bash" }] }), /Non-allowlisted tool executed/);
});

test("export trace actions are asserted in order", () => {
	assert.deepEqual(assertExportTraceOrder([
		{ tool: "export_research_report_preview" },
		{ tool: "export_research_report_preview" },
		{ tool: "export_research_report_write" },
	]), ["preview", "preview", "write"]);
	assert.throws(() => assertExportTraceOrder([{ tool: "export_research_report_preview" }, { tool: "export_research_report_write" }]), /preview → preview → write/);
});

function mutateAfter(args, flag, value) {
	const changed = [...args];
	changed[changed.indexOf(flag) + 1] = value;
	return changed;
}
