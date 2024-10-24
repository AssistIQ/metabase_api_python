import abc
import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, Any, Optional

from metabase_api.metabase_api import Metabase_API
from metabase_api.utility.db.tables import TablesEquivalencies
from metabase_api.utility.options import Options

_logger = logging.getLogger(__name__)


def clean_labels(labels: set[str]) -> set[str]:
    """Does a minimal clean-up of labels."""
    return {l.strip() for l in labels if len(l.strip()) > 0}


class TraverseStackElement(Enum):
    """All the elements I can visit when I am traversing an object."""

    CARD = auto()
    CLICK_BEHAVIOR = auto()
    COLLECTION = auto()
    COLUMN_SETTINGS = auto()
    DASHBOARD = auto()
    GRAPH_DIMENSIONS = auto()
    PARAMETER_MAPPING = auto()
    PARAMETER = auto()
    PARAM_FIELDS = auto()
    PARAM_VALUES = auto()
    PULSE = auto()
    QUERY_PART = auto()
    SERIES_SETTINGS = auto()
    TABLE_COLUMN = auto()
    TABLE_COLUMNS = auto()
    TABS = auto()
    VISUALIZATION_SETTINGS = auto()

    def __str__(self) -> str:
        return self.name + (f" (title: {self.title})" if self.has_title() else "")

    def __new__(cls, *args, **kwds):  # type:ignore
        value = len(cls.__members__) + 1
        obj = object.__new__(cls)
        obj._value_ = value
        return obj

    def __init__(self, value: int) -> None:
        self._title: str = ""

    def has_title(self) -> bool:
        return self.title != ""

    @property
    def title(self) -> str:
        return self._title

    @title.setter
    def title(self, value: str) -> None:
        self._title = value

    def set_title(self, value: str) -> "TraverseStackElement":
        self.title = value
        return self


class TraverseStack:
    """A stack containing the elements we visit."""

    def __init__(self, l: Optional[list[TraverseStackElement]] = None) -> None:
        # super().__init__()
        self._as_list: list[TraverseStackElement] = l if l else []

    @property
    def top(self) -> TraverseStackElement:
        return self._as_list[-1]

    @property
    def bottom(self) -> "TraverseStack":
        """Take off the top and return the rest."""
        return TraverseStack(l=self._as_list[:-1])

    def __len__(self) -> int:
        return len(self._as_list)

    def add(self, elt: TraverseStackElement) -> "TraverseStack":
        self._as_list.append(elt)
        return self

    @property
    def empty(self) -> bool:
        return len(self) == 0

    def __enter__(self):  # type:ignore
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):  # type:ignore
        _ = self._as_list.pop()

    def __str__(self) -> str:
        return (
            "--[bottom]--" + " | ".join([str(v) for v in self._as_list]) + "--[top]--"
        )


class ReturnValue:
    """todo: document."""

    def __init__(self, v: Any):
        self.v = v

    @classmethod
    def empty(cls) -> "ReturnValue":
        return ReturnValue(None)

    def union(self, v: Any) -> "ReturnValue":
        if isinstance(v, ReturnValue):
            v = v.v
        # union-ing with None always works:
        if self.v is None:
            return ReturnValue(v)
        if v is None:
            return ReturnValue(self.v)
        # two values need to be the same type
        if not isinstance(v, type(self.v)):
            raise TypeError("Impossible to union")
        if isinstance(self.v, set):
            return ReturnValue(set(self.v).union(set(v)))
        if isinstance(self.v, list):
            return ReturnValue(self.v + v)
        raise NotImplementedError("not sure what happened here")


