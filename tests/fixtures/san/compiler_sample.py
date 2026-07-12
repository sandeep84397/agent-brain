import json


class JsonEnvelope:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def render(self, payload: dict[str, object]) -> str:
        if not payload:
            raise ValueError("payload must not be empty")
        return f"{self.prefix}:{json.dumps(payload, sort_keys=True)}"
