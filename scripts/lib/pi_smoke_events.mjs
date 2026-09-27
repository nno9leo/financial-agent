export function assistantErrorMessage(event) {
	const messages = event?.type === "agent_end" ? event.messages : [event?.message ?? event];
	if (!Array.isArray(messages)) return undefined;
	const failed = messages.find((message) => message?.role === "assistant" && message.stopReason === "error");
	return typeof failed?.errorMessage === "string" ? failed.errorMessage : undefined;
}

export function assertNoAssistantError(events) {
	for (const event of events) {
		const error = assistantErrorMessage(event);
		if (error !== undefined) throw new Error(error);
	}
}

export function activeToolsFromState(state) {
	const candidates = [state?.activeTools, state?.toolNames, state?.tools, state?.data?.activeTools, state?.data?.toolNames, state?.data?.tools];
	for (const value of candidates) {
		if (Array.isArray(value) && value.every((item) => typeof item === "string")) return value;
		if (Array.isArray(value) && value.every((item) => item && typeof item.name === "string")) return value.map((item) => item.name);
	}
	return "not_reported_by_runtime";
}

export function assertActiveTools(state, allowlist) {
	const activeTools = activeToolsFromState(state);
	if (activeTools !== "not_reported_by_runtime" && !sameSet(activeTools, allowlist)) {
		throw new Error(`Active Pi tools differ from allowlist: ${activeTools.join(", ")}`);
	}
	return activeTools;
}

export function recordConfirmRequest(event, confirmations, respond) {
	if (event?.type !== "extension_ui_request") return false;
	if (event.method !== "confirm") {
		throw new Error(`Unsupported Pi dialog type: ${event.method ?? "unknown"}`);
	}
	const title = event.title ?? event.params?.title;
	const message = event.message ?? event.params?.message;
	if (typeof event.id !== "string" || typeof title !== "string" || typeof message !== "string") {
		throw new Error("Malformed Pi confirm request: expected id, title, and message");
	}
	confirmations.push({ title, message });
	respond({ type: "extension_ui_response", id: event.id, confirmed: true });
	return true;
}

export function assertExportSmoke({ events, confirmations, reportText, reportHash, expectedReportHash,
	unsupportedClaimText, allowlist }) {
	const executed = events.filter((event) => event.type === "tool_execution_start").map((event) => event.toolName);
	for (const required of ["calculate_financial_metric", "export_research_report"]) {
		if (!executed.includes(required)) throw new Error(`Required tool execution missing: ${required}`);
	}
	const outside = [...new Set(executed.filter((name) => !allowlist.includes(name)))];
	if (outside.length) throw new Error(`Non-allowlisted tool executed: ${outside.join(", ")}`);
	if (confirmations.length !== 2) throw new Error(`Expected exactly 2 confirm requests; observed ${confirmations.length}`);
	const countPrompt = /supported\s+claims?:\s*2\b/i.test(confirmations[0].message)
		&& /partially supported\s+claims?:\s*0\b/i.test(confirmations[0].message)
		&& /unsupported\s+claims?:\s*1\b/i.test(confirmations[0].message);
	if (!countPrompt) throw new Error("First confirm does not mention supported, partially supported, and unsupported counts");
	if (!/removed/i.test(`${confirmations[1].title}\n${confirmations[1].message}`)) throw new Error("Second confirm does not concern the Removed appendix");
	if (typeof expectedReportHash !== "string" || !expectedReportHash) throw new Error("Export tool result did not expose content_sha256");
	if (reportHash !== expectedReportHash) throw new Error(`Report hash mismatch: expected ${expectedReportHash}; observed ${reportHash}`);
	const findings = reportText.match(/## Findings\s*([\s\S]*?)(?=\n## |$)/i)?.[1] ?? "";
	if (findings.includes(unsupportedClaimText)) throw new Error("Unsupported claim appears in the Findings section");
	if (!reportText.includes("Removed — not supported by evidence")) throw new Error("Report is missing the Removed appendix");
	return executed;
}

export function assertExportTraceOrder(rows) {
	const actions = rows.filter((row) => row.tool?.startsWith("export_research_report"))
		.map((row) => row.tool.replace("export_research_report_", ""));
	if (JSON.stringify(actions) !== JSON.stringify(["preview", "preview", "write"])) {
		throw new Error(`Expected export trace preview → preview → write; observed ${actions.join(" → ")}`);
	}
	return actions;
}

function sameSet(a, b) { return a.length === b.length && a.every((item) => b.includes(item)) && b.every((item) => a.includes(item)); }
