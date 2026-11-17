"""Quét trace / kết quả / stdout tìm secret và PII. Công cụ dùng chung của nhóm CapNongCapNia.

Chạy trong thư mục `src/`:

    # 1) Quét thư mục traces/ đã có (sau khi chạy lab8.run, benchmark, pytest...)
    python -m scripts.trace_scan --traces traces

    # 2) Chạy lại các shape trên mọi claim public rồi quét result + trace + file <run_id>* + stdout
    python -m scripts.trace_scan --run workflow,single,orchestrator --seeds 11

    # 3) Bộ hidden tự kiểm (student_drop/ không có nhãn, scanner không cần nhãn)
    python -m scripts.trace_scan --run workflow,single,orchestrator --seeds 11 \
        --cases student_drop/hidden_claims.jsonl --world student_drop/hidden_world_state.json

Danh sách chuỗi cấm được dựng từ dữ liệu, không hard-code:

- claim: `parent_secret`, mọi giá trị trong `raw_headers`, `expected.forbid_substrings` (nếu có nhãn)
- order: `phone`, `address`, `internal_note`, `item_name`
- regex: `CANARY_SECRET_*`, `CANARY_INTERNAL_*`, `Bearer <token>`, số điện thoại VN 10 số
- tên field thô (`parent_secret`, `raw_headers`, `phone`, `address`, `internal_note`, `item_name`)
  xuất hiện như key JSON: dấu hiệu ghi nguyên claim row / order row vào trace.

Exit code 0 nếu sạch, 1 nếu có phát hiện. Scanner chỉ đọc, không ghi gì vào `reports/`.
Output không in lại giá trị bị lộ đầy đủ (chỉ in dạng che) để chính scanner không thành nơi rò rỉ.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

SRC = Path(__file__).resolve().parents[1]
DEFAULT_WORLD = SRC / "data" / "public_world_state.json"
DEFAULT_CASES = SRC / "data" / "public_claims.jsonl"

ORDER_PII_FIELDS = ("phone", "address", "internal_note", "item_name")
RAW_FIELD_KEYS = ("parent_secret", "raw_headers", "raw_headers_json", *ORDER_PII_FIELDS)

PATTERNS: dict[str, re.Pattern[str]] = {
    "canary_secret": re.compile(r"CANARY_SECRET_[A-Za-z0-9_\-]+"),
    "canary_internal": re.compile(r"CANARY_INTERNAL_[A-Za-z0-9_\-]+"),
    "bearer_token": re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]+"),
    "phone_pattern": re.compile(r"(?<!\d)0\d{9}(?!\d)"),
}
RAW_KEY_PATTERN = re.compile(r'"(' + "|".join(map(re.escape, RAW_FIELD_KEYS)) + r')"\s*:')


@dataclass(frozen=True)
class Finding:
    source: str  # file path, "result:<run_id>" hoặc "stdout"
    kind: str  # token | canary_secret | canary_internal | bearer_token | phone_pattern | raw_field
    label: str  # token lấy từ đâu (vd "O-107.phone"), hoặc tên regex / key
    preview: str  # giá trị đã che

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def mask(value: str) -> str:
    """Che giá trị để report không tự làm lộ secret: giữ 4 ký tự đầu."""
    value = str(value)
    if len(value) <= 4:
        return "*" * len(value)
    return value[:4] + "*" * min(len(value) - 4, 12)


def _load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def forbidden_tokens(world: dict[str, Any], claims: Iterable[dict[str, Any]]) -> dict[str, str]:
    """token -> label. Dựng từ world state và claims, nên dùng được cho cả bộ hidden."""
    tokens: dict[str, str] = {}

    def add(value: Any, label: str) -> None:
        if isinstance(value, str) and value.strip():
            tokens.setdefault(value.strip(), label)

    # Order trước để nhãn PII rõ nguồn (vd "O-107.phone" thay vì "C-0xx.forbid_substrings").
    for order in world.get("orders", []):
        oid = order.get("order_id", "?")
        for field in ORDER_PII_FIELDS:
            add(order.get(field), f"{oid}.{field}")
    for claim in claims:
        cid = claim.get("claim_id", "?")
        add(claim.get("parent_secret"), f"{cid}.parent_secret")
        for key, value in (claim.get("raw_headers") or {}).items():
            add(value, f"{cid}.raw_headers.{key}")
        for token in (claim.get("expected") or {}).get("forbid_substrings") or []:
            add(token, f"{cid}.forbid_substrings")
    return tokens


def _token_regex(token: str) -> re.Pattern[str]:
    # Biên từ để tên hàng ngắn (vd "Sach", "Ghe") không khớp nhầm bên trong từ khác.
    left = r"(?<![A-Za-z0-9_])" if token[0].isalnum() else ""
    right = r"(?![A-Za-z0-9_])" if token[-1].isalnum() else ""
    return re.compile(left + re.escape(token) + right)


class Scanner:
    def __init__(self, tokens: dict[str, str]) -> None:
        self.tokens = tokens
        self._compiled = [(tok, label, _token_regex(tok)) for tok, label in tokens.items()]

    @classmethod
    def from_files(cls, world_path: Path = DEFAULT_WORLD, cases_path: Path = DEFAULT_CASES) -> "Scanner":
        return cls(forbidden_tokens(_load_json(world_path), _load_jsonl(cases_path)))

    def scan_text(self, text: str, source: str) -> list[Finding]:
        found: dict[tuple[str, str], Finding] = {}
        for token, label, regex in self._compiled:
            if regex.search(text):
                found[("token", token)] = Finding(source, "token", label, mask(token))
        for kind, regex in PATTERNS.items():
            for match in regex.finditer(text):
                value = match.group(0)
                if any(value in tok or tok in value for tok in self.tokens if ("token", tok) in found):
                    continue  # đã báo qua token cụ thể
                found.setdefault((kind, value), Finding(source, kind, kind, mask(value)))
        for match in RAW_KEY_PATTERN.finditer(text):
            key = match.group(1)
            found.setdefault(("raw_field", key), Finding(source, "raw_field", key, key))
        return list(found.values())

    def scan_file(self, path: Path) -> list[Finding]:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        return self.scan_text(text, str(path))

    def scan_dir(self, directory: Path) -> list[Finding]:
        findings: list[Finding] = []
        for path in sorted(Path(directory).rglob("*")):
            if path.is_file():
                findings.extend(self.scan_file(path))
        return findings

    def scan_result(self, result: dict[str, Any]) -> list[Finding]:
        """RunResult dict + trace_path + mọi file <run_id>* cùng thư mục (giống grader)."""
        run_id = str(result.get("run_id") or "")
        findings = self.scan_text(json.dumps(result, ensure_ascii=False, default=str), f"result:{run_id}")
        for path in related_files(result):
            findings.extend(self.scan_file(path))
        return findings


def related_files(result: dict[str, Any]) -> list[Path]:
    files: list[Path] = []
    run_id = str(result.get("run_id") or "")
    folders = {Path("traces")}
    trace_path = result.get("trace_path")
    if trace_path:
        path = Path(trace_path)
        folders.add(path.parent)
        if path.is_file():
            files.append(path)
    if run_id:
        for folder in folders:
            if folder.is_dir():
                for path in sorted(folder.glob(f"{run_id}*")):
                    if path.is_file() and path not in files:
                        files.append(path)
    return files


def run_and_scan(
    shapes: list[str],
    seeds: list[int],
    *,
    world_path: Path = DEFAULT_WORLD,
    cases_path: Path = DEFAULT_CASES,
    workdir: Path | None = None,
) -> tuple[list[dict[str, Any]], list[Finding]]:
    """Chạy từng shape × claim × seed trên SQLite mới, quét result, trace, file phụ và stdout."""
    from lab8.app import prepare_store, run_shape

    claims = _load_jsonl(cases_path)
    scanner = Scanner(forbidden_tokens(_load_json(world_path), claims))
    rows: list[dict[str, Any]] = []
    findings: list[Finding] = []
    own_tmp = None
    if workdir is None:
        own_tmp = tempfile.TemporaryDirectory(prefix="trace-scan-", ignore_cleanup_errors=True)
        workdir = Path(own_tmp.name)
    old_cwd = os.getcwd()
    os.chdir(workdir)  # trace mặc định ghi vào ./traces → gom trong workdir
    try:
        for shape in shapes:
            for claim in claims:
                claim_id = claim["claim_id"]
                for seed in seeds:
                    store = Path(workdir) / f"{shape}-{claim_id}-{seed}.sqlite"
                    prepare_store(store, world_path, cases_path)
                    captured = io.StringIO()
                    row: dict[str, Any] = {"shape": shape, "claim_id": claim_id, "seed": seed}
                    try:
                        with contextlib.redirect_stdout(captured):
                            result = run_shape(shape, claim_id, seed=seed, store_path=str(store)).as_dict()
                    except Exception as exc:  # noqa: BLE001 — scanner ghi lại rồi chạy tiếp
                        row.update(status="error", error=f"{type(exc).__name__}: {str(exc)[:120]}", findings=0)
                        rows.append(row)
                        continue
                    trial = scanner.scan_result(result)
                    trial.extend(scanner.scan_text(captured.getvalue(), f"stdout:{result['run_id']}"))
                    row.update(status=result["status"], run_id=result["run_id"], findings=len(trial))
                    rows.append(row)
                    findings.extend(trial)
    finally:
        os.chdir(old_cwd)
        if own_tmp is not None:
            own_tmp.cleanup()
    return rows, findings


def _print_findings(findings: list[Finding]) -> None:
    for item in findings:
        print(f"[LEAK] {item.source} | {item.kind} | {item.label} | {item.preview}")


def main(argv: list[str] | None = None) -> int:
    with contextlib.suppress(AttributeError):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description="Quét trace/result/stdout tìm secret và PII")
    parser.add_argument("--traces", default=None, help="thư mục trace cần quét (mặc định: traces)")
    parser.add_argument("--run", default=None, help="chạy lại các shape, ví dụ workflow,single,orchestrator")
    parser.add_argument("--seeds", default="11")
    parser.add_argument("--world", default=str(DEFAULT_WORLD))
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--json", action="store_true", help="in kết quả dạng JSON")
    args = parser.parse_args(argv)

    world, cases = Path(args.world), Path(args.cases)
    if args.run:
        shapes = [s.strip() for s in args.run.split(",") if s.strip()]
        seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
        rows, findings = run_and_scan(shapes, seeds, world_path=world, cases_path=cases)
        errors = [row for row in rows if row["status"] == "error"]
        summary = {"trials": len(rows), "errors": len(errors), "findings": len(findings)}
    else:
        directory = Path(args.traces or "traces")
        if not directory.is_dir():
            print(f"không thấy thư mục {directory}")
            return 2
        findings = Scanner.from_files(world, cases).scan_dir(directory)
        rows = []
        summary = {"files": sum(1 for p in directory.rglob("*") if p.is_file()), "findings": len(findings)}

    if args.json:
        print(json.dumps({"summary": summary, "findings": [f.as_dict() for f in findings], "trials": rows},
                         ensure_ascii=False, indent=2))
    else:
        _print_findings(findings)
        for row in rows:
            if row["status"] == "error":
                print(f"[ERR ] {row['shape']} {row['claim_id']} s{row['seed']}: {row['error']}")
        verdict = "CLEAN" if not findings else "LEAK"
        print(f"trace_scan {verdict} | " + " | ".join(f"{k}={v}" for k, v in summary.items()))
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
