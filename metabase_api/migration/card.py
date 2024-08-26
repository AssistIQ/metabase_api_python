from copy import deepcopy
from typing import Any
import logging

from metabase_api import Metabase_API
from metabase_api.utility.db.tables import TablesEquivalencies
from metabase_api.utility.options import Options
from metabase_api.utility.translation import Language, Translators

from dataclasses import dataclass

MIGRATED_CARDS: list[int] = list()


_logger = logging.getLogger(__name__)


@dataclass
class Card:
    """A Card! :-)"""

    card_json: dict

    @classmethod
    def from_id(cls, card_id: int, metabase_api: Metabase_API) -> "Card":
        card_json = metabase_api.get(f"/api/card/{card_id}")
        return Card(card_json=card_json)

    @property
    def card_id(self) -> int:
        return self.card_json["id"]

    def push(self, metabase_api: Metabase_API) -> bool:
        success = (
            metabase_api.put("/api/card/{}".format(self.card_id), json=self.card_json)
            == 200
        )
        if success:
            MIGRATED_CARDS.append(self.card_id)
        return success


@dataclass
class CardParameters:
    """Encapsulates logic for migration of a card."""

    lang: Language
    metabase_api: Metabase_API
    db_target: int
    transformations: dict
    table_equivalencies: TablesEquivalencies
    personalization_options: Options

    def migrate_card_by_id(
        self,
        card_id: int,
    ) -> bool:
        if card_id in MIGRATED_CARDS:
            _logger.debug(f"[already migrated card id '{card_id}']")
            return True
        _logger.info(f"Visiting card id '{card_id}'")
        card_json = Card.from_id(
            card_id=card_id, metabase_api=self.metabase_api
        ).card_json
        # update db and table id
        # db
        card_json["database_id"] = (
            self.db_target if card_json["database_id"] is not None else None
        )
        if "dataset_query" in card_json:
            card_json["dataset_query"]["database"] = self.db_target
            # change query
            if "query" in card_json["dataset_query"]:
                query_part = card_json["dataset_query"]["query"]
                # if "source-table" in query_part:
                self._update_query_part(
                    card_id=card_id,
                    query_part=query_part,
                    cards_src2dst=self.transformations["cards"],
                )
        # table
        table_id = card_json.get("table_id", None)
        if table_id is not None:
            try:
                card_json["table_id"] = self.table_equivalencies[table_id].unique_id
            except KeyError as ke:
                # mmmh... by any chance is this table_id already in target?
                # (in which case it would mean that it had already been replaced)
                if table_id not in self.table_equivalencies.dst_tables_ids:
                    msg = f"[re-writing references on card '{card_id}']"
                    msg += f"Table '{table_id}' is referenced at source, but no replacement is specified."
                    raise ValueError(msg) from ke
            # and now I have to replace the fields' references to this table
            # these next 2 lines were not used. Commenting them as I don't understand what's up. todo: delete?
            # src_table_fields = column_references["src"][table_id]
            # dst_table_fields = column_references["dst"][table_src2dst[table_id]]
        # change result metadata
        if ("result_metadata" not in card_json) or (
            card_json["result_metadata"] is None
        ):
            _logger.debug(f"[card: {card_id}] There is no 'result_metadata'")
        else:
            for md in card_json["result_metadata"]:
                if ("field_ref" in md) and (md["field_ref"][0] == "field"):
                    old_field_id = md["field_ref"][1]
                    if isinstance(old_field_id, int):
                        new_field_id = self.replace_column_id(column_id=old_field_id)
                        # new_field_id = self.table_equivalencies.column_equivalent_for(
                        #     column_id=old_field_id
                        # )
                        # awesomeness!
                        md["field_ref"][1] = new_field_id
                        md["id"] = new_field_id
                if "table_id" in md:
                    md["table_id"] = self.table_equivalencies[table_id].unique_id
        self.handle_card(card_json)
        # and go!
        success = (
            self.metabase_api.put("/api/card/{}".format(card_id), json=card_json) == 200
        )
        if success:
            MIGRATED_CARDS.append(card_id)
        return success

    def migrate_card(
        self,
        card_json: dict,
    ) -> bool:
        assert (
            card_json["model"] == "card"
        ), f"Trying to migrate a card that is NOT actually a card: it is a '{card_json['model']}'"
        # translate to specific language
        # card_json = _translate_card(card_json, lang=lang)
        card_id = card_json["id"]
        # even if I have the 'full' json here, I need to make sure I don't miss any details of this card
        # The way to do that is to get and fetch those details from the API. So:
        return self.migrate_card_by_id(card_id=card_id)

    def handle_card(self, card_json):
        """todo: document!"""
        self._translate_card(card_json)
        # card itself
        if "card" in card_json:
            card = card_json["card"]
            if "table_id" in card:
                if card["table_id"] is not None:
                    if card["table_id"] not in self.table_equivalencies.dst_tables_ids:
                        card["table_id"] = self.table_equivalencies[
                            card["table_id"]
                        ].unique_id
            if "database_id" in card:
                if card["database_id"] != self.db_target:
                    card["database_id"] = self.db_target
        # mappings to filters
        for mapping in card_json.get("parameter_mappings", []):
            if "card_id" in mapping:
                mapping["card_id"] = self.transformations["cards"][mapping["card_id"]]
            if "target" in mapping:
                t = mapping["target"]
                if (t[0] == "dimension") and (t[1][0] == "field"):
                    if isinstance(t[1][1], int):  # is this a column ID?
                        t[1][1] = self.replace_column_id(column_id=t[1][1])
                    else:  # no, no column ID - then maybe column NAME?
                        _r = self.personalization_options.fields_replacements.get(
                            t[1][1], None
                        )
                        if _r is not None:
                            t[1][1] = _r
        if "visualization_settings" in card_json:
            self._update_viz_settings(
                viz_settings=card_json["visualization_settings"],
            )

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

    def _handle_condition_filter(self, filter_parts: Any):
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

    def _update_query_part(
        self,
        card_id: int,
        query_part: dict,  # todo: be more specific!
        cards_src2dst: dict[int, int],  # transformations['cards']
    ) -> tuple[dict, list[int]]:  # todo: be more specific!
        """change query."""

        # table
        if "source-table" in query_part:
            src_table_in_query = query_part["source-table"]
            if isinstance(src_table_in_query, int):
                # if the source is an int => it MUST be the id of a table
                # (and so its correspondence must be found in the input)
                try:
                    query_part["source-table"] = self.table_equivalencies[
                        src_table_in_query
                    ].unique_id
                except KeyError as ke:
                    msg = f"[re-writing references on card '{card_id}']"
                    msg += f"Table '{src_table_in_query}' is referenced at source, but no replacement is specified."
                    raise ValueError(msg) from ke
                # # and now I have to replace the fields' references to this table
                # src_table_fields = column_references["src"][src_table_in_query]
                # dst_table_fields = column_references["dst"][
                #     table_src2dst[src_table_in_query]
                # ]
            elif str(src_table_in_query).startswith("card"):
                # it's reference a card. Which one?
                ref_card_id = int(src_table_in_query.split("__")[1])
                try:
                    # when we find such reference, this referenced card MUST be migrated before the referencees cards:
                    new_card_id = cards_src2dst[ref_card_id]
                except KeyError as ke:
                    raise KeyError(
                        f"Card {ref_card_id} is referenced in dashboard but we can't find the card itself."
                    ) from ke
                _logger.debug(f"=---- migrating referenced card '{new_card_id}'")
                self.migrate_card_by_id(
                    card_id=new_card_id,
                )
                query_part["source-table"] = f"card__{new_card_id}"
            else:
                raise ValueError(
                    f"I don't know what this reference is: {src_table_in_query}"
                )
        # query!
        if "source-query" in query_part:
            query_part["source-query"] = self._update_query_part(
                card_id=card_id,
                query_part=query_part["source-query"],
                cards_src2dst=cards_src2dst,
            )
        if "filter" in query_part:
            self._handle_condition_filter(filter_parts=query_part["filter"])
        if "aggregation" in query_part:
            for agg_details_as_list in query_part["aggregation"]:
                self._replace_field_info_refs(
                    field_info=agg_details_as_list,
                )
        if "expressions" in query_part:
            assert isinstance(query_part["expressions"], dict)
            for key, expr_as_list in query_part["expressions"].items():
                try:
                    self._replace_field_info_refs(expr_as_list)
                except Exception as e:
                    raise e
                # for field_info in expr_as_list:
                #     # todo: can't I just do self._replace_field_info_refs(expr_as_list) ??
                #     if isinstance(field_info, list):
                #         self._replace_field_info_refs(field_info)
        # breakout
        if "breakout" in query_part:
            for brk in query_part["breakout"]:
                if brk[0] == "field":
                    # reference to a table's column. Replace it.
                    old_field_id = brk[1]
                    if isinstance(old_field_id, int):
                        brk[1] = self.replace_column_id(column_id=old_field_id)
        if "order-by" in query_part:
            for ob in query_part["order-by"]:
                # reference to a table's column. Replace it.
                desc = ob[1][0]
                old_field_id = ob[1][1]
                if isinstance(old_field_id, int) and (desc != "aggregation"):
                    ob[1][1] = self.replace_column_id(column_id=old_field_id)

        return query_part  # todo: get rid of this return, as I think no-one uses it!

    def _replace_field_info_refs(
        self,
        field_info: list,
    ) -> list:
        if field_info[0] == "field":
            # reference to a table's column. Replace it.
            old_field_id = field_info[1]
            if isinstance(old_field_id, int):
                field_info[1] = self.replace_column_id(column_id=old_field_id)
            else:
                # here: is old_field_id actually the NAME of a field we are replacing
                # (through perso_options)?
                # if so: replace
                # otherwise: leave it alone.
                _r = self.personalization_options.fields_replacements.get(
                    old_field_id, None
                )
                if _r is not None:
                    field_info[1] = _r
                else:
                    _logger.warning(
                        f"All good here????? I don't have to replace '{old_field_id}'...?"
                    )
        else:
            for idx, item in enumerate(field_info):
                if isinstance(item, list):
                    field_info[idx] = self._replace_field_info_refs(
                        item,
                    )
        return field_info

    def _update_viz_settings(
        self,
        viz_settings: dict,
    ):
        for k, v in viz_settings.items():
            if (
                (k == "text")
                or (k == "graph.x_axis.title_text")
                or (k == "graph.y_axis.title_text")
            ):
                viz_settings[k] = Translators[self.lang].translate(viz_settings[k])
            elif k == "series_settings":
                ser_sets = viz_settings["series_settings"]
                for _, d in ser_sets.items():
                    for _k, _v in d.items():
                        if _k == "title":
                            d[_k] = Translators[self.lang].translate(_v)
            elif k == "table.columns":
                for table_column in viz_settings["table.columns"]:
                    self._update_table_cols_info(table_column)
            elif k == "column_settings":
                # first, let's change keys (if needed)
                for _k in deepcopy(viz_settings["column_settings"]).keys():
                    # continue
                    l = eval(_k.replace("null", "None"))
                    if l[0] == "ref":
                        field_info = l[1]
                        if field_info[0] == "field":
                            field_info[1] = self.replace_column_id(field_info[1])
                            new_k = str(l).replace("None", "null").replace("'", '"')
                            viz_settings["column_settings"][new_k] = viz_settings[
                                "column_settings"
                            ].pop(_k)
                for _, d in viz_settings["column_settings"].items():
                    if "click_behavior" in d:
                        self._handle_click_behavior(d["click_behavior"])
            elif k == "click_behavior":
                self._handle_click_behavior(viz_settings["click_behavior"])
            elif k == "pivot_table.column_split":
                _logger.debug(
                    f"WARNING == LUIS, DO SOMETHING!!! '{k}' -- THIS IS WEIRD!??"
                )
                # for _, a_list in v.items():
                #     self._replace_field_info_refs(a_list)
            elif k == "pie.dimension":
                _logger.debug(
                    f"WARNING == LUIS, DO SOMETHING!!! '{k}' (value is '{v}')??"
                )
            elif k == "graph.dimensions":
                _logger.debug(
                    f"CHECK IF EVERYTHING IS OK!!!!! for '{k}' (value is '{v}')??"
                )
                _l = []
                for _v in viz_settings[k]:
                    # do I have to replace it?
                    _r = self.personalization_options.fields_replacements.get(_v, None)
                    _l.append(_r if _r is not None else _v)
                viz_settings[k] = _l
            elif k == "table.pivot_column":
                _logger.debug(
                    f"WARNING == LUIS, DO SOMETHING!!! '{k}' (value is '{v}')??"
                )
            else:
                _logger.debug(
                    f"WARNING == CHECK: do you have to handle '{k}' (value is '{v}')??"
                )

    def _handle_click_behavior(
        self,
        click_behavior: dict,
    ):
        if "targetId" in click_behavior:
            try:
                old_targetid = click_behavior["targetId"]
            except KeyError as ke:
                raise ValueError(f"no target id") from ke
            try:
                new_targetid = self.transformations["cards"][old_targetid]
            except KeyError:
                msg = f"Target '{old_targetid}' is referenced at source, but no replacement is specified."
                _logger.error(msg)
                raise RuntimeError(msg)
            click_behavior["targetId"] = new_targetid
        if "parameterMapping" in click_behavior:
            for mapping_name, mapping in deepcopy(
                click_behavior["parameterMapping"]
            ).items():
                # I can see fields in 'target'. # todo: are there some in 'source' too...?
                if "target" in mapping:
                    map_target = mapping["target"]
                    if map_target["type"] == "dimension":
                        map_target_dim = map_target["dimension"]
                        field_info = map_target_dim[1]
                        if field_info[0] == "field":
                            if isinstance(field_info[1], int):
                                field_info[1] = self.replace_column_id(
                                    column_id=field_info[1]
                                )
                                # field_info[
                                #     1
                                # ] = self.table_equivalencies.column_equivalent_for(
                                #     column_id=field_info[1]
                                # )
                            map_target["id"] = str(map_target["dimension"])
                            old_id = mapping["id"]
                            mapping["id"] = map_target["id"]
                            click_behavior["parameterMapping"].pop(old_id)
                            click_behavior["parameterMapping"][mapping["id"]] = mapping

    def _translate_card(self, card_json: dict) -> dict:
        """
        Translates a card, on its totality.
        Args:
            card_json:

        Returns:

        """
        for k, v in card_json.items():
            if (k == "description") or (k == "name"):
                if v is not None:
                    card_json[k] = Translators[self.lang].translate(v)
            elif k == "visualization_settings":
                viz_set = v
                for k, v in viz_set.items():
                    if k.endswith("title_text"):
                        viz_set[k] = Translators[self.lang].translate(v)
                    elif "text" in k:
                        viz_set[k] = Translators[self.lang].translate(v)
                    elif (k == "graph.metrics") or (k == "pie.metric"):
                        _logger.debug(
                            f"[visualization_settings] anything I have to do for '{k}'...? (value is '{v}')"
                        )
                    elif k == "column_settings":
                        cols_set = viz_set["column_settings"]
                        for _, d in cols_set.items():
                            for k, v in d.items():
                                if k == "column_title":
                                    d[k] = Translators[self.lang].translate(v)
                                else:
                                    _logger.debug(
                                        f"[visualization_settings][column_settings] '{k}': I guess I do nothing? (value is '{v}')"
                                    )
                    elif k == "series_settings":
                        series_set = viz_set["series_settings"]
                        for _, d in series_set.items():
                            for k, v in d.items():
                                if k == "title":
                                    d[k] = Translators[self.lang].translate(v)
                                else:
                                    _logger.debug(
                                        f"[visualization_settings][series_settings] '{k}': I guess I do nothing? (value is '{v}')"
                                    )
                    elif k.endswith("column") or k.endswith("columns"):
                        _logger.debug(
                            f"[visualization_settings]['{k}']: accesses column names so I don't think we need to change anything (value is '{v}')"
                        )
                    else:
                        _logger.debug(
                            f"[visualization_settings]['{k}'] is it possible there is nothing to do? (value is '{v}')"
                        )
            # elif k == 'parameter_mappings':
            #     print("lala")
            # elif k == 'parameters':
            #     print("lala")
            # else:
            #     print(f"[{k}] Anything else to translate?")
        return card_json

    def _update_table_cols_info(
        self,
        table_column: dict,
    ) -> dict:
        for key, value in table_column.items():
            if key == "fieldRef":
                field_ref = value  # table_column["fieldRef"]
                if field_ref[0] == "field":
                    old_field_id = field_ref[1]
                    if isinstance(old_field_id, int):
                        field_ref[1] = self.replace_column_id(column_id=old_field_id)
            elif key == "key":
                l = eval(value.replace("null", "None"))
                if l[0] == "ref":
                    field_info = l[1]
                    if field_info[0] == "field":
                        field_info[1] = self.replace_column_id(column_id=field_info[1])
                        table_column[key] = (
                            str(l)
                            .replace("None", "null")
                            .replace("'", '"')
                            .replace(" ", "")
                        )
            elif key == "name":
                _logger.debug(
                    f"WARNING [replacement] Do I have to do something with 'name'??????? (currently = '{value}')"
                )
            else:
                _logger.debug(
                    f"WARNING [replacement] (I think not, but...) Do I have to do something with '{key}'? (currently = '{value}')"
                )
        return table_column