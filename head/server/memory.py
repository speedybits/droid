"""Persistent memory for the robot: transcript archive, rolling summary, durable facts.

Layout under the data dir (plain text, safe to edit or delete by hand):
  transcripts/YYYY-MM-DD.jsonl   append-only archive, one {ts, session, role, text[, room]} per line
  summary.md                     rolling summary of past sessions
  memory.md                      durable facts, one "- fact" bullet per line
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from head.jsonutil import parse_json

log = logging.getLogger("head.memory")

MAX_FACTS = 50
MAX_FACT_CHARS = 200
BRIEF_LINES = 10

SUMMARIZE_PROMPT = """You maintain the long-term memory of a small household robot named Droid.
Given the existing summary, the known facts and the latest conversation, reply with ONLY a JSON object:
{"summary": "<updated rolling summary, at most 250 words, plain prose>", "facts": ["<new durable fact>", ...]}
Facts are short, lasting things worth knowing next time (names, preferences, room layout, routines), not
one-off requests. Leave out facts already known. Keep the summary about what happened and what mattered.
Use they/them for people unless they stated their own pronouns; never guess from a name.
Some lines are tagged [room]. "heard" lines were overheard from people the robot could not identify (never
invent who said what); "droid" lines are what the robot itself said. Keep track of which room things happened in."""


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", s).strip().lower()


class MemoryStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._tdir = self.root / "transcripts"
        self._summarized: set[str] = set()

    # ---- transcript archive ----
    def log(self, session: str, role: str, text: str, room: str | None = None) -> None:
        now = datetime.now(timezone.utc)
        self._tdir.mkdir(parents=True, exist_ok=True)
        rec = {"ts": now.isoformat(timespec="seconds"), "session": session, "role": role, "text": text}
        if room:
            rec["room"] = room
        with open(self._tdir / f"{now:%Y-%m-%d}.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")

    def _records(self, files: int = 2) -> list[dict]:
        """Records from the newest few day files, oldest first (sessions rarely span more than midnight)."""
        if not self._tdir.exists():
            return []
        out = []
        for p in sorted(self._tdir.glob("*.jsonl"))[-files:]:
            for line in p.read_text().splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass  # a torn last line from a crash is not worth failing over
        return out

    def recent(self, n: int = 12) -> list[dict]:
        return [{"role": r["role"], "text": r["text"]} for r in self._records()[-n:]]

    def recall(self, room: str | None = None, per_room: int = 6) -> str:
        """What was overheard / said in each room (newest lines), plus the rolling summary.

        With `room`, that room is listed first and marked as where the robot is now.
        """
        by_room: dict[str, list[dict]] = {}
        for r in self._records(files=3):
            if r.get("room") and r["role"] in ("heard", "droid"):
                by_room.setdefault(r["room"], []).append(r)
        parts = []
        if self.summary():
            parts.append("Earlier (summary):\n" + self.summary())
        for name in sorted(by_room, key=lambda n: (n != room, n)):
            lines = by_room[name][-per_room:]
            label = f"{name} (you are here now)" if name == room else name
            parts.append(f"In the {label}:\n" + "\n".join(
                f"  {'you said' if r['role'] == 'droid' else 'overheard'}: {r['text']}" for r in lines))
        return "\n\n".join(parts)

    def session_lines(self, session: str) -> list[dict]:
        return [r for r in self._records() if r.get("session") == session]

    # ---- durable facts ----
    def facts(self) -> list[str]:
        p = self.root / "memory.md"
        if not p.exists():
            return []
        return [l[2:].strip() for l in p.read_text().splitlines() if l.startswith("- ") and l[2:].strip()]

    def add_fact(self, fact: str) -> bool:
        fact = " ".join(fact.split())[:MAX_FACT_CHARS]
        known = self.facts()
        if not fact or _norm(fact) in {_norm(k) for k in known}:
            return False
        known = (known + [fact])[-MAX_FACTS:]  # oldest facts fall off first
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "memory.md").write_text("".join(f"- {k}\n" for k in known))
        return True

    # ---- rolling summary ----
    def summary(self) -> str:
        p = self.root / "summary.md"
        return p.read_text().strip() if p.exists() else ""

    def _write_summary(self, text: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "summary.md").write_text(text.strip() + "\n")

    # ---- what the voice model / Claude are told ----
    def brief(self, session: str | None = None) -> str:
        parts = []
        if self.facts():
            parts.append("Things you know:\n" + "\n".join(f"- {f}" for f in self.facts()))
        if self.summary():
            parts.append("Earlier conversations:\n" + self.summary())
        if session:
            lines = self.session_lines(session)[-BRIEF_LINES:]
            if lines:
                parts.append("This conversation so far (you were disconnected briefly):\n"
                             + "\n".join(f"{r['role']}: {r['text']}" for r in lines))
        return "\n\n".join(parts)

    async def summarize(self, llm, model: str, session: str) -> bool:
        """Fold one finished session into summary.md and memory.md. Idempotent per session."""
        lines = self.session_lines(session)
        if session in self._summarized or sum(r["role"] in ("user", "heard") for r in lines) < 2:
            return False
        self._summarized.add(session)
        convo = "\n".join(f"{r['role']}{' [' + r['room'] + ']' if r.get('room') else ''}: {r['text']}" for r in lines)
        facts = "\n".join(f"- {f}" for f in self.facts()) or "(none)"
        prompt = (f"Existing summary:\n{self.summary() or '(none)'}\n\nKnown facts:\n{facts}\n\n"
                  f"Latest conversation:\n{convo}")
        try:
            data = None
            for _ in range(2):  # a model occasionally returns nothing usable: one retry
                resp = await llm.messages.create(model=model, max_tokens=800, system=SUMMARIZE_PROMPT,
                                                 messages=[{"role": "user", "content": prompt}])
                try:
                    data = parse_json("".join(b.text for b in resp.content if b.type == "text"))
                    break
                except ValueError:
                    continue
            if data is None:
                raise ValueError("model returned no usable summary")
            if isinstance(data.get("summary"), str) and data["summary"].strip():
                self._write_summary(data["summary"])
            for f in data.get("facts") or []:
                if isinstance(f, str):
                    self.add_fact(f)
        except Exception:
            self._summarized.discard(session)  # allow a retry; the JSONL archive is intact either way
            log.exception("summarize failed")
            return False
        return True
