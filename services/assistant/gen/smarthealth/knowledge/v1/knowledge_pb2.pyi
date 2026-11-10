from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SearchRequest(_message.Message):
    __slots__ = ("query", "limit")
    QUERY_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    query: str
    limit: int
    def __init__(self, query: _Optional[str] = ..., limit: _Optional[int] = ...) -> None: ...

class Hit(_message.Message):
    __slots__ = ("source_id", "kind", "title", "snippet", "score")
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    SNIPPET_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    source_id: str
    kind: str
    title: str
    snippet: str
    score: float
    def __init__(self, source_id: _Optional[str] = ..., kind: _Optional[str] = ..., title: _Optional[str] = ..., snippet: _Optional[str] = ..., score: _Optional[float] = ...) -> None: ...

class SearchReply(_message.Message):
    __slots__ = ("hits",)
    HITS_FIELD_NUMBER: _ClassVar[int]
    hits: _containers.RepeatedCompositeFieldContainer[Hit]
    def __init__(self, hits: _Optional[_Iterable[_Union[Hit, _Mapping]]] = ...) -> None: ...
