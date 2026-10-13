"""State 밖 저장소: 데이터셋 원문과 체크포인트.

State에는 작은 값만 둔다 (노션 C. State Schema — 지속성 비용·재개/복구).
- 데이터셋 원문(레코드 수백 건)은 파일로 저장하고, State에는 참조 `dataset_ref`만 둔다.
  체크포인트는 단계마다 State 전체를 저장하므로, 원문이 State에 있으면 단계 수만큼 복사된다.
- 참조 파일은 내용 해시로 이름을 지어 같은 데이터셋은 한 번만 쓰고, 재실행·재개 때 그대로 다시 읽는다.
- Worker 입력 계약(common_state.md 4장)은 dataset 본문을 요구하므로, Worker에 보내는 순간(Send)에만
  그 Worker 몫을 잘라 넣는다 (routing.slice_dataset).

dataset_ref = {"version": str, "uri": 파일 경로, "sha": 내용 해시 12자리, "records": n, "sources": n}
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[2]
STORE_ENV = "ORCH_DATASET_STORE"


def store_dir() -> Path:
    """기본 위치: 프로젝트 outputs/.dataset_store (환경변수 ORCH_DATASET_STORE로 변경)."""
    return Path(os.getenv(STORE_ENV) or ROOT / "outputs" / ".dataset_store")


def _digest(dataset: dict) -> tuple[str, bytes]:
    body = json.dumps(dataset, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(body).hexdigest()[:12], body


def put_dataset(dataset: dict, directory: Optional[Path] = None) -> dict:
    """데이터셋을 파일로 저장하고 참조를 돌려준다. 같은 내용이면 다시 쓰지 않는다."""
    sha, body = _digest(dataset)
    version = str(dataset.get("version") or "unversioned")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", version)[:80]
    path = (directory or store_dir()) / f"{name}-{sha}.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.replace(path)                                  # 중간에 끊겨도 반쯤 쓴 파일이 남지 않게
    return {"version": version, "uri": str(path), "sha": sha,
            "records": len(dataset.get("records") or []), "sources": len(dataset.get("sources") or [])}


@lru_cache(maxsize=128)
def _read(uri: str, sha: str) -> dict:
    data = json.loads(Path(uri).read_text(encoding="utf-8"))
    if _digest(data)[0] != sha:
        raise ValueError(f"데이터셋 파일 내용이 참조와 다름: {uri}")
    return data


def load_dataset(state: dict) -> dict:
    """State의 데이터셋 본문. 참조(dataset_ref)가 있으면 파일에서 읽고, 없으면 인라인 dataset(테스트·하위 호환)."""
    ref = state.get("dataset_ref")
    if ref:
        return _read(ref["uri"], ref["sha"])
    return state.get("dataset") or {}


def dataset_version(state: dict) -> Optional[str]:
    ref = state.get("dataset_ref")
    return ref["version"] if ref else (state.get("dataset") or {}).get("version")


def externalize(bundles: Iterable[dict], directory: Optional[Path] = None) -> list[dict]:
    """기업 입력 목록의 dataset 본문을 저장소로 옮기고 dataset_ref로 바꾼다 (그래프에 넣기 전에 호출)."""
    out = []
    for b in bundles:
        b = dict(b)
        if b.get("dataset") is not None:
            b["dataset_ref"] = put_dataset(b.pop("dataset"), directory)
        out.append(b)
    return out


# ── 체크포인터 (재개/복구) ─────────────────────────────────────────────────
def make_checkpointer(path: Optional[Path]) -> Any:
    """path가 있으면 SQLite 파일 체크포인터(프로세스가 죽어도 남음). 패키지가 없으면 메모리 체크포인터로 대체.

    같은 thread_id로 다시 실행하면 끝난 단계는 건너뛰고 멈춘 지점부터 이어서 실행한다.
    """
    if path is None:
        return None
    try:
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError:
        from langgraph.checkpoint.memory import InMemorySaver
        print("[경고] langgraph-checkpoint-sqlite 미설치 → 메모리 체크포인터 사용 (프로세스 종료 시 재개 불가)")
        return InMemorySaver()
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))


def invoke_resumable(graph: Any, inputs: dict, config: dict) -> dict:
    """체크포인트에 이 thread_id의 미완료 실행이 있으면 이어서, 없으면 처음부터 실행한다."""
    if getattr(graph, "checkpointer", None) and config.get("configurable", {}).get("thread_id"):
        snapshot = graph.get_state(config)
        if snapshot.next:                                   # 다음에 실행할 노드가 남아 있음 = 중단된 실행
            print(f"[재개] thread_id={config['configurable']['thread_id']} · 남은 노드 {list(snapshot.next)}")
            return graph.invoke(None, config)
        if snapshot.values:                                 # 이미 끝난 실행 → 저장된 결과 반환
            print(f"[완료된 실행] thread_id={config['configurable']['thread_id']} · 저장된 결과를 반환")
            return snapshot.values
    return graph.invoke(inputs, config)
