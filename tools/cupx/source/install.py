from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import os, string

@dataclass
class InstallProbe:
    path: Path
    score: int
    reasons: list[str]

def score_install(path: Path) -> InstallProbe:
    reasons, score = [], 0
    if not path.exists() or not path.is_dir():
        return InstallProbe(path, 0, ["Path does not exist"])
    names = {p.name.lower() for p in path.iterdir()}
    if "cuphead.exe" in names:
        score += 50; reasons.append("Cuphead.exe")
    data_dirs = [p for p in path.iterdir() if p.is_dir() and p.name.lower().endswith("_data")]
    if any(p.name.lower() == "cuphead_data" for p in data_dirs):
        score += 35; reasons.append("Cuphead_Data")
    elif data_dirs:
        score += 10; reasons.append("Unity-style *_Data directory")
    if "unityplayer.dll" in names:
        score += 10; reasons.append("UnityPlayer.dll")
    if any(n in names for n in ("gameassembly.dll",)):
        score += 5; reasons.append("GameAssembly.dll")
    return InstallProbe(path, score, reasons)

def candidate_paths() -> list[Path]:
    candidates = []
    env = os.environ
    roots = [
        Path(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        Path(env.get("ProgramFiles", r"C:\Program Files")),
    ]
    rels = [
        Path("Steam/steamapps/common/Cuphead"),
        Path("GOG Galaxy/Games/Cuphead"),
        Path("Cuphead"),
    ]
    for root in roots:
        for rel in rels:
            candidates.append(root / rel)
    # Common additional Steam libraries; bounded, no recursive drive crawl.
    for drive in string.ascii_uppercase:
        d = Path(f"{drive}:/")
        if d.exists():
            candidates += [
                d/"SteamLibrary/steamapps/common/Cuphead",
                d/"Games/Steam/steamapps/common/Cuphead",
            ]
    seen, out = set(), []
    for p in candidates:
        k = str(p).lower()
        if k not in seen:
            seen.add(k); out.append(p)
    return out

def auto_detect() -> list[InstallProbe]:
    found = [score_install(p) for p in candidate_paths()]
    return sorted([x for x in found if x.score >= 50], key=lambda x: x.score, reverse=True)
