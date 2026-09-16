from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ToolDesc:
	name: str
	description: str
	input_schema: dict[str, Any]
	handler: Callable[..., str]

	def to_wire(self) -> dict[str, Any]:
		return {
			"name": self.name,
			"description": self.description,
			"input_schema": self.input_schema,
		}
