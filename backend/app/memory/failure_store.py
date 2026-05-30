from __future__ import annotations
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import faiss
import numpy as np

from app.config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
  id            TEXT PRIMARY KEY,
  started_at    REAL NOT NULL,
  lang          TEXT NOT NULL DEFAULT 'en',
  transcript    TEXT NOT NULL,
  ticket_json   TEXT NOT NULL,
  eval_status   TEXT,
  eval_report   TEXT,
  latency_p50   REAL,
  latency_p95   REAL,
  screenshot_url TEXT
);

CREATE TABLE IF NOT EXISTS failures (
  id              TEXT PRIMARY KEY,
  call_id         TEXT NOT NULL,
  lang            TEXT NOT NULL DEFAULT 'en',
  failure_mode    TEXT NOT NULL,
  utterance       TEXT NOT NULL,
  wrong_output    TEXT NOT NULL,
  correct_output  TEXT NOT NULL,
  faiss_id        INTEGER NOT NULL,
  created_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_failures_lang ON failures(lang);
CREATE INDEX IF NOT EXISTS idx_failures_mode ON failures(failure_mode);

CREATE TABLE IF NOT EXISTS chat_decisions (
  id             TEXT PRIMARY KEY,
  viewer         TEXT NOT NULL,
  text           TEXT NOT NULL,
  injected_at    REAL NOT NULL,
  mode           TEXT,            -- 'TAKE' | 'ROAST' | 'ACK' | 'IGNORE' | NULL until decided
  response_text  TEXT,            -- Officer's spoken response (after tag strip)
  decided_at     REAL
);

CREATE INDEX IF NOT EXISTS idx_chat_decisions_pending
  ON chat_decisions(decided_at) WHERE decided_at IS NULL;

CREATE TABLE IF NOT EXISTS transcript_turns (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id    TEXT NOT NULL,
  role          TEXT NOT NULL,    -- 'assistant' | 'user' | 'game' | 'chat'
  text          TEXT NOT NULL,
  at            REAL NOT NULL,
  meta          TEXT              -- optional JSON sidecar
);

CREATE INDEX IF NOT EXISTS idx_transcript_at ON transcript_turns(at);
CREATE INDEX IF NOT EXISTS idx_transcript_session ON transcript_turns(session_id);

CREATE TABLE IF NOT EXISTS beats (
  id              TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL,
  started_at      REAL NOT NULL,
  ended_at        REAL NOT NULL,
  transcript      TEXT NOT NULL,   -- JSON: list of {role, text, at}
  chat_window     TEXT NOT NULL,   -- JSON: list of {viewer, text, mode, at}
  game_state      TEXT,            -- rendered context_store text
  eval_status     TEXT,            -- 'pass' | 'fail' | 'unknown'
  eval_score      REAL,
  eval_reasons    TEXT,            -- JSON list
  suggested_mode  TEXT,
  retrieved_n     INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_beats_session ON beats(session_id);
CREATE INDEX IF NOT EXISTS idx_beats_started ON beats(started_at);
"""

DIM = 384


@dataclass
class Failure:
    id: str
    call_id: str
    lang: str
    failure_mode: str
    utterance: str
    wrong_output: str
    correct_output: str
    faiss_id: int


class FailureStore:
    def __init__(self, db_path: str = settings.sqlite_path, faiss_path: str = settings.faiss_path):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

        self._faiss_path = faiss_path
        self._index = self._load_or_init_index()

    def _load_or_init_index(self) -> faiss.Index:
        if os.path.exists(self._faiss_path):
            idx = faiss.read_index(self._faiss_path)
            if idx.d == DIM:
                return idx
        return faiss.IndexFlatIP(DIM)

    def _persist(self):
        faiss.write_index(self._index, self._faiss_path)

    def record_call(
        self,
        call_id: str,
        lang: str,
        transcript: list[dict],
        ticket: dict,
        eval_status: Optional[str] = None,
        eval_report: Optional[str] = None,
        latency_p50: Optional[float] = None,
        latency_p95: Optional[float] = None,
        screenshot_url: Optional[str] = None,
    ) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO calls VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                call_id, time.time(), lang,
                json.dumps(transcript), json.dumps(ticket),
                eval_status, eval_report, latency_p50, latency_p95, screenshot_url,
            ),
        )
        self._db.commit()

    def add_failure(
        self,
        call_id: str,
        lang: str,
        failure_mode: str,
        utterance: str,
        wrong_output: str,
        correct_output: str,
        embedding: np.ndarray,
    ) -> Failure:
        faiss_id = self._index.ntotal
        vec = embedding.reshape(1, -1).astype("float32")
        self._index.add(vec)
        f = Failure(
            id=uuid.uuid4().hex,
            call_id=call_id,
            lang=lang,
            failure_mode=failure_mode,
            utterance=utterance,
            wrong_output=wrong_output,
            correct_output=correct_output,
            faiss_id=faiss_id,
        )
        self._db.execute(
            "INSERT INTO failures VALUES (?,?,?,?,?,?,?,?,?)",
            (f.id, f.call_id, f.lang, f.failure_mode, f.utterance,
             f.wrong_output, f.correct_output, f.faiss_id, time.time()),
        )
        self._db.commit()
        self._persist()
        return f

    # ---- chat decisions (TAKE/ROAST/ACK/IGNORE) ----------------------------

    def record_pending_chat(self, viewer: str, text: str) -> str:
        """Called by chat_bridge when a chat is enqueued for the Officer.
        Returns the chat_decision id. The mode is filled in later by
        link_chat_mode once the Officer's response is parsed."""
        cid = uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO chat_decisions (id, viewer, text, injected_at) VALUES (?,?,?,?)",
            (cid, viewer, text, time.time()),
        )
        self._db.commit()
        return cid

    def link_chat_mode(self, mode: str, response_text: str) -> Optional[str]:
        """Called by ChatModeParser when [mode=X] is detected in a response.
        Finds the oldest pending chat decision and stamps it with the mode +
        response. Returns the linked chat_decision id (or None if no pending)."""
        row = self._db.execute(
            "SELECT id FROM chat_decisions WHERE decided_at IS NULL "
            "ORDER BY injected_at ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        cid = row[0]
        self._db.execute(
            "UPDATE chat_decisions SET mode=?, response_text=?, decided_at=? WHERE id=?",
            (mode, response_text, time.time(), cid),
        )
        self._db.commit()
        return cid

    def recent_chat_decisions(self, limit: int = 20) -> list[dict]:
        cur = self._db.execute(
            "SELECT id, viewer, text, injected_at, mode, response_text, decided_at "
            "FROM chat_decisions ORDER BY injected_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": r[0], "viewer": r[1], "text": r[2], "injected_at": r[3],
                "mode": r[4], "response_text": r[5], "decided_at": r[6],
            }
            for r in cur
        ]

    # ---- failure retrieval -------------------------------------------------

    def search(
        self,
        query_embedding: np.ndarray,
        k: int = 3,
        lang: Optional[str] = None,
        min_score: float = 0.78,
    ) -> list[tuple[Failure, float]]:
        if self._index.ntotal == 0:
            return []
        vec = query_embedding.reshape(1, -1).astype("float32")
        D, I = self._index.search(vec, min(k * 3, self._index.ntotal))
        results: list[tuple[Failure, float]] = []
        for score, fid in zip(D[0].tolist(), I[0].tolist()):
            if score < min_score or fid < 0:
                continue
            row = self._db.execute(
                "SELECT id,call_id,lang,failure_mode,utterance,wrong_output,correct_output,faiss_id "
                "FROM failures WHERE faiss_id=?", (fid,)
            ).fetchone()
            if not row:
                continue
            f = Failure(*row)
            if lang and f.lang != lang:
                continue
            results.append((f, score))
            if len(results) >= k:
                break
        return results

    # ---- transcript turns (live log) --------------------------------------

    def record_transcript_turn(
        self,
        session_id: str,
        role: str,
        text: str,
        at: Optional[float] = None,
        meta: Optional[dict] = None,
    ) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._db.execute(
            "INSERT INTO transcript_turns (session_id, role, text, at, meta) "
            "VALUES (?,?,?,?,?)",
            (session_id, role, text, at or time.time(),
             json.dumps(meta) if meta else None),
        )
        self._db.commit()

    def recent_transcript(
        self,
        since: float = 0.0,
        limit: int = 200,
        session_id: Optional[str] = None,
    ) -> list[dict]:
        if session_id:
            cur = self._db.execute(
                "SELECT role, text, at FROM transcript_turns "
                "WHERE at > ? AND session_id = ? "
                "ORDER BY at ASC LIMIT ?",
                (since, session_id, limit),
            )
        else:
            cur = self._db.execute(
                "SELECT role, text, at FROM transcript_turns "
                "WHERE at > ? ORDER BY at ASC LIMIT ?",
                (since, limit),
            )
        return [{"role": r[0], "text": r[1], "at": r[2]} for r in cur.fetchall()]

    # ---- beats (Cekura-scored ~30s windows) -------------------------------

    def record_beat(
        self,
        beat_id: str,
        session_id: str,
        started_at: float,
        ended_at: float,
        transcript: list[dict],
        chat_window: list[dict],
        game_state: Optional[str],
        eval_status: Optional[str],
        eval_score: Optional[float],
        eval_reasons: Optional[list[str]],
        suggested_mode: Optional[str],
        retrieved_n: int = 0,
    ) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO beats VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                beat_id, session_id, started_at, ended_at,
                json.dumps(transcript), json.dumps(chat_window),
                game_state, eval_status, eval_score,
                json.dumps(eval_reasons or []),
                suggested_mode, retrieved_n,
            ),
        )
        self._db.commit()

    def recent_beats(self, since: float = 0.0, limit: int = 50) -> list[dict]:
        cur = self._db.execute(
            "SELECT id, session_id, started_at, ended_at, eval_status, "
            "eval_score, eval_reasons, suggested_mode, retrieved_n "
            "FROM beats WHERE started_at > ? "
            "ORDER BY started_at DESC LIMIT ?",
            (since, limit),
        )
        out = []
        for r in cur.fetchall():
            out.append({
                "id": r[0], "session_id": r[1],
                "started_at": r[2], "ended_at": r[3],
                "eval_status": r[4], "eval_score": r[5],
                "eval_reasons": json.loads(r[6]) if r[6] else [],
                "suggested_mode": r[7], "retrieved_n": r[8],
            })
        return out

    def metrics(self) -> dict:
        cur = self._db.execute(
            "SELECT eval_status, COUNT(*) FROM calls WHERE eval_status IS NOT NULL GROUP BY eval_status"
        ).fetchall()
        counts = {s: c for s, c in cur}
        total = sum(counts.values()) or 1
        modes = self._db.execute(
            "SELECT failure_mode, COUNT(*) FROM failures GROUP BY failure_mode"
        ).fetchall()
        lat = self._db.execute(
            "SELECT AVG(latency_p50), AVG(latency_p95) FROM calls WHERE latency_p50 IS NOT NULL"
        ).fetchone()
        chat_modes = self._db.execute(
            "SELECT mode, COUNT(*) FROM chat_decisions WHERE mode IS NOT NULL GROUP BY mode"
        ).fetchall()
        chat_total = self._db.execute(
            "SELECT COUNT(*) FROM chat_decisions"
        ).fetchone()[0]
        beat_rows = self._db.execute(
            "SELECT eval_status, COUNT(*) FROM beats WHERE eval_status IS NOT NULL "
            "GROUP BY eval_status"
        ).fetchall()
        beat_counts = {s: c for s, c in beat_rows}
        beat_total = sum(beat_counts.values()) or 0
        beat_pass = beat_counts.get("pass", 0)
        return {
            "total_calls": total,
            "pass_rate": counts.get("pass", 0) / total,
            "failure_modes": dict(modes),
            "latency_p50_avg_ms": (lat[0] or 0) * 1000,
            "latency_p95_avg_ms": (lat[1] or 0) * 1000,
            "chat_total": chat_total,
            "chat_modes": dict(chat_modes),
            "beat_total": beat_total,
            "beat_pass_rate": (beat_pass / beat_total) if beat_total else 0.0,
            "beat_status_counts": beat_counts,
        }
