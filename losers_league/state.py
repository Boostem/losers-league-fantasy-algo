import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


STATE_DIR = os.path.join(os.getcwd(), "state")


@dataclass
class Pick:
    week: int
    team: str
    opponent: Optional[str] = None
    result: str = "pending"  # pending | win | loss | tie


@dataclass
class ProfileState:
    profile: str
    season: int
    saves_total: int = 0
    saves_left: int = 0
    eliminated: bool = False
    used: Set[str] = field(default_factory=set)
    picks: Dict[str, Pick] = field(default_factory=dict)  # key: week as str

    @property
    def path(self) -> str:
        os.makedirs(STATE_DIR, exist_ok=True)
        return os.path.join(STATE_DIR, f"{self.profile}_{self.season}.json")

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "season": self.season,
            "saves_total": self.saves_total,
            "saves_left": self.saves_left,
            "eliminated": self.eliminated,
            "used": sorted(list(self.used)),
            "picks": {k: vars(v) for k, v in self.picks.items()},
        }

    @staticmethod
    def from_dict(d: dict) -> "ProfileState":
        ps = ProfileState(
            profile=d.get("profile"),
            season=int(d.get("season")),
            saves_total=int(d.get("saves_total", 0)),
            saves_left=int(d.get("saves_left", 0)),
            eliminated=bool(d.get("eliminated", False)),
            used=set([u.upper() for u in d.get("used", [])]),
            picks={},
        )
        for k, v in (d.get("picks", {}) or {}).items():
            ps.picks[str(k)] = Pick(week=int(v.get("week")), team=v.get("team").upper(), opponent=v.get("opponent"), result=v.get("result", "pending"))
        return ps


def load_state(profile: str, season: int) -> ProfileState:
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"{profile}_{season}.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return ProfileState.from_dict(data)
    return ProfileState(profile=profile, season=season)


def save_state(ps: ProfileState) -> None:
    with open(ps.path, "w", encoding="utf-8") as f:
        json.dump(ps.to_dict(), f, indent=2, sort_keys=True)

