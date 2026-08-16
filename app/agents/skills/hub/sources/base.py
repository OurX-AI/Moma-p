from abc import ABC, abstractmethod
from ..models import SkillBundle, SkillMeta


class SkillSource(ABC):
    source_id: str

    @abstractmethod
    def search(self, query: str, *, limit: int = 50, offset: int = 0) -> list[SkillMeta]:
        raise NotImplementedError

    @abstractmethod
    def inspect(self, identifier_path: str) -> SkillMeta | None:
        raise NotImplementedError

    @abstractmethod
    def fetch_bundle(self, identifier_path: str) -> SkillBundle | None:
        raise NotImplementedError
