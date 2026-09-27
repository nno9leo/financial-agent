import { isIP } from "node:net";

export interface ApiErrorBody {
	error: { code: string; message: string; fallback?: string | null; tool?: string | null };
}

export class BackendToolError extends Error {
	readonly code: string;
	readonly status: number;
	readonly fallback?: string;

	constructor(code: string, message: string, fallback: string | null | undefined, status: number) {
		super(`${code}: ${message}${fallback ? ` (${fallback})` : ""}`);
		this.name = "BackendToolError";
		this.code = code;
		this.status = status;
		this.fallback = fallback ?? undefined;
	}
}

export class BackendNotRunningError extends Error {
	readonly code = "backend_not_running";

	constructor(cause?: unknown) {
		super("backend_not_running: the local API is not running; start it with scripts/serve.py", { cause });
		this.name = "BackendNotRunningError";
	}
}

export type FetchLike = typeof fetch;

function normalizeBaseUrl(value: string): string {
	let url: URL;
	try {
		url = new URL(value);
	} catch (cause) {
		throw new BackendToolError("invalid_backend_url", "FRA_API_URL must be a valid HTTP URL", undefined, 0);
	}
	const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, "");
	const ipVersion = isIP(host);
	const loopback = host === "localhost" || host === "::1" ||
		(ipVersion === 4 && Number(host.split(".")[0]) === 127);
	if (url.protocol !== "http:" || !loopback || url.username || url.password || url.search || url.hash) {
		throw new BackendToolError("invalid_backend_url", "FRA_API_URL must target a loopback HTTP address", undefined, 0);
	}
	return url.toString().replace(/\/$/, "");
}

export class ApiClient {
	readonly baseUrl: string;
	readonly timeoutMs: number;
	private readonly fetchImpl: FetchLike;

	constructor(options: { baseUrl?: string; timeoutMs?: number; fetchImpl?: FetchLike } = {}) {
		this.baseUrl = normalizeBaseUrl(options.baseUrl ?? process.env.FRA_API_URL ?? "http://127.0.0.1:8765");
		this.timeoutMs = options.timeoutMs ?? 30_000;
		this.fetchImpl = options.fetchImpl ?? fetch;
	}

	async post<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
		let response: Response;
		try {
			response = await this.fetchImpl(`${this.baseUrl}${path}`, {
				method: "POST",
				headers: { "content-type": "application/json", accept: "application/json" },
				body: JSON.stringify(body),
				signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(this.timeoutMs)]) : AbortSignal.timeout(this.timeoutMs),
			});
		} catch (cause) {
			if (cause instanceof BackendToolError) throw cause;
			throw new BackendNotRunningError(cause);
		}
		let payload: unknown;
		try {
			payload = await response.json();
		} catch (cause) {
			if (!response.ok) throw new BackendToolError("invalid_error_response", `Backend returned HTTP ${response.status} with an unreadable body`, undefined, response.status);
			throw new BackendToolError("invalid_backend_response", "Backend returned invalid JSON", undefined, response.status);
		}
		if (!response.ok) {
			const envelope = payload as Partial<ApiErrorBody>;
			if (envelope?.error && typeof envelope.error.code === "string" && typeof envelope.error.message === "string") {
				throw new BackendToolError(envelope.error.code, envelope.error.message, envelope.error.fallback, response.status);
			}
			if (response.status === 404 || response.status === 501) {
				const unavailableMessage = typeof (payload as any)?.detail === "string" ? (payload as any).detail : "The backend endpoint is not available";
				throw new BackendToolError("endpoint_not_available", unavailableMessage, "This endpoint is not available in this version.", response.status);
			}
			throw new BackendToolError("invalid_error_response", `Backend returned HTTP ${response.status} without the standard error envelope`, undefined, response.status);
		}
		return payload as T;
	}

	async get<T>(path: string, signal?: AbortSignal): Promise<T> {
		let response: Response;
		try {
			response = await this.fetchImpl(`${this.baseUrl}${path}`, {
				method: "GET", headers: { accept: "application/json" },
				signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(this.timeoutMs)]) : AbortSignal.timeout(this.timeoutMs),
			});
		} catch (cause) {
			if (cause instanceof BackendToolError) throw cause;
			throw new BackendNotRunningError(cause);
		}
		let payload: unknown;
		try { payload = await response.json(); }
		catch (cause) {
			if (!response.ok) throw new BackendToolError("invalid_error_response", `Backend returned HTTP ${response.status} with an unreadable body`, undefined, response.status);
			throw new BackendToolError("invalid_backend_response", "Backend returned invalid JSON", undefined, response.status);
		}
		if (!response.ok) {
			const envelope = payload as Partial<ApiErrorBody>;
			if (envelope?.error && typeof envelope.error.code === "string" && typeof envelope.error.message === "string") {
				throw new BackendToolError(envelope.error.code, envelope.error.message, envelope.error.fallback, response.status);
			}
			throw new BackendToolError("invalid_error_response", `Backend returned HTTP ${response.status} without the standard error envelope`, undefined, response.status);
		}
		return payload as T;
	}
}

export function createApiClient(): ApiClient {
	return new ApiClient();
}
