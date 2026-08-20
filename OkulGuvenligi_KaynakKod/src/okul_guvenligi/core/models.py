from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class PlanOperation:
    """AI veya yerel planlayıcıdan gelen semantik, henüz uygulanmamış işlem."""

    kind: str
    module: str = ""
    target: str = ""
    label: str = ""
    data_type: str = ""
    placement: str = ""
    direction: str = ""
    value: Any = None
    reason: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PlanOperation":
        if not isinstance(raw, dict):
            raise ValueError("Plan işlemi nesne biçiminde olmalıdır.")
        return cls(
            kind=str(raw.get("kind", "")).strip(),
            module=str(raw.get("module", "") or "").strip(),
            target=str(raw.get("target", "") or "").strip(),
            label=str(raw.get("label", "") or "").strip(),
            data_type=str(raw.get("data_type", "") or "").strip(),
            placement=str(raw.get("placement", "") or "").strip(),
            direction=str(raw.get("direction", "") or "").strip(),
            value=raw.get("value"),
            reason=str(raw.get("reason", "") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChangePlan:
    """Kullanıcı talebinin açıklaması ve güvenli çekirdeğin inceleyeceği işlemler."""

    message: str
    operations: list[PlanOperation] = field(default_factory=list)
    source: str = "local"
    vision_used: bool = False
    assistant_text: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, source: str = "openai", vision_used: bool = False) -> "ChangePlan":
        if not isinstance(raw, dict):
            raise ValueError("Asistan planı nesne biçiminde olmalıdır.")
        operations_raw = raw.get("operations", [])
        if not isinstance(operations_raw, list):
            raise ValueError("Asistan işlemleri liste biçiminde olmalıdır.")
        return cls(
            message=str(raw.get("message", "") or "").strip(),
            operations=[PlanOperation.from_dict(item) for item in operations_raw],
            source=source,
            vision_used=vision_used,
            assistant_text=str(raw.get("assistant_text", "") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "operations": [item.to_dict() for item in self.operations],
            "source": self.source,
            "vision_used": self.vision_used,
            "assistant_text": self.assistant_text,
        }
