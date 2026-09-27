#!/usr/bin/env python3
"""Small JSONL RPC stand-in for offline eval runner tests; never launches Pi."""
import json
import sys


def emit(value):
    print(json.dumps(value), flush=True)


tool_seq = 0


def tool(name, args, details, *, is_error=False):
    global tool_seq
    tool_seq += 1
    tool_call_id = f"tool-{tool_seq}"
    emit({"type": "tool_execution_start", "toolCallId": tool_call_id, "toolName": name, "args": args})
    emit({"type": "tool_execution_end", "toolCallId": tool_call_id, "toolName": name,
          "result": {"details": details, "isError": is_error}})


def stats():
    return {"cost": 0.123, "tokens": {"input": 100, "output": 50}}


mode = "happy"
for raw in sys.stdin:
    request = json.loads(raw)
    kind = request.get("type")
    if kind == "prompt":
        mode = request.get("message", "happy")
        if mode in {"stall", "ignore-abort"}:
            emit({"type": "message_start"})
            continue
        if mode in {"provider-error", "zero-tool-error"}:
            message = ("400 invalid_request_error: Third-party apps now draw from your extra usage. "
                       "Request ID: req_stub_123") if mode == "provider-error" else "stub runtime unavailable"
            emit({"type": "agent_end", "willRetry": False, "messages": [
                {"role": "assistant", "stopReason": "error", "errorMessage": message, "content": []}]})
            continue
        if mode == "auto-retry":
            emit({"type": "auto_retry_start", "attempt": 1, "delayMs": 250,
                  "errorMessage": "529 overloaded"})
            emit({"type": "auto_retry_end", "attempt": 1, "success": True,
                  "finalError": None, "reason": "provider recovered"})
        if mode in {"stderr-flood", "stderr-failure"}:
            sys.stderr.write("S" * 250_000 + "STDERR-END-MARKER\n")
            sys.stderr.flush()
        if mode == "typed-error":
            emit({"type": "tool_execution_start", "toolCallId": "typed-error-call", "toolName": "calculate_financial_metric",
                  "args": {"metric": "revenue_growth", "fiscal_year": 2023}})
            emit({"type": "tool_execution_end", "toolCallId": "typed-error-call", "toolName": "calculate_financial_metric",
                  "result": {"isError": True, "details": {}, "content": [{"type": "text",
                      "text": "fiscal_year_not_in_filing: fiscal year 2023 is unavailable (not in filing)"}]}})
            emit({"type": "agent_end", "willRetry": False, "messages": [
                {"role": "assistant", "stopReason": "stop",
                 "content": [{"type": "text", "text": "I can’t substantiate that from the pinned filing."}]}]})
            emit({"type": "agent_settled"})
            continue
        if mode == "parallel-metric":
            for call_id, year in (("metric-2024", 2024), ("metric-2025", 2025)):
                emit({"type": "tool_execution_start", "toolCallId": call_id,
                      "toolName": "calculate_financial_metric", "args": {"fiscal_year": year}})
            emit({"type": "tool_execution_end", "toolCallId": "metric-2025", "toolName": "calculate_financial_metric",
                  "result": {"isError": True, "details": {}, "content": [{"type": "text",
                      "text": "fiscal_year_not_in_filing: unavailable"}]}})
            emit({"type": "tool_execution_end", "toolCallId": "metric-2024", "toolName": "calculate_financial_metric",
                  "result": {"isError": False, "details": {"result": "0.31"}}})
            emit({"type": "agent_end", "willRetry": False, "messages": [
                {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "Completed."}]}]})
            emit({"type": "agent_settled"})
            continue
        if mode == "parallel-search-verify":
            emit({"type": "tool_execution_start", "toolCallId": "search-1", "toolName": "search_filing_chunks",
                  "args": {"company": "AAPL", "question": "q", "top_k": 4}})
            emit({"type": "tool_execution_start", "toolCallId": "verify-1", "toolName": "verify_citations",
                  "args": {"claims": []}})
            emit({"type": "tool_execution_end", "toolCallId": "verify-1", "toolName": "verify_citations",
                  "result": {"isError": False, "details": {"report": {"verifications": []},
                      "judge_observations": [{"observed_model": "openai/gpt-6-sol", "cost_usd": 0.02}]}}})
            emit({"type": "tool_execution_end", "toolCallId": "search-1", "toolName": "search_filing_chunks",
                  "result": {"isError": False, "details": {"evidence": [{"evidence_id": "ev-search"}]}}})
            emit({"type": "agent_end", "willRetry": False, "messages": [
                {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "Completed."}]}]})
            emit({"type": "agent_settled"})
            continue
        if mode == "missing-tool-end":
            emit({"type": "tool_execution_start", "toolCallId": "never-ended", "toolName": "calculate_financial_metric",
                  "args": {"fiscal_year": 2024}})
            emit({"type": "tool_execution_end", "toolCallId": "unmatched-result", "toolName": "calculate_financial_metric",
                  "result": {"isError": False, "details": {"result": "must not be assigned"}}})
            emit({"type": "agent_end", "willRetry": False, "messages": [
                {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "Completed."}]}]})
            emit({"type": "agent_settled"})
            continue
        tool("calculate_financial_metric", {"metric": "revenue"}, {"result": "100"})
        tool("verify_citations", {"claims": []}, {"report": {"verifications": []},
            "judge_observations": [{"observed_model": "openai/gpt-6-sol", "provider": "OpenAI",
                                    "reasoning_effort": "high", "cost_usd": 0.02,
                                    "prompt_tokens": 20, "completion_tokens": 10}]})
        if mode == "dialog":
            for method in ("confirm", "select", "input", "editor"):
                dialog_id = f"dlg-{method}"
                emit({"type": "extension_ui_request", "id": dialog_id, "method": method, "title": f"{method} dialog"})
                response = json.loads(sys.stdin.readline())
                valid_payload = response.get("confirmed") is False if method == "confirm" else response.get("cancelled") is True
                if response.get("type") != "extension_ui_response" or response.get("id") != dialog_id or not valid_payload:
                    sys.exit(3)
        if mode in {"final-error", "stderr-failure"}:
            emit({"type": "agent_end", "willRetry": False, "messages": [
                {"role": "assistant", "stopReason": "error", "errorMessage": "stub final failure", "content": []}]})
        elif mode == "hang":
            # Stay alive and answer the runner's bounded salvage stats request.
            continue
        else:
            if mode != "mismatch":
                emit({"type": "agent_end", "willRetry": True, "messages": [
                    {"role": "assistant", "stopReason": "error", "errorMessage": "retrying",
                     "content": [{"type": "text", "text": "Intermediate attempt must be discarded."}]}]})
            emit({"type": "agent_end", "willRetry": False, "messages": [
                {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "Final answer only."}]}]})
            emit({"type": "agent_settled"})
    elif kind == "get_state":
        model = {"provider": "anthropic", "id": "wrong-model"} if mode == "mismatch" else {
            "provider": "anthropic", "id": "claude-sonnet-5"}
        emit({"type": "response", "id": request["id"], "data": {"model": model, "thinkingLevel": "medium"}})
    elif kind == "get_session_stats":
        if mode != "no-stats":
            sys.stderr.write("STATS-REQUESTED\n")
            sys.stderr.flush()
            emit({"type": "response", "id": request["id"], "data": stats()})
    elif kind == "abort":
        if mode == "stall":
            sys.stderr.write("ABORT-RECEIVED\n")
            sys.stderr.flush()
            emit({"type": "response", "id": request["id"], "data": {"aborted": True}})
