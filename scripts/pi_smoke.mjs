#!/usr/bin/env node
import { spawn } from "node:child_process";
import { once } from "node:events";
import { createInterface } from "node:readline";
import { appendFile, mkdir, readFile, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { buildPiArgv, readRuntimeConfig } from "./lib/pi_args.mjs";
import { activeToolsFromState, assertActiveTools, assertExportSmoke, assertExportTraceOrder,
	assertNoAssistantError, recordConfirmRequest } from "./lib/pi_smoke_events.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const artifacts = path.join(root, "artifacts", "smoke", "pi");
const reportsDir = path.join(root, "artifacts", "reports");
const config = await readRuntimeConfig(path.join(root, "config", "agent_runtime.json"));
const exportMode = process.argv.includes("--export-confirm");
const apiUrl = process.env.FRA_API_URL ?? "http://127.0.0.1:8765";
const api = new URL(apiUrl);
if (api.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]", "::1"].includes(api.hostname)) {
	throw new Error("FRA_API_URL must use a loopback HTTP address");
}
const startedAt = new Date();
const timestamp = startedAt.toISOString().replaceAll(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
const sessionDir = path.join(artifacts, `session-${startedAt.toISOString().replaceAll(/[:.]/g, "-")}`);
const discoveryPath = path.join(artifacts, "discovery.jsonl");
const summaryPath = path.join(artifacts, "export-confirm-summary.json");
const backendScript = path.join(root, "scripts", "serve.py");
const python = path.join(root, ".venv", "bin", "python");
const piArgs = buildPiArgv(config, sessionDir);
const tracePath = path.join(root, "artifacts", "traces", `${startedAt.toISOString().slice(0, 10)}.jsonl`);

await mkdir(sessionDir, { recursive: true });
// Passing an explicit port avoids Settings()'s default .env lookup in serve.py.
const backend = spawn(python, [backendScript, "--port", String(api.port || 80)], { cwd: root, stdio: ["ignore", "ignore", "pipe"] });
let backendStderr = "";
backend.stderr.setEncoding("utf8").on("data", (chunk) => { backendStderr += chunk; });
let pi;
try {
	await waitForBackend(apiUrl, backend, () => backendStderr);
	if (exportMode) {
		const traceOffset = await fileSize(tracePath);
		const seeded = await seedExportDraft(apiUrl);
		const reportName = `pi-smoke-${timestamp}.md`;
		const reportPath = path.join(reportsDir, reportName);
		const unsupportedClaimText = "Microsoft's revenue for fiscal 2025 was $331.8 billion.";
		const run = await runExportRpc({ url: apiUrl, sessionDir, piArgs, config, reportName, reportPath,
			draftId: seeded.report_draft_id, unsupportedClaimText, onProcess: (child) => { pi = child; } });
		const traceRows = await readTraceRows(tracePath, traceOffset);
		assertExportTraceOrder(traceRows);
		const summary = {
			argv: [process.env.PI_BIN ?? "pi", ...piArgs],
			observed_model: run.observedModel,
			tools_executed: run.toolsExecuted,
			confirmations: run.confirmations,
			report_path: path.relative(root, reportPath),
			report_sha256: run.reportHash,
			tokens: run.stats?.data?.tokens ?? null,
			cost: run.stats?.data?.cost ?? null,
			report_draft_id: seeded.report_draft_id,
			semantic_judge: "not invoked; all seed claims are deterministic",
			trace_actions: ["preview", "preview", "write"],
		};
		await writeFile(summaryPath, `${JSON.stringify(summary, null, 2)}\n`, "utf8");
		console.log(`Export-confirm run completed. Summary: ${path.relative(root, summaryPath)}`);
		console.log(`Model: ${run.observedModel.provider}/${run.observedModel.id} (${run.observedModel.thinkingLevel}); tools: ${run.toolsExecuted.join(", ")}`);
		console.log(`Report: ${path.relative(root, reportPath)} (${run.reportHash}); tokens: ${JSON.stringify(summary.tokens)}; cost: ${JSON.stringify(summary.cost)}`);
	} else {
		await runRpc({ mode: "discovery", url: apiUrl, sessionDir, piArgs, config,
			outputPath: discoveryPath, onProcess: (child) => { pi = child; } });
	}
} finally {
	if (pi && pi.exitCode === null) {
		pi.stdin.end();
		await Promise.race([waitForExit(pi), delay(5000)]);
		if (pi.exitCode === null) pi.kill("SIGTERM");
	}
	backend.kill("SIGTERM");
	await Promise.race([waitForExit(backend), delay(5000)]);
	if (backend.exitCode === null) backend.kill("SIGTERM");
}

async function seedExportDraft(url) {
	const revenue = await postTool(url, "calculate_financial_metric", { metric: "revenue", company: "MSFT", fiscal_year: 2026 });
	await postTool(url, "calculate_financial_metric", { metric: "operating_margin", company: "MSFT", fiscal_year: 2026 });
	const evidenceId = revenue.result?.evidence_ids?.[0];
	if (typeof evidenceId !== "string" || !evidenceId.startsWith("fact_")) throw new Error("Revenue seed did not return a structured fact evidence ID");
	const claims = [
		{ claim_id: "smoke-supported-revenue-fy2026", claim_text: "Microsoft's revenue for fiscal 2026 was $331.8 billion.", claim_type: "numeric", ticker: "MSFT", evidence_ids: [evidenceId], numeric: { value: "331.8", scale: "billions", unit: "USD", period_start: "2025-07-01", period_end: "2026-06-30" } },
		{ claim_id: "smoke-unsupported-revenue-fy2025", claim_text: "Microsoft's revenue for fiscal 2025 was $331.8 billion.", claim_type: "numeric", ticker: "MSFT", evidence_ids: [evidenceId], numeric: { value: "331.8", scale: "billions", unit: "USD", period_start: "2024-07-01", period_end: "2025-06-30" } },
		{ claim_id: "smoke-supported-accession", claim_text: "Microsoft filing accession is 0001193125-26-323660.", claim_type: "metadata", ticker: "MSFT", evidence_ids: [evidenceId], metadata: { field: "accession_number", value: "0001193125-26-323660" } },
	];
	if (claims.some((claim) => claim.claim_type === "narrative")) throw new Error("Seed must not contain narrative claims; semantic judge must not be reachable");
	const verified = await postTool(url, "verify_citations", { ticker: "MSFT", claims });
	const verdicts = verified.report?.verifications ?? [];
	const expected = ["supported", "unsupported", "supported"];
	if (verdicts.length !== expected.length || verdicts.some((item, i) => item.verdict !== expected[i])) {
		throw new Error(`Unexpected deterministic seed verdicts: ${verdicts.map((item) => `${item.claim_id}=${item.verdict}`).join(", ")}`);
	}
	if (verdicts.some((item) => item.claim_type === "narrative")) {
		throw new Error("Seed unexpectedly contains a narrative verification; judge calls are prohibited");
	}
	if (typeof verified.report_draft_id !== "string" || !verified.report_draft_id.startsWith("draft_")) throw new Error("Citation verification did not return report_draft_id");
	return verified;
}

async function postTool(url, name, body) {
	const response = await fetch(`${url.replace(/\/$/, "")}/tools/${name}`, {
		method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
	});
	const data = await response.json();
	if (!response.ok) throw new Error(`${name} returned HTTP ${response.status}: ${JSON.stringify(data)}`);
	return data;
}

async function runRpc({ mode, url, sessionDir: sessions, piArgs: args, config: runtime, outputPath, onProcess }) {
	if (outputPath) await writeFile(outputPath, "", "utf8");
	const child = spawn(process.env.PI_BIN ?? "pi", args, { cwd: root, stdio: ["pipe", "pipe", "inherit"] });
	const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
	const events = [];
	const waiters = [];
	const observedTools = new Set();
	let modelState;
	let assistantText = "";
	let upstreamAssistantError;
	let recordTail = Promise.resolve();
	const record = async (line) => {
		if (outputPath) await appendFile(outputPath, `${line}\n`);
		let event;
		try { event = JSON.parse(line); } catch { return; }
		events.push(event);
		if (event.type === "tool_execution_start") observedTools.add(event.toolName);
		if (event.type === "agent_end") {
			upstreamAssistantError ??= assistantErrorMessage(event);
			assistantText += extractAssistantText(event.messages);
		}
		if (event.type === "message_end") upstreamAssistantError ??= assistantErrorMessage(event);
		for (let i = waiters.length - 1; i >= 0; i--) {
			if (waiters[i].predicate(event)) {
				const [waiter] = waiters.splice(i, 1);
				waiter.resolve(event);
			}
		}
	};
	lines.on("line", (line) => { recordTail = recordTail.then(() => record(line)); });
	child.once("error", (error) => { for (const waiter of waiters.splice(0)) waiter.reject(error); });
	onProcess(child, { events });
	await waitForRpcResponse(child, waiters, events, "discovery-prompt", {
		type: "prompt", message: "List the names of all tools available to you that come from the financial-research extension. Do not call any tools.",
	});
	await waitForEvent(waiters, events, (event) => event.type === "agent_settled", "agent_settled");
	await recordTail;
	assertNoAssistantError(events);
	if (upstreamAssistantError !== undefined) throw new Error(upstreamAssistantError);
	const stateResponse = await requestRpc(child, waiters, events, "get_state");
	modelState = stateResponse.data;
	const activeTools = activeToolsFromState(modelState);
	assertActiveTools(modelState, runtime.pi_tool_allowlist);
	await appendFile(outputPath, `${JSON.stringify({ type: "smoke_summary", active_tools: activeTools })}\n`);
	const textResponse = await requestRpc(child, waiters, events, "get_last_assistant_text");
	assistantText ||= textResponse.data?.text ?? "";
	const stats = await requestRpc(child, waiters, events, "get_session_stats");
	if (modelState?.model?.provider !== runtime.provider || modelState?.model?.id !== runtime.model_id || modelState?.thinkingLevel !== runtime.thinking_level) {
		throw new Error(`Configured/observed Pi model mismatch: configured ${runtime.provider}/${runtime.model_id} ${runtime.thinking_level}; observed ${modelState?.model?.provider}/${modelState?.model?.id} ${modelState?.thinkingLevel}`);
	}
	const missing = runtime.pi_tool_allowlist.filter((name) => !assistantText.includes(name));
	if (missing.length) throw new Error(`Discovery reply omitted tool names: ${missing.join(", ")}`);
	if (observedTools.size) throw new Error(`Discovery prompt unexpectedly called tools: ${[...observedTools].join(", ")}`);
	console.log(`Observed model: ${modelState?.model?.provider ?? "unknown"}/${modelState?.model?.id ?? "unknown"} (${modelState?.thinkingLevel ?? "thinking level unreported"})`);
	console.log(`Discovery reply lists all 8 tools; no tool calls observed. Event stream: ${path.relative(root, outputPath)}`);
	console.log(`Session usage: ${JSON.stringify(stats?.data?.tokens ?? null)}; cost: ${JSON.stringify(stats?.data?.cost ?? null)}`);
	await finishChild(child);
}

async function runExportRpc({ url, sessionDir: sessions, piArgs: args, config: runtime, reportName, reportPath, draftId, unsupportedClaimText, onProcess }) {
	const child = spawn(process.env.PI_BIN ?? "pi", args, { cwd: root, stdio: ["pipe", "pipe", "inherit"] });
	const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
	const events = [];
	const waiters = [];
	const confirmations = [];
	const observedTools = new Set();
	let recordTail = Promise.resolve();
	const record = async (line) => {
		let event;
		try { event = JSON.parse(line); } catch { return; }
		events.push(event);
		if (event.type === "tool_execution_start") observedTools.add(event.toolName);
		if (event.type === "extension_ui_request") {
			recordConfirmRequest(event, confirmations, (response) => child.stdin.write(`${JSON.stringify(response)}\n`));
		}
		for (let i = waiters.length - 1; i >= 0; i--) if (waiters[i].predicate(event)) waiters.splice(i, 1)[0].resolve(event);
	};
	lines.on("line", (line) => { recordTail = recordTail.then(() => record(line)); });
	child.once("error", (error) => { for (const waiter of waiters.splice(0)) waiter.reject(error); });
	onProcess(child);
	await waitForRpcResponse(child, waiters, events, "export-prompt", {
		type: "prompt",
		message: `Call calculate_financial_metric for MSFT operating_margin fiscal year 2026. Then call export_research_report for report draft ${draftId}, format markdown, output_path ${reportName}. Follow the confirmation prompts.`,
	});
	await waitForEvent(waiters, events, (event) => event.type === "agent_settled", "agent_settled");
	await recordTail;
	assertNoAssistantError(events);
	const stateResponse = await requestRpc(child, waiters, events, "get_state");
	const modelState = stateResponse.data;
	const activeTools = activeToolsFromState(modelState);
	assertActiveTools(modelState, runtime.pi_tool_allowlist);
	if (modelState?.model?.provider !== runtime.provider || modelState?.model?.id !== runtime.model_id || modelState?.thinkingLevel !== runtime.thinking_level) {
		throw new Error(`Configured/observed Pi model mismatch: configured ${runtime.provider}/${runtime.model_id} ${runtime.thinking_level}; observed ${modelState?.model?.provider}/${modelState?.model?.id} ${modelState?.model?.thinkingLevel}`);
	}
	const stats = await requestRpc(child, waiters, events, "get_session_stats");
	await recordTail;
	const fileBytes = await readFile(reportPath);
	const reportHash = createHash("sha256").update(fileBytes).digest("hex");
	const reportText = fileBytes.toString("utf8");
	const writeEnd = events.find((event) => event.type === "tool_execution_end" && event.toolName === "export_research_report");
	const details = writeEnd?.result?.details ?? writeEnd?.details;
	const resultObject = typeof details === "string" ? parseJsonObject(details) : details;
	const expectedReportHash = resultObject?.written?.content_sha256 ?? resultObject?.content_sha256;
	const executed = assertExportSmoke({ events, confirmations, reportText, reportHash, expectedReportHash,
		unsupportedClaimText, allowlist: runtime.pi_tool_allowlist });
	await finishChild(child);
	return {
		observedModel: { provider: modelState?.model?.provider, id: modelState?.model?.id, thinkingLevel: modelState?.thinkingLevel },
		toolsExecuted: [...observedTools], confirmations, reportHash, stats,
	};
}

function parseJsonObject(value) {
	try { return JSON.parse(value); } catch { return undefined; }
}

async function finishChild(child) {
	child.stdin.end();
	const [code] = await Promise.race([waitForExit(child), delay(5000).then(() => [null])]);
	if (code === null) child.kill("SIGTERM");
	if (code !== null && code !== 0) throw new Error(`Pi RPC exited with code ${code}`);
}

async function waitForRpcResponse(child, waiters, events, id, command) {
	const waiting = waitForEvent(waiters, events, (event) => event.type === "response" && event.id === id, `response ${id}`);
	child.stdin.write(`${JSON.stringify({ id, ...command })}\n`);
	const result = await waiting;
	if (!result.success) throw new Error(`Pi RPC command ${id} failed: ${JSON.stringify(result)}`);
}

async function requestRpc(child, waiters, events, command) {
	const id = `smoke-${command}`;
	const waiting = waitForEvent(waiters, events, (event) => event.type === "response" && event.id === id, `response ${command}`);
	child.stdin.write(`${JSON.stringify({ id, type: command })}\n`);
	const result = await waiting;
	if (!result.success) throw new Error(`Pi RPC command ${command} failed: ${JSON.stringify(result)}`);
	return result;
}

function waitForEvent(waiters, events, predicate, label) {
	const prior = events.find(predicate);
	if (prior) return Promise.resolve(prior);
	return new Promise((resolve, reject) => {
		waiters.push({ predicate, resolve, reject, label });
		setTimeout(() => {
			const index = waiters.findIndex((item) => item.label === label);
			if (index >= 0) waiters.splice(index, 1)[0].reject(new Error(`Timed out waiting for ${label}`));
		}, 180_000).unref();
	});
}

async function waitForBackend(url, child, stderr) {
	const deadline = Date.now() + 30_000;
	while (Date.now() < deadline) {
		if (child.exitCode !== null) throw new Error(`Backend exited before startup: ${stderr()}`);
		try {
			const response = await fetch(`${url.replace(/\/$/, "")}/health`, { signal: AbortSignal.timeout(1000) });
			if (response.ok) return;
		} catch { /* wait for local server */ }
		await delay(250);
	}
	throw new Error(`Timed out starting backend: ${stderr()}`);
}

async function fileSize(filePath) {
	try { return (await stat(filePath)).size; } catch (error) { if (error.code === "ENOENT") return 0; throw error; }
}

async function readTraceRows(filePath, offset) {
	const buffer = await readFile(filePath);
	return buffer.subarray(offset).toString("utf8").split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

function extractAssistantText(messages) {
	if (!Array.isArray(messages)) return "";
	return messages.filter((message) => message.role === "assistant")
		.flatMap((message) => Array.isArray(message.content) ? message.content : [])
		.filter((part) => part.type === "text").map((part) => part.text ?? "").join("\n");
}

function assistantErrorMessage(event) {
	const messages = event?.type === "agent_end" ? event.messages : [event?.message ?? event];
	if (!Array.isArray(messages)) return undefined;
	const failed = messages.find((message) => message?.role === "assistant" && message.stopReason === "error");
	return typeof failed?.errorMessage === "string" ? failed.errorMessage : undefined;
}

function delay(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

function waitForExit(child) {
	if (child.exitCode !== null) return Promise.resolve([child.exitCode, child.signalCode]);
	return once(child, "exit");
}
