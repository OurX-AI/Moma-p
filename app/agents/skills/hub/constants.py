from app.agents.contants import BUILTIN_SKILLS_DIR
from ..paths import EXCLUDED_SKILL_DIRS

HUB_DIR = BUILTIN_SKILLS_DIR / ".hub"
QUARANTINE_DIR = HUB_DIR / "quarantine"
LOCK_FILE = HUB_DIR / "lock.json"
TAPS_FILE = HUB_DIR / "taps.json"
AUDIT_LOG = HUB_DIR / "audit.log"
MAX_SKILL_FILE_BYTES = 1_048_576
MAX_SKILL_TOTAL_BYTES = 5_242_880
TRUSTED_REPOS = frozenset({
    "openai/skills",
    "anthropics/skills",
    "NousResearch/hermes-agent",
    "vercel-labs/agent-skills",
    "VoltAgent/awesome-agent-skills",
})