import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const requiredSwitches = ["--no-extensions", "-na", "--no-skills", "--no-prompt-templates"];

export class PiIsolationError extends Error {
	constructor(message) {
		super(message);
		this.name = "PiIsolationError";
	}
}

export async function readRuntimeConfig(configPath = path.join(root, "config", "agent_runtime.json")) {
	return JSON.parse(await readFile(configPath, "utf8"));
}

export function buildPiArgv(config, sessionDir) {
	const args = ["--mode", "rpc", ...config.pi_cli_args, ...config.pi_isolation_args, "--session-dir", sessionDir];
	validatePiArgv(args, config);
	return args;
}

export function validatePiArgv(args, config) {
	if (!Array.isArray(args) || !Array.isArray(config.pi_tool_allowlist) || !Array.isArray(config.pi_cli_args)) {
		throw new PiIsolationError("Pi isolation config is malformed");
	}
	if (args.includes("-a") || args.includes("--approve")) {
		throw new PiIsolationError("Pi approval flag is forbidden");
	}
	const valuesFor = (flag) => args.flatMap((arg, i) => arg === flag ? [args[i + 1]] : []);
	const expectedTools = config.pi_tool_allowlist.join(",");
	if (valuesFor("--tools").length !== 1 || valuesFor("--tools")[0] !== expectedTools) {
		throw new PiIsolationError("Pi --tools must exactly match pi_tool_allowlist");
	}
	for (const flag of requiredSwitches) {
		if (args.filter((arg) => arg === flag).length !== 1) throw new PiIsolationError(`Pi isolation flag required exactly once: ${flag}`);
	}
	const extensions = valuesFor("--extension");
	if (extensions.length !== 1 || extensions[0] !== config.pi_extension_path) {
		throw new PiIsolationError("Pi must load exactly pi_extension_path");
	}
	if (!Array.isArray(config.pi_isolation_args) || !sameArray(config.pi_isolation_args, args.slice(2 + config.pi_cli_args.length, -2))) {
		throw new PiIsolationError("Pi argv isolation arguments differ from config");
	}
	if (args.at(-2) !== "--session-dir" || !args.at(-1)) throw new PiIsolationError("Pi argv must end with --session-dir <dir>");
	return true;
}

function sameArray(a, b) { return a.length === b.length && a.every((value, i) => value === b[i]); }
