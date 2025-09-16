import os
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..database import get_session
from ..db_models import AppSetting

router = APIRouter(prefix="/metadata", tags=["metadata"])

# Static catalog v1.0 used by frontend
COMPONENTS = [
    {
        "key": "tank", "code": "ta", "name": "Tank", "emoji": "🛡", "cooldown": 3, "color": "#3b82f6",
        "role": "Defense", "power": "shield()", "description": "Reduce 75% of next 40 dmg"
    },
    {"key": "sniper", "code": "sn", "name": "Sniper", "emoji": "🎯", "cooldown": 3, "color": "#ef4444",
     "role": "Damage", "power": "snipe(target)", "description": "40 dmg, range 5, LoS required"},
    {"key": "bomber", "code": "bo", "name": "Bomber", "emoji": "💣", "cooldown": 3, "color": "#f97316",
     "role": "AoE", "power": "explode()", "description": "30 AoE dmg (2-radius), includes self"},
    {"key": "scout", "code": "sc", "name": "Scout", "emoji": "🏃", "cooldown": 2, "color": "#06b6d4",
     "role": "Mobility", "power": "dash(dir)", "description": "Move 2 tiles straight, tiles must be empty"},
    {"key": "teleporter", "code": "te", "name": "Teleporter", "emoji": "✨", "cooldown": 4, "color": "#a855f7",
     "role": "Mobility", "power": "blink()", "description": "Jump to random empty tile"},
    {"key": "poisoner", "code": "po", "name": "Poisoner", "emoji": "💉", "cooldown": 3, "color": "#84cc16",
     "role": "Damage", "power": "infect(target)", "description": "10 dmg/turn for 3 turns (stackable)"},
    {"key": "trap_setter", "code": "ts", "name": "Trap Setter", "emoji": "🪤", "cooldown": 3, "color": "#f97316",
     "role": "Control", "power": "drop_trap()", "description": "Plants trap; 20 AoE dmg on trigger, lasts 5 turns"},
    {"key": "healer", "code": "he", "name": "Healer", "emoji": "🧬", "cooldown": 3, "color": "#22c55e",
     "role": "Support", "power": "heal(ally)", "description": "Restore 30 HP to ally within 2 tiles"},
    {"key": "shield_giver", "code": "sg", "name": "Shield Giver", "emoji": "🧲", "cooldown": 2, "color": "#10b981",
     "role": "Support", "power": "project_shield(ally)", "description": "Grants 1-turn shield to an ally (3-tile range)"},
    {"key": "puller", "code": "pu", "name": "Puller", "emoji": "🌀", "cooldown": 3, "color": "#4338ca",
     "role": "Control", "power": "yank(enemy)", "description": "Pulls visible enemy 1 tile closer (range 3, LoS)"},
    {"key": "bruiser", "code": "br", "name": "Bruiser", "emoji": "🦾", "cooldown": 0, "color": "#92400e",
     "role": "Melee", "power": "dash()", "description": "Passive: base attack = 30 dmg; has 2-tile dash"},
    {"key": "jammer", "code": "ja", "name": "Jammer", "emoji": "🛰", "cooldown": 3, "color": "#6b7280",
     "role": "Utility", "power": "scramble()", "description": "Nearby enemies (3-tile radius) 50% chance to miss for 2 turns"},
    {"key": "reflector", "code": "re", "name": "Reflector", "emoji": "🪞", "cooldown": 4, "color": "#d1d5db",
     "role": "Defense", "power": "mirror()", "description": "Reflects next attack (single use)"},
    {"key": "wall_builder", "code": "wb", "name": "Wall Builder", "emoji": "🧱", "cooldown": 3, "color": "#64748b",
     "role": "Utility", "power": "drop_wall()", "description": "Place temporary wall tile for 5 turns"},
    {"key": "pusher", "code": "ps", "name": "Pusher", "emoji": "🌀→", "cooldown": 2, "color": "#708090",
     "role": "Control", "power": "shove(enemy)", "description": "Push enemy back 1 tile (adjacent)"},
    {"key": "decoy_caster", "code": "dc", "name": "Decoy Caster", "emoji": "🐾", "cooldown": 4, "color": "#f5f5dc",
     "role": "Disruptor", "power": "clone()", "description": "Summons 1 HP decoy (blocks, 3 turns)"},
    {"key": "leaper", "code": "le", "name": "Leaper", "emoji": "🐸", "cooldown": 2, "color": "#14b8a6",
     "role": "Mobility", "power": "leap()", "description": "Jump diagonally 2 tiles over 1 bot/wall"},
    {"key": "silencer", "code": "si", "name": "Silencer", "emoji": "🔇", "cooldown": 3, "color": "#800000",
     "role": "Utility", "power": "silence(enemy)", "description": "Target enemy bot can't use power next 2 turns"},
]

@router.get("/components")
def get_components():
    return {"components": COMPONENTS} 


@router.get("/map_rules")
def get_map_rules():
    """Expose current map rules for QA (optional)."""
    try:
        from ..maps import load_rules
        r = load_rules()
        return {
            "grid_size": r.grid_size,
            "wall_density": r.wall_density,
            "forest_density": r.forest_density,
            "swamp_density": r.swamp_density,
            "ice_density": r.ice_density,
            "zone_counts": r.zone_counts,
            "spawn_margin": r.spawn_margin,
            "ensure_path_between_spawns": r.ensure_path_between_spawns,
        }
    except Exception:
        return {"error": "map rules unavailable"}


@router.get("/config")
async def get_public_config(session: AsyncSession = Depends(get_session)):
    """Public configuration for frontend: whether signup is enabled."""
    res = await session.execute(select(AppSetting).where(AppSetting.key == "signup_enabled"))
    row = res.scalar_one_or_none()
    enabled = True
    if row is not None:
        val = (row.value or "").strip().lower()
        enabled = val in ("1","true","yes","on")
    return {"signup_enabled": enabled}