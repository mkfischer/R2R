from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ResultsWrapper(BaseModel, Generic[T]):
    results: T


class PaginatedResultsWrapper(BaseModel, Generic[T]):
    results: T
    total_entries: int