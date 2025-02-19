from types import ModuleType
from typing import List, Optional, Type, TypedDict, Union

from dlt.common.typing import StrAny
from dlt.common.normalizers.naming import NamingConvention

TNamingConventionReferenceArg = Union[str, Type[NamingConvention], ModuleType]


class TJSONNormalizer(TypedDict, total=False):
    module: str
    config: Optional[StrAny]  # config is a free form and is validated by `module`


class TNormalizersConfig(TypedDict, total=False):
    names: str
    allow_identifier_change_on_table_with_data: Optional[bool]
    detections: Optional[List[str]]
    json: TJSONNormalizer
