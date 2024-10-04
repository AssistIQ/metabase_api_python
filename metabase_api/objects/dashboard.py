import logging
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, Optional, Any

from metabase_api.metabase_api import Metabase_API
from metabase_api.objects.card import Card
from metabase_api.objects.defs import (
    CollectionObject,
    ReturnValue,
    TraverseStack,
    TraverseStackElement,
    CardParameters,
    clean_labels,
)

_logger = logging.getLogger(__name__)


@dataclass(init=False)
class Dashboard(CollectionObject):

    as_json: dict[Any, Any]

    def __init__(self, as_json: dict[Any, Any]):
        super().__init__(as_json=as_json)

    @property
    def dashboard_id(self) -> int:
        return int(self.as_json["id"])

    def traverse(
        self,
        f: Callable[[dict[Any, Any], TraverseStack], ReturnValue],
        call_stack: Optional[TraverseStack] = None,
    ) -> ReturnValue:

        r = ReturnValue(None)  # basically: init with 'nothing' inside.
        if call_stack is None:
            call_stack = TraverseStack()
        with call_stack.add(TraverseStackElement.DASHBOARD):
            for card_json in self.as_json["dashcards"]:
                r = r.union(Card(card_json).traverse(f, call_stack))
            r = r.union(f(self.as_json, call_stack))
            for k, v in self.as_json.items():
                if k == "tabs":  # tabs in dashboard
                    with call_stack.add(TraverseStackElement.TABS):
                        r = r.union(f(v, call_stack))
                if k == "parameters":
                    parameters = v
                    for params_dict in parameters:
                        with call_stack.add(TraverseStackElement.PARAMETER):
                            r = r.union(f(params_dict, call_stack))
                elif k == "param_values":
                    param_values = v
                    if param_values is not None:
                        with call_stack.add(TraverseStackElement.PARAM_VALUES):
                            r = r.union(f(param_values, call_stack))
                elif k == "param_fields":
                    old_param_fields = deepcopy(v)
                    if old_param_fields is not None:
                        with call_stack.add(TraverseStackElement.PARAM_FIELDS):
                            r = r.union(f(old_param_fields, call_stack))
        return r

    def migrate(self, params: CardParameters, push: bool) -> bool:
        from metabase_api.migration.defs import migration_function

        self.traverse(
            f=lambda a_json, a_stack: migration_function(
                caller_json=a_json, params=params, call_stack=a_stack
            ),
        )
        # todo: think about this.
        # # and also I change the formatting of the numbers
        # self.traverse(
        #     f=lambda a_json, a_stack: number_formatter(
        #         caller_json=a_json,
        #         number_format=params.personalization_options.number_format,
        #         call_stack=a_stack,
        #     ),
        # )
        return self.push(metabase_api=params.metabase_api) if push else True

    @property
    def labels(self) -> set[str]:
        if len(self._labels) == 0:
            dash = self.as_json
            for card_json in self.as_json.get("dashcards", list()):
                self._labels = self._labels.union(Card(card_json).labels)
            for k, v in dash.items():
                if k == "description":
                    if dash["description"] is not None:
                        self._labels.add(dash["description"])
                elif k == "tabs":
                    # tabs in dashboard
                    tabs = v
                    for a_tab in tabs:
                        # let's translate the name
                        self._labels.add(a_tab["name"])
                elif k == "parameters":
                    parameters = v
                    for params_dict in parameters:
                        # let's translate the name
                        self._labels.add(params_dict["name"])
                # elif k == "name":
                #     # change name, tag it, and go!
                #     dash["name"] = (
                #         new_dashboard_name
                #         if new_dashboard_name is not None
                #         else dash["name"]
                #     )
                elif k == "description":
                    if (dash["description"] is not None) and (
                        dash["description"] != ""
                    ):
                        self._labels.add(dash["description"])
        return clean_labels(self._labels)

    def push(self, metabase_api: Metabase_API) -> bool:
        _logger.info(f"Using API to update dashboard '{self.dashboard_id}'...")
        r = metabase_api.put(f"/api/dashboard/{self.dashboard_id}", json=self.as_json)
        # sanity check
        assert r == 200, f"Problems updating dashboard '{self.dashboard_id}'; code {r}"
        return bool(r == 200)