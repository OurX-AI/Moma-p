import re
from pathlib import Path
from .constants import MAX_SKILL_FILE_BYTES, MAX_SKILL_TOTAL_BYTES
from .models import ScanFinding, ScanResult

THREAT_PATTERNS: list[tuple[str, str, str, str, str]] = [
    (r"ignore\s+(?:\w+\s+)*(previous|all|above|prior)\s+instructions", "prompt_injection_ignore", "critical", "injection", "prompt injection: ignore previous instructions"),
    (r"you\s+are\s+(?:\w+\s+)*now\s+", "role_hijack", "high", "injection", "attempts to override the agent role"),
    (r"disregard\s+(?:\w+\s+)*(your|all|any)\s+(?:\w+\s+)*(instructions|rules|guidelines)", "disregard_rules", "critical", "injection", "instructs agent to disregard rules"),
    (r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "env_exfil_curl", "critical", "exfiltration", "curl with secret env interpolation"),
    (r"wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "env_exfil_wget", "critical", "exfiltration", "wget with secret env interpolation"),
    (r"curl\s+[^\n]*\|\s*(ba)?sh", "curl_pipe_shell", "critical", "supply_chain", "curl piped to shell"),
    (r"wget\s+[^\n]*-O\s*-\s*\|\s*(ba)?sh", "wget_pipe_shell", "critical", "supply_chain", "wget piped to shell"),
    (r"rm\s+-rf\s+/", "destructive_root_rm", "critical", "destructive", "recursive delete from root"),
    (r"\bmkfs\b", "format_filesystem", "critical", "destructive", "formats a filesystem"),
    (r"authorized_keys", "ssh_backdoor", "critical", "persistence", "modifies SSH authorized keys"),
    (r"\bnc\s+-[lp]|ncat\s+-[lp]|\bsocat\b", "reverse_shell", "critical", "network", "potential reverse shell"),
    (r"\.\./\.\./", "path_traversal_deep", "high", "traversal", "deep relative path traversal"),
]

INSTALL_POLICY = {
    "builtin": ("allow", "allow", "allow"),
    "trusted": ("allow", "allow", "block"),
    "community": ("allow", "block", "block"),
}
VERDICT_INDEX = {"safe": 0, "caution": 1, "dangerous": 2}
TEXT_EXTENSIONS = {".md", ".txt", ".py", ".sh", ".bash", ".js", ".ts", ".json", ".yaml", ".yml", ".toml"}


def _verdict_from_findings(findings: list[ScanFinding]) -> str:
    if any(f.severity == "critical" for f in findings):
        return "dangerous"
    if any(f.severity == "high" for f in findings):
        return "caution"
    return "safe"


def _scan_text(rel_path: str, content: str) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    lines = content.splitlines()
    for line_no, line in enumerate(lines, start=1):
        for pattern, pattern_id, severity, category, description in THREAT_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                findings.append(
                    ScanFinding(
                        pattern_id=pattern_id,
                        severity=severity,
                        category=category,
                        file=rel_path,
                        line=line_no,
                        match=line.strip()[:160],
                        description=description,
                    )
                )
    return findings


def scan_skill_dir(skill_dir: Path, *, source: str, trust_level: str, skill_name: str) -> ScanResult:
    findings: list[ScanFinding] = []
    total_bytes = 0
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(skill_dir)).replace("\\", "/")
        if ".." in rel.split("/"):
            findings.append(
                ScanFinding("path_escape", "critical", "traversal", rel, 0, rel, "path escapes skill directory")
            )
            continue
        size = path.stat().st_size
        total_bytes += size
        if size > MAX_SKILL_FILE_BYTES:
            findings.append(
                ScanFinding("file_too_large", "high", "size", rel, 0, rel, f"file exceeds {MAX_SKILL_FILE_BYTES} bytes")
            )
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        findings.extend(_scan_text(rel, text))
    if total_bytes > MAX_SKILL_TOTAL_BYTES:
        findings.append(
            ScanFinding("skill_too_large", "high", "size", "SKILL.md", 0, "", f"skill exceeds {MAX_SKILL_TOTAL_BYTES} bytes total")
        )
    verdict = _verdict_from_findings(findings)
    summary = f"{len(findings)} finding(s); verdict={verdict}"
    return ScanResult(skill_name=skill_name, source=source, trust_level=trust_level, verdict=verdict, findings=findings, summary=summary)


def should_allow_install(result: ScanResult, *, force: bool = False) -> tuple[bool | None, str]:
    policy = INSTALL_POLICY.get(result.trust_level, INSTALL_POLICY["community"])
    idx = VERDICT_INDEX.get(result.verdict, 2)
    decision = policy[idx]
    if decision == "allow":
        return True, "allowed"
    if decision == "block":
        if force and result.verdict != "dangerous":
            return True, "forced"
        return False, f"blocked by policy ({result.verdict})"
    return False, "blocked"


def format_scan_report(result: ScanResult) -> str:
    lines = [f"Skill: {result.skill_name}", f"Verdict: {result.verdict}", f"Trust: {result.trust_level}", ""]
    if not result.findings:
        lines.append("No findings.")
        return "\n".join(lines)
    for f in result.findings[:20]:
        lines.append(f"[{f.severity}] {f.file}:{f.line} {f.pattern_id} — {f.description}")
    if len(result.findings) > 20:
        lines.append(f"... and {len(result.findings) - 20} more")
    return "\n".join(lines)