@dataclass
class MigrationParameters:
    """Encapsulates logic for migration of a (metabase) object."""

    metabase_api: Metabase_API
    db_target: int
    transformations: dict  # type:ignore
    table_equivalencies: TablesEquivalencies
    personalization_options: Options

    def replace_column_id(self, column_id: int) -> int:
        """todo: doc."""
        (
            new_field_id,
            t_src,
            t_target,
        ) = self.table_equivalencies.column_equivalent_and_details_for(
            column_id=column_id
        )
        # this column I just updated - does it appear among the ones to be replaced?
        c_to_id = self.personalization_options.replacement_column_id_for(
            column_id=new_field_id, t=t_target
        )
        if c_to_id is not None:  # None == 'no replacement specified'
            return c_to_id
        else:
            return new_field_id

    def _handle_condition_filter(self, filter_parts: Any) -> None:
        # todo: do I need to return anything....?
        def _is_cmp_op(op: str) -> bool:
            # cmp operator (like '>', '=', ...)
            return (op == ">") or (op == "=") or (op == "<=>")

        def _is_logical_op(op: str) -> bool:
            # logical operator
            return (op == "or") or (op == "and")

        if isinstance(filter_parts, list):
            op = filter_parts[0].strip()
            if op == "field":
                # reference to a table's column. Replace it.
                field_info = filter_parts
                old_field_id = field_info[1]
                if isinstance(old_field_id, int):
                    field_info[1] = self.replace_column_id(old_field_id)
            elif _is_cmp_op(op) or _is_logical_op(op) or (op.strip() == "starts-with"):
                self._handle_condition_filter(filter_parts=filter_parts[1])
                self._handle_condition_filter(filter_parts=filter_parts[2])
            else:
                raise RuntimeError(f"Luis, this should be a constant: '{op}'... is it?")

    def _replace_field_info_refs(self, field_info: list[Any]) -> list[Any]:
        if field_info[0] == "field":
            # reference to a table's column. Replace it.
            old_field_id = field_info[1]
            if isinstance(old_field_id, int):
                try:
                    field_info[1] = self.replace_column_id(column_id=old_field_id)
                except ValueError as ve:
                    # it couldn't replace it - if it happens that we're _already_ migrated, all good
                    # So only fail if there looks like a problem
                    if (
                        self.table_equivalencies.target_table_for_column(
                            column_id=old_field_id
                        )
                        is None
                    ):
                        raise ve
            else:
                # do we actually have the NAME of a field we are replacing?
                # if so: replace
                _r = self.personalization_options.fields_replacements.get(
                    old_field_id, None
                )
                if _r is not None:
                    field_info[1] = _r
        else:
            for idx, item in enumerate(field_info):
                if isinstance(item, list):
                    field_info[idx] = self._replace_field_info_refs(
                        item,
                    )
        return field_info


class CollectionObject(abc.ABC):
    """Any object that can appear on a collection."""

    def __init__(self, as_json: dict[Any, Any]):
        self.as_json = as_json
        self._labels: set[str] = set()

    @property
    def object_id(self) -> int:
        return int(self.as_json["id"])

    @property
    def labels(self) -> set[str]:
        from metabase_api.objects.visitors.defs import label_fetcher

        if len(self._labels) == 0:
            self._labels = clean_labels(set(self.traverse(f=label_fetcher).v))
        return self._labels

    @abc.abstractmethod
    def traverse(
        self,
        f: Callable[[dict[Any, Any], TraverseStack], ReturnValue],
        call_stack: Optional[TraverseStack] = None,
    ) -> ReturnValue:
        """
        Traverses the object, applying a function to the visited place.
        Args:
            f: function to apply
            call_stack: stack to know where these calls originated from.

        Returns:
            The same value as 'f'
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def push(self, metabase_api: Metabase_API) -> bool:
        pass

    def migrate(self, params: MigrationParameters, push: bool) -> bool:
        """Migrates the object, based on a set of parameters. Pushes if flag is True."""
        from metabase_api.migration.defs import migration_function
        from metabase_api.objects.visitors.defs import number_formatter

        self.traverse(
            f=lambda a_json, a_stack: migration_function(
                caller_json=a_json, params=params, call_stack=a_stack
            ),
        )
        # and also I change the formatting of the numbers
        self.traverse(
            f=lambda a_json, a_stack: number_formatter(
                caller_json=a_json,
                number_format=params.personalization_options.number_format,
                call_stack=a_stack,
            ),
        )
        return self.push(metabase_api=params.metabase_api) if push else True

    def translate(self, translation_dict: dict[str, str]) -> None:
        """Changes labels in the object - aka, 'translates' it."""
        from metabase_api.objects.visitors.defs import label_replacer

        self.traverse(
            f=lambda a_json, a_stack: label_replacer(
                caller_json=a_json, call_stack=a_stack, labels_repl=translation_dict
            ),
        )
