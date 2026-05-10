#!/usr/bin/env python3
"""
vault 格式清洗(不翻译)批处理器。

跟 vault_ppt_translate.py 区别:
- 不要求 PPT 源——任何 md 都可候选,通过"格式问题信号"启发式判断
- PROMPT 严格不翻译——中文保中文/英文保英文,只清洗格式
- 跳过条件: frontmatter 含 processed: true 或 translated_at: 或 cleaned_at:
- 加 --scope-prefix 限定路径(如 "生活/10-19 生活管理")

格式问题信号(满足任一即候选):
1. slide marker (`<!-- Slide number:`)
2. 占位图密度 (`![Picture N]`、`![占位图]`、`![图片 N]` 占行 > 10%)
3. OCR 残骸密度 (单字符孤立行 > 5%、连续乱码段)
4. 栅格化空表格 (含 `|  |  |  |` 这种空白分隔行 > 3 行)
5. 单行碎片化 (50%+ 非空行长度 < 15 字符)

用法:
    python3 vault_format_clean.py --scope-prefix "生活/10-19 生活管理" --scan
    python3 vault_format_clean.py --scope-prefix "生活/10-19 生活管理" --backup
    python3 vault_format_clean.py --scope-prefix "生活/10-19 生活管理" --apply --workers 4
"""
from __future__ import annotations
import argparse
import fcntl
import json
import re
import subprocess
import sys
import tarfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# === 路径常量 ===
VAULT = Path("/Users/joshuaspc/Documents/Obsidian Vault")
BACKUP_DIR = VAULT / ".translation_backups"
WORK_DIR = Path(__file__).parent / "_format_clean"
PROMPT_PATH = WORK_DIR / "PROMPT.md"
EXCLUDE_PATH = WORK_DIR / "exclude_list.json"
CANDIDATES_PATH = WORK_DIR / "candidates.json"

# === 阈值 ===
MIN_INPUT_QUALITY = 0.85
MAX_INPUT_CHARS = 60_000
LLM_TIMEOUT = 360
MAX_SOURCE_SIZE = 5_000_000
MAX_FILE_SIZE = 60_000  # md 自身 > 60KB 跳过(LLM 处理 60K+ 持续 timeout)

# 格式问题信号阈值
SLIDE_MARKER_RE = re.compile(r"<!--\s*Slide number")
IMG_PLACEHOLDER_RE = re.compile(r"!\[(占位图|Picture \d+|图片 \d+|)\]")
EMPTY_TABLE_ROW_RE = re.compile(r"^\s*(\|\s*){4,}\|\s*$")
SHORT_LINE_THRESHOLD = 15  # 字符
SHORT_LINE_RATIO_TRIGGER = 0.5

# 跳过 frontmatter 字段(已处理)
SKIP_FRONTMATTER_KEYS = ("processed: true", "translated_at:", "cleaned_at:")


# ============================================================
# Frontmatter
# ============================================================

def split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        return "", text
    parts = text.split("\n---\n", 1)
    if len(parts) != 2:
        return "", text
    fm = parts[0] + "\n---\n"
    body = parts[1].lstrip("\n")
    return fm, body


def parse_frontmatter(text: str) -> dict:
    fm, _ = split_frontmatter(text)
    if not fm:
        return {}
    result = {}
    for line in fm.split("\n"):
        line = line.strip()
        if line in ("---", "") or line.startswith("#"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def update_frontmatter(fm_block: str, updates: dict) -> str:
    if not fm_block:
        lines = ["---"] + [f'{k}: "{v}"' for k, v in updates.items()] + ["---"]
        return "\n".join(lines) + "\n"
    lines = fm_block.rstrip("\n").split("\n")
    if lines[-1] != "---":
        return fm_block
    existing_keys = {l.split(":", 1)[0].strip() for l in lines[1:-1] if ":" in l}
    insertions = [f'{k}: "{v}"' for k, v in updates.items() if k not in existing_keys]
    new_lines = lines[:-1] + insertions + [lines[-1]]
    return "\n".join(new_lines) + "\n"


# ============================================================
# 质量
# ============================================================

def quality_score(text: str) -> float:
    if not text:
        return 0.0
    recognizable = sum(
        1 for c in text
        if c.isalnum() or c.isspace()
        or '一' <= c <= '鿿'
        or '　' <= c <= '〿'
        or c in '.,;:!?\'"()[]{}#*-_/\\|`~@&%$^+=<>—–·'
    )
    return recognizable / len(text)


# ============================================================
# 格式问题信号
# ============================================================

def detect_format_issues(body: str) -> list[str]:
    """返回格式问题信号列表。空列表 = 无明显问题。"""
    issues = []
    head = body[:8000]

    if SLIDE_MARKER_RE.search(head):
        issues.append("slide_marker")

    lines = head.split("\n")
    non_empty = [l for l in lines if l.strip()]
    if not non_empty:
        return issues

    # 占位图密度
    img_lines = sum(1 for l in non_empty if IMG_PLACEHOLDER_RE.search(l))
    if img_lines / len(non_empty) > 0.10:
        issues.append(f"img_placeholder_{img_lines}")

    # 栅格化空表格
    empty_tbl_rows = sum(1 for l in lines if EMPTY_TABLE_ROW_RE.match(l))
    if empty_tbl_rows >= 3:
        issues.append(f"empty_table_{empty_tbl_rows}")

    # 单行碎片化(短行占比)
    short_lines = sum(1 for l in non_empty if len(l.strip()) < SHORT_LINE_THRESHOLD)
    if len(non_empty) >= 20 and short_lines / len(non_empty) > SHORT_LINE_RATIO_TRIGGER:
        issues.append(f"short_lines_{short_lines}of{len(non_empty)}")

    # 异体字 OCR 残骸
    if re.search(r"[⾼⾥⾃⼈⽇⼩⼤⽉⾼⾝⼯⼟⼿⼯⼩⽉⼿⽋⽣⽴]", head):
        issues.append("ocr_kangxi_radical")

    # 单字符孤立行(`q` `]` `*` 等)
    single_char = sum(1 for l in non_empty if len(l.strip()) == 1 and not l.strip().isalnum())
    if single_char >= 5:
        issues.append(f"single_char_lines_{single_char}")

    return issues


# ============================================================
# 排除
# ============================================================

def load_excludes() -> tuple[set, list]:
    if not EXCLUDE_PATH.exists():
        return set(), []
    data = json.loads(EXCLUDE_PATH.read_text(encoding="utf-8"))
    exact = set(data.get("exact_paths", []))
    exact |= set(data.get("duplicates_skip", []))
    return exact, data.get("path_prefixes", [])


def is_excluded(rel_path: str, exact_set: set, prefixes: list) -> bool:
    if rel_path in exact_set:
        return True
    return any(rel_path.startswith(p) for p in prefixes)


# ============================================================
# Step 1: 候选扫描
# ============================================================

def scan_candidates(scope_prefix: str | None) -> dict:
    exact_set, prefixes = load_excludes()
    candidates = []
    excluded = []

    if scope_prefix:
        scope_root = VAULT / scope_prefix
        if not scope_root.exists():
            print(f"scope 不存在: {scope_root}", file=sys.stderr)
            return {"error": f"scope_not_found: {scope_prefix}"}
    else:
        scope_root = VAULT
        scope_prefix = ""

    for md in sorted(scope_root.rglob("*.md")):
        if any(x in md.parts for x in [".obsidian", ".trash"]):
            continue
        rel = str(md.relative_to(VAULT))
        if md.name.startswith(("_bi_", "_ppt_", "_misclassified", "_format_")):
            continue

        if is_excluded(rel, exact_set, prefixes):
            excluded.append({"path": rel, "reason": "exclude_list"})
            continue

        # md 自身大小过滤(避免 LLM timeout)
        try:
            file_size = md.stat().st_size
        except Exception:
            file_size = 0
        if file_size > MAX_FILE_SIZE:
            excluded.append({"path": rel, "reason": f"file_too_large_{file_size}"})
            continue

        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            excluded.append({"path": rel, "reason": f"read_error: {e}"})
            continue

        # 已处理跳过
        head_fm = text[:2000]
        if any(k in head_fm for k in SKIP_FRONTMATTER_KEYS):
            reason = "already_processed"
            for k in SKIP_FRONTMATTER_KEYS:
                if k in head_fm:
                    reason = f"already_{k.split(':')[0]}"
                    break
            excluded.append({"path": rel, "reason": reason})
            continue

        fm, body = split_frontmatter(text)
        if len(body) < 200:
            excluded.append({"path": rel, "reason": "too_short"})
            continue

        meta = parse_frontmatter(text)
        try:
            src_size = int(meta.get("source_size", "0"))
        except ValueError:
            src_size = 0
        if src_size > MAX_SOURCE_SIZE:
            excluded.append({"path": rel, "reason": f"source_too_large_{src_size}"})
            continue

        # 输入质量
        q = quality_score(body[:60_000])
        if q < MIN_INPUT_QUALITY:
            excluded.append({"path": rel, "reason": f"low_quality_{q:.2f}"})
            continue

        # 格式问题信号
        issues = detect_format_issues(body)
        if not issues:
            excluded.append({"path": rel, "reason": "no_format_issues"})
            continue

        candidates.append({
            "path": rel,
            "size": md.stat().st_size,
            "input_quality": round(q, 3),
            "issues": issues,
        })

    return {
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "scope": scope_prefix,
        "candidates": candidates,
        "excluded": excluded,
        "stats": {
            "candidates_total": len(candidates),
            "excluded_total": len(excluded),
            "excluded_by_reason": _count_by(excluded, "reason"),
            "candidates_by_subdir": _count_by_subdir(candidates, scope_prefix),
        },
    }


def _count_by(items: list, key: str) -> dict:
    out = {}
    for x in items:
        v = x.get(key, "")
        kk = re.sub(r"_\d+(\.\d+)?(of\d+)?$", "", v)
        out[kk] = out.get(kk, 0) + 1
    return out


def _count_by_subdir(candidates: list, scope_prefix: str) -> dict:
    """按 scope 下的第一级(或第二级)子目录分组"""
    out = {}
    for c in candidates:
        rel = c["path"]
        if scope_prefix:
            if not rel.startswith(scope_prefix + "/"):
                continue
            tail = rel[len(scope_prefix) + 1:]
        else:
            tail = rel
        # 全 vault 模式:用前 2 级路径作分组(更细粒度)
        parts = tail.split("/", 2)
        if len(parts) >= 2 and not parts[0].startswith(("_", ".")):
            sub = f"{parts[0]}/{parts[1]}"
        else:
            sub = parts[0] if parts else "root"
        out[sub] = out.get(sub, 0) + 1
    return out


# ============================================================
# Step 2: tar 备份
# ============================================================

def backup_candidates(candidates: list, scope_tag: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    tar_path = BACKUP_DIR / f"vault_format_clean_{scope_tag}_{ts}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        for c in candidates:
            full = VAULT / c["path"]
            arc = f"format_clean/{c['path']}"
            tf.add(full, arcname=arc)
    return tar_path


# ============================================================
# Step 3: 单文件清洗
# ============================================================

def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def clean_one(rel_path: str) -> dict:
    md = VAULT / rel_path
    text = md.read_text(encoding="utf-8", errors="replace")
    fm, body = split_frontmatter(text)

    if any(k in fm for k in SKIP_FRONTMATTER_KEYS):
        return {"path": rel_path, "status": "skip", "reason": "already_processed"}

    body_truncated = body[:MAX_INPUT_CHARS]
    prompt = load_prompt() + "\n\n---\n\n以下是要处理的 markdown 内容:\n\n" + body_truncated

    t0 = time.time()
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=LLM_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"path": rel_path, "status": "fail", "reason": "timeout", "elapsed": LLM_TIMEOUT}

    elapsed = time.time() - t0
    if result.returncode != 0:
        return {
            "path": rel_path, "status": "fail", "reason": "nonzero_exit",
            "stderr": result.stderr[:2000], "elapsed": round(elapsed, 1),
        }

    out = result.stdout.strip()
    import os
    min_ratio = float(os.environ.get("CLEAN_MIN_OUT_RATIO", "0.3"))
    if len(out) < len(body_truncated) * min_ratio:
        return {
            "path": rel_path, "status": "fail", "reason": "output_too_short",
            "in_len": len(body_truncated), "out_len": len(out), "elapsed": round(elapsed, 1),
        }

    if out.startswith("---\n"):
        _, out = split_frontmatter(out)
        out = out.strip()

    # 剥离 ★ Insight 块(explanatory mode 泄漏)
    insight_re = re.compile(r"★\s*Insight\s*[─—\-]+.*?[─—\-]+\s*\n", flags=re.DOTALL)
    out = insight_re.sub("", out).lstrip()

    while out.startswith(("[STATE]", "[Tool Use]", "[State]")):
        lines = out.split("\n", 1)
        out = lines[1].lstrip() if len(lines) > 1 else ""

    for opener in ["下面是", "以下是", "Here is", "Here's", "好的", "处理后", "清洗完成", "我已完成"]:
        if out.startswith(opener):
            lines = out.split("\n", 1)
            if len(lines) > 1:
                out = lines[1].lstrip()

    new_fm = update_frontmatter(fm, {
        "cleaned_at": datetime.now().isoformat(timespec="seconds"),
        "cleaned_by": "claude (vault 格式清洗 v1)",
        "clean_prompt_version": "1.0",
    })

    md.write_text(new_fm + "\n" + out + "\n", encoding="utf-8")

    return {
        "path": rel_path, "status": "ok",
        "in_len": len(body_truncated), "out_len": len(out),
        "elapsed": round(elapsed, 1),
    }


# ============================================================
# Step 3 并发
# ============================================================

def append_jsonl(log_path: Path, record: dict):
    with open(log_path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def apply_batch(candidates: list, workers: int, limit: int | None) -> dict:
    if limit:
        candidates = candidates[:limit]

    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    log_dir = Path(f"/Users/joshuaspc/Documents/_logs/vault_format_clean/{ts}")
    log_dir.mkdir(parents=True, exist_ok=True)
    progress = log_dir / "progress.jsonl"

    print(f"批处理: {len(candidates)} 文件, {workers} workers")
    print(f"日志: {progress}")

    stats = {"ok": 0, "skip": 0, "fail": 0}
    fails = []
    t0 = time.time()

    with ProcessPoolExecutor(max_workers=workers) as ex:
        future_to_path = {ex.submit(clean_one, c["path"]): c["path"] for c in candidates}
        n = len(candidates)
        for i, fut in enumerate(as_completed(future_to_path), 1):
            try:
                rec = fut.result()
            except Exception as e:
                rec = {
                    "path": future_to_path[fut],
                    "status": "fail",
                    "reason": f"exception: {type(e).__name__}",
                    "error": str(e)[:500],
                }
            append_jsonl(progress, rec)
            stats[rec["status"]] += 1
            if rec["status"] == "fail":
                fails.append(rec)
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed else 0
            eta = (n - i) / rate if rate else 0
            print(
                f"  [{i:>3}/{n}] {rec['status']:5s} "
                f"ok={stats['ok']} skip={stats['skip']} fail={stats['fail']}  "
                f"{rate:.2f}/s  eta={eta/60:.1f}min  | {rec['path'][-60:]}",
                flush=True,
            )

    elapsed = time.time() - t0
    summary = log_dir / "summary.md"
    lines = [
        "# vault 格式清洗批处理 · 收尾报告",
        "",
        f"- 完成时间: {datetime.now().isoformat(timespec='seconds')}",
        f"- 候选数: {len(candidates)}",
        f"- 并发: {workers}",
        f"- 总耗时: {elapsed/60:.1f} min",
        "",
        "## 结果",
        "",
        "| 状态 | 数量 |",
        "|---|---|",
    ]
    for k in ("ok", "skip", "fail"):
        lines.append(f"| {k} | {stats[k]} |")
    if fails:
        lines += ["", f"## 失败 ({len(fails)})", ""]
        for f in fails[:50]:
            lines.append(f"- `{f['path']}` — {f.get('reason', '?')}")
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n汇总写入: {summary}")
    return stats


# ============================================================
# CLI
# ============================================================

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--scope-prefix", default=None, help="vault 内的 scope 前缀(如 '生活/10-19 生活管理');省略 = 全 vault")
    p.add_argument("--scan", action="store_true")
    p.add_argument("--backup", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--file", type=str, default=None)
    p.add_argument("--min-output-ratio", type=float, default=0.3)
    args = p.parse_args()

    if args.scan:
        result = scan_candidates(args.scope_prefix)
        if "error" in result:
            return 2
        CANDIDATES_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        s = result["stats"]
        print(f"候选 {s['candidates_total']} 个 / 排除 {s['excluded_total']} 个")
        print(f"排除原因: {s['excluded_by_reason']}")
        print(f"候选按子目录分布: {s['candidates_by_subdir']}")
        print(f"写入: {CANDIDATES_PATH}")
        return 0

    if args.backup:
        if not CANDIDATES_PATH.exists():
            print("先跑 --scan", file=sys.stderr)
            return 2
        data = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))
        scope_tag = re.sub(r"[^\w]", "_", args.scope_prefix or "all_vault")[:40]
        tar_path = backup_candidates(data["candidates"], scope_tag)
        print(f"备份: {tar_path}")
        return 0

    if args.apply:
        import os
        os.environ["CLEAN_MIN_OUT_RATIO"] = str(args.min_output_ratio)
        if args.file:
            print(f"单文件: {args.file}(min_output_ratio={args.min_output_ratio})")
            rec = clean_one(args.file)
            print(json.dumps(rec, ensure_ascii=False, indent=2))
            return 0 if rec["status"] == "ok" else 1
        if not CANDIDATES_PATH.exists():
            print("先跑 --scan", file=sys.stderr)
            return 2
        data = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))
        candidates = data["candidates"]
        stats = apply_batch(candidates, args.workers, args.limit)
        return 0 if stats["fail"] == 0 else 1

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
