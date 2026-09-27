import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

const extensionRoot = path.resolve(".pi/extensions/financial-research");
const forbiddenEnvironmentAccess = /(?:process\.env|Bun\.env|Deno\.env|import\.meta\.env)(?!\.FRA_API_URL\b)/g;

test("extension source reads only process.env.FRA_API_URL", async () => {
	const files = await sourceFiles(extensionRoot);
	const violations = [];
	for (const file of files) {
		const source = await readFile(file, "utf8");
		for (const match of source.matchAll(forbiddenEnvironmentAccess)) violations.push(`${path.relative(extensionRoot, file)}:${lineAt(source, match.index)}`);
	}
	assert.deepEqual(violations, []);
});

async function sourceFiles(dir) {
	const result = [];
	for (const entry of await readdir(dir, { withFileTypes: true })) {
		if (entry.isDirectory() && ["node_modules", "tests"].includes(entry.name)) continue;
		const full = path.join(dir, entry.name);
		if (entry.isDirectory()) result.push(...await sourceFiles(full));
		else if (entry.isFile() && full.endsWith(".ts")) result.push(full);
	}
	return result;
}

function lineAt(text, offset) { return text.slice(0, offset).split("\n").length; }
