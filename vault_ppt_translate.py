#!/usr/bin/env python3
"""
vault 全局 PPT 转 Markdown 文件的清洗 + 翻译批处理器（通用版）。

基于 bi_translate_clean.py,改造点:
- VAULT 根扫全 vault 而不仅 BI_ROOT
- 工作目录 _ppt_translate/ 含通用领域 PROMPT
- exclude_list.json path_prefixes 排除归档/备份/已处理区

用法:
    python3 vault_ppt_translate.py --scan
    python3 vault_ppt_translate.py --backup
    python3 vault_ppt_translate.py --apply
    python3 vault_ppt_translate.py --apply --workers 4 --limit 5
    python3 vault_ppt_translate.py --apply --file "工作/.../xxx.md" --min-output-ratio 0.2
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
WORK_DIR = Path(__file__).parent / "_ppt_translate"
PROMPT_PATH = WORK_DIR / "PROMPT.md"
EXCLUDE_PATH = WORK_DIR / "exclude_list.json"
CANDIDATES_PATH = WORK_DIR / "candidates.json"

# === 阈值 ===
MIN_INPUT_QUALITY = 0.85
MAX_INPUT_CHARS = 60_000
LLM_TIMEOUT = 360
MAX_SOURCE_SIZE = 5_000_000
CN_RATIO_SKIP = 0.4
TABLE_PIPE_RATIO = 0.05
IMAGE_LINE_RATIO = 0.6


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


def cn_ratio(text: str) -> float:
    if not text:
        return 0.0
    return sum(1 for c in text if '一' <= c <= '鿿') / len(text)


# ============================================================
# 排除
# ============================================================

def load_excludes() -> tuple[set, list]:
    if not EXCLUDE_PATH.exists():
        return set(), []
    data = json.loads(EXCLUDE_PATH.read_text(encoding="utf-8"))
    exact = set(data.get("exact_paths", []))
    exact |= set(data.get("duplicates_skip", []))
    exact |= set(data.get("already_bilingual_skip", []))
    return exact, data.get("path_prefixes", [])


def is_excluded(rel_path: str, exact_set: set, prefixes: list) -> bool:
    if rel_path in exact_set:
        return True
    return any(rel_path.startswith(p) for p in prefixes)


# ============================================================
# Step 1: 候选扫描
# ============================================================

def scan_candidates() -> dict:
    exact_set, prefixes = load_excludes()
    candidates = []
    excluded = []

    for md in sorted(VAULT.rglob("*.md")):
        # 跳过 vault 内部隐藏目录
        if any(x in md.parts for x in [".obsidian", ".trash"]):
            continue
        rel = str(md.relative_to(VAULT))
        # 跳过日志文件
        if md.name.startswith("_bi_") or md.name.startswith("_misclassified") or md.name.startswith("_ppt_"):
            continue

        if is_excluded(rel, exact_set, prefixes):
            excluded.append({"path": rel, "reason": "exclude_list"})
            continue

        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            excluded.append({"path": rel, "reason": f"read_error: {e}"})
            continue

        # 已翻译
        if "translated_at:" in text[:1500]:
            excluded.append({"path": rel, "reason": "already_translated"})
            continue

        fm, body = split_frontmatter(text)
        if len(body) < 200:
            excluded.append({"path": rel, "reason": "too_short"})
            continue

        head = body[:5000]
        meta = parse_frontmatter(text)

        # PPT 源识别
        src_p = meta.get("source_path", "")
        if not (src_p.endswith(".ppt") or src_p.endswith(".pptx")
                or src_p.endswith('.ppt"') or src_p.endswith('.pptx"')):
            excluded.append({"path": rel, "reason": "not_ppt_source"})
            continue

        # 已是中文为主
        head_cn = cn_ratio(head)
        if head_cn > CN_RATIO_SKIP:
            # 仍可能含 slide marker 需清洗,但本批以"清洗+翻译"为主,已是中文跳过
            excluded.append({"path": rel, "reason": f"already_chinese_{head_cn:.2f}"})
            continue

        # 纯表格
        if head.count("|") / max(len(head), 1) > TABLE_PIPE_RATIO:
            excluded.append({"path": rel, "reason": "table_dominant"})
            continue

        # 纯图片残骸
        lines = head.split("\n")
        img_lines = sum(1 for l in lines if l.strip().startswith("!["))
        non_empty_lines = sum(1 for l in lines if l.strip())
        if non_empty_lines > 0 and img_lines / non_empty_lines > IMAGE_LINE_RATIO:
            excluded.append({"path": rel, "reason": "image_only_artifact"})
            continue

        # source_size > 5MB
        try:
            src_size = int(meta.get("source_size", "0"))
        except ValueError:
            src_size = 0
        if src_size > MAX_SOURCE_SIZE:
            excluded.append({"path": rel, "reason": f"source_too_large_{src_size}"})
            continue

        # 输入质量门槛
        q = quality_score(body[:60_000])
        if q < MIN_INPUT_QUALITY:
            excluded.append({"path": rel, "reason": f"low_quality_{q:.2f}"})
            continue

        candidates.append({
            "path": rel,
            "size": md.stat().st_size,
            "cn_ratio": round(head_cn, 3),
            "input_quality": round(q, 3),
            "source_size": src_size,
        })

    return {
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "vault_root": str(VAULT),
        "candidates": candidates,
        "excluded": excluded,
        "stats": {
            "candidates_total": len(candidates),
            "excluded_total": len(excluded),
            "excluded_by_reason": _count_by(excluded, "reason"),
        },
    }


def _count_by(items: list, key: str) -> dict:
    out = {}
    for x in items:
        v = x.get(key, "")
        # 截断含数字的(如 already_chinese_0.42 -> already_chinese)
        kk = re.sub(r"_\d+(\.\d+)?$", "", v)
        out[kk] = out.get(kk, 0) + 1
    return out


# ============================================================
# Step 2: tar 备份
# ============================================================

def backup_candidates(candidates: list) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    tar_path = BACKUP_DIR / f"vault_ppt_translate_{ts}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        for c in candidates:
            full = VAULT / c["path"]
            arc = f"vault_ppt/{c['path']}"
            tf.add(full, arcname=arc)
    return tar_path


# ============================================================
# Step 3: 单文件清洗+翻译
# ============================================================

def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def clean_and_translate_one(rel_path: str) -> dict:
    md = VAULT / rel_path
    text = md.read_text(encoding="utf-8", errors="replace")
    fm, body = split_frontmatter(text)

    if "translated_at:" in fm:
        return {"path": rel_path, "status": "skip", "reason": "already_translated"}

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
    min_ratio = float(os.environ.get("PPT_MIN_OUT_RATIO", "0.3"))
    if len(out) < len(body_truncated) * min_ratio:
        return {
            "path": rel_path, "status": "fail", "reason": "output_too_short",
            "in_len": len(body_truncated), "out_len": len(out), "elapsed": round(elapsed, 1),
        }

    if out.startswith("---\n"):
        _, out = split_frontmatter(out)
        out = out.strip()

    # 剥离 ★ Insight 块(explanatory mode 泄漏)
    # 块格式: 一行 ★ Insight ─*  ... 一行 ─*
    insight_re = re.compile(
        r"★\s*Insight\s*[─—\-]+.*?[─—\-]+\s*\n",
        flags=re.DOTALL,
    )
    out = insight_re.sub("", out).lstrip()

    # 剥离开头的 [STATE] / [Tool Use] 标签
    while out.startswith(("[STATE]", "[Tool Use]", "[State]")):
        lines = out.split("\n", 1)
        out = lines[1].lstrip() if len(lines) > 1 else ""

    for opener in ["下面是", "以下是", "Here is", "Here's", "好的", "处理后", "翻译完成", "我已完成"]:
        if out.startswith(opener):
            lines = out.split("\n", 1)
            if len(lines) > 1:
                out = lines[1].lstrip()

    new_fm = update_frontmatter(fm, {
        "translated_at": datetime.now().isoformat(timespec="seconds"),
        "translated_by": "claude (vault PPT 清洗+翻译 v1)",
        "translation_prompt_version": "1.0",
        "original_lang": "en",
    })

    md.write_text(new_fm + "\n" + out + "\n", encoding="utf-8")

    return {
        "path": rel_path, "status": "ok",
        "in_len": len(body_truncated), "out_len": len(out),
        "out_cn_ratio": round(cn_ratio(out[:5000]), 3),
        "elapsed": round(elapsed, 1),
    }


# ============================================================
# Step 3 并发与日志
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
    log_dir = Path(f"/Users/joshuaspc/Documents/_logs/vault_ppt_translate/{ts}")
    log_dir.mkdir(parents=True, exist_ok=True)
    progress = log_dir / "progress.jsonl"

    print(f"批处理: {len(candidates)} 文件, {workers} workers")
    print(f"日志: {progress}")

    stats = {"ok": 0, "skip": 0, "fail": 0}
    fails = []
    t0 = time.time()

    with ProcessPoolExecutor(max_workers=workers) as ex:
        future_to_path = {
            ex.submit(clean_and_translate_one, c["path"]): c["path"]
            for c in candidates
        }
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
        "# vault PPT 清洗+翻译批处理 · 收尾报告",
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
    p.add_argument("--scan", action="store_true")
    p.add_argument("--backup", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--file", type=str, default=None)
    p.add_argument("--min-output-ratio", type=float, default=0.3)
    args = p.parse_args()

    if args.scan:
        result = scan_candidates()
        CANDIDATES_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"候选 {result['stats']['candidates_total']} 个")
        print(f"排除 {result['stats']['excluded_total']} 个")
        print(f"排除原因分布: {result['stats']['excluded_by_reason']}")
        print(f"写入: {CANDIDATES_PATH}")
        return 0

    if args.backup:
        if not CANDIDATES_PATH.exists():
            print("先跑 --scan", file=sys.stderr)
            return 2
        data = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))
        tar_path = backup_candidates(data["candidates"])
        print(f"备份: {tar_path}")
        return 0

    if args.apply:
        import os
        os.environ["PPT_MIN_OUT_RATIO"] = str(args.min_output_ratio)
        if args.file:
            print(f"单文件重跑: {args.file}(min_output_ratio={args.min_output_ratio})")
            rec = clean_and_translate_one(args.file)
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
