import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]


class DemoRoutingConfig(BaseModel):
    shelf_unit_kopecks: int = Field(ge=0)
    direct_base_unit_kopecks: int = Field(ge=0)
    direct_trip_kopecks: int = Field(ge=0)
    direct_total_individual_delay_kopecks: int = Field(ge=0)
    hub_unit_kopecks: int = Field(ge=0)
    retained_hub_unit_kopecks: int = Field(ge=0)
    trip_orders: int = Field(gt=0)
    shelf_minutes: int = Field(gt=0)
    direct_minutes: int = Field(gt=0)
    hub_minutes: int = Field(gt=0)


class PilotConfig(BaseModel):
    capacity_reserve_percent: int = Field(ge=0, lt=100)
    slow_stock_days: int = Field(gt=0)
    demo_routing: DemoRoutingConfig


def load_config() -> PilotConfig:
    path = Path(os.environ.get("MESH_PILOT_CONFIG", ROOT / "config" / "pilot.json"))
    return PilotConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))
