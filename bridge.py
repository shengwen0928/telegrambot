import argparse
import json
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def ensure_cmd_exists(cmd_name: str) -> None:
    if shutil.which(cmd_name) is None:
        raise FileNotFoundError(f"Command not found in PATH: {cmd_name}")


def resolve_windows_command(cmd_name: str) -> str:
    # On Windows, `where` may return extension-less shims and .cmd files.
    # subprocess on a direct argv list is most reliable with explicit .cmd/.exe/.bat.
    candidates = [
        shutil.which(cmd_name),
        shutil.which(f"{cmd_name}.cmd"),
        shutil.which(f"{cmd_name}.exe"),
        shutil.which(f"{cmd_name}.bat"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise FileNotFoundError(f"Unable to resolve executable for: {cmd_name}")


def run_cmd(cmd: list[str], timeout_sec: int, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        input="",
        encoding="utf-8",
        errors="replace",
        timeout=timeout_sec,
        env=env,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def call_codex(
    codex_exe: str,
    prompt: str,
    timeout_sec: int,
    extra_args: str,
    global_args: str,
    retries: int,
) -> str:
    extra = shlex.split(extra_args)
    global_tokens = shlex.split(global_args)
    variants = [
        [codex_exe] + global_tokens + ["exec"] + extra + [prompt],
        [codex_exe, "exec"] + extra + [prompt],
    ]
    last_error = ""
    total_attempts = max(1, retries + 1)

    for attempt in range(1, total_attempts + 1):
        for cmd in variants:
            print(f"[bridge] codex cmd: {' '.join(cmd[:4])} ... (attempt {attempt}/{total_attempts})")
            try:
                rc, out, err = run_cmd(cmd, timeout_sec)
            except subprocess.TimeoutExpired:
                last_error = f"timed out after {timeout_sec}s"
                continue

            if rc == 0 and out:
                return out

            combined = (err or out or "").lower()
            if "unexpected argument '--ask-for-approval'" in combined:
                # This codex version doesn't support that global flag; try fallback variant.
                last_error = err or out or "unsupported global args"
                continue

            last_error = err or out or f"exit={rc}"

    raise RuntimeError(f"codex failed after retries: {last_error}")


def call_copilot_manual(prompt: str) -> str:
    print("\n=== Copilot Turn (manual relay) ===")
    print("請把以下 prompt 貼給 Copilot，並把 Copilot 回覆貼回來。")
    print("輸入完成後，單獨一行輸入 <<END>> 結束。")
    print("\n----- PROMPT FOR COPILOT -----")
    print(prompt)
    print("----- END PROMPT -----")

    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "<<END>>":
            break
        lines.append(line)
    output = "\n".join(lines).strip()
    if not output:
        raise RuntimeError("copilot(manual relay) returned empty output")
    return output


def trim_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated]..."


def build_forward_prompt(previous_speaker: str, text: str, max_reply_chars: int) -> str:
    return (
        f"你正在和另一個模型討論技術問題。"
        f"請回應對方觀點、補充或反駁，並保持具體。"
        f"回覆上限約 {max_reply_chars} 字。\n\n"
        f"上一位（{previous_speaker}）內容：\n{text}"
    )


def build_participants(mode: str, start_with: str) -> list[str]:
    if mode == "duo":
        participants = ["codex", "copilot"]
    else:
        participants = ["codex", "copilot"]

    if start_with not in participants:
        raise ValueError(f"--start-with={start_with} is not allowed in mode={mode}")

    idx = participants.index(start_with)
    return participants[idx:] + participants[:idx]


def append_log(log_path: Path, row: dict) -> None:
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Bridge Codex and Copilot (manual relay)")
    parser.add_argument("--seed", required=True, help="Initial question/topic")
    parser.add_argument("--rounds", type=int, default=4, help="Total rounds (default: 4)")
    parser.add_argument(
        "--mode",
        choices=["duo"],
        default="duo",
        help="duo: codex<->copilot(manual relay)",
    )
    parser.add_argument(
        "--start-with",
        choices=["codex", "copilot"],
        default="codex",
        help="Which model speaks first",
    )
    parser.add_argument("--timeout", type=int, default=90, help="Timeout per call in seconds")
    parser.add_argument(
        "--codex-extra-args",
        default="",
        help="Extra args for codex exec (leave empty by default for compatibility)",
    )
    parser.add_argument(
        "--codex-global-args",
        default="--ask-for-approval never",
        help="Global args placed before `exec` (auto-fallback if unsupported)",
    )
    parser.add_argument(
        "--codex-retries",
        type=int,
        default=1,
        help="Retry count when codex times out/fails (default: 1)",
    )
    parser.add_argument(
        "--max-forward-chars",
        type=int,
        default=1200,
        help="Max chars forwarded to the next model",
    )
    parser.add_argument(
        "--max-reply-chars",
        type=int,
        default=800,
        help="Hinted max reply chars in forwarding prompt",
    )
    parser.add_argument(
        "--log",
        default="bridge_log.jsonl",
        help="JSONL log path (default: bridge_log.jsonl)",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        default=True,
        help="Continue to next speaker when a model call fails or times out (default: enabled)",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Override and stop immediately on first model error",
    )
    args = parser.parse_args()

    if args.rounds < 1:
        raise ValueError("--rounds must be >= 1")

    ensure_cmd_exists("codex")
    codex_cmd = resolve_windows_command("codex")

    log_path = Path(args.log)
    current_prompt = args.seed
    participants = build_participants(args.mode, args.start_with)
    current_idx = 0

    print(
        f"[bridge] mode={args.mode}, start={participants[0]}, rounds={args.rounds}, "
        f"log={log_path}"
    )
    print(f"[bridge] resolved codex={codex_cmd}")
    continue_on_error = args.continue_on_error and not args.stop_on_error
    print(f"[bridge] continue_on_error={'1' if continue_on_error else '0'}")

    for i in range(1, args.rounds + 1):
        ts = datetime.now().isoformat(timespec="seconds")
        current_speaker = participants[current_idx]
        print(f"\n[bridge] round={i} speaker={current_speaker} calling...")
        try:
            if current_speaker == "codex":
                output = call_codex(
                    codex_cmd,
                    current_prompt,
                    args.timeout,
                    args.codex_extra_args,
                    args.codex_global_args,
                    args.codex_retries,
                )
            else:
                output = call_copilot_manual(current_prompt)
        except Exception as e:
            output = f"[{current_speaker} error] {e}"
            print(output)
            if not continue_on_error:
                raise

        print(f"\n--- Round {i} | {current_speaker} ---")
        print(output)

        append_log(
            log_path,
            {
                "time": ts,
                "round": i,
                "speaker": current_speaker,
                "prompt": current_prompt,
                "output": output,
            },
        )

        forwarded = trim_text(output, args.max_forward_chars)
        current_prompt = build_forward_prompt(current_speaker, forwarded, args.max_reply_chars)
        current_idx = (current_idx + 1) % len(participants)

    print("\n[bridge] completed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"[bridge] error: {e}", file=sys.stderr)
        raise SystemExit(1)
