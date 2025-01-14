#!/usr/bin/env python3

#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import copy
import functools
import heapq
import json
import pickle
import numpy as np   # type: ignore[import]
import pandas as pd  # type: ignore[import]
from typing import Any, Dict, List, Optional, Tuple, Union

from pyspark.sql import DataFrame, SparkSession, functions  # type: ignore[import]
from pyspark.sql.functions import col, expr  # type: ignore[import]
from pyspark.sql.types import ByteType, IntegerType, LongType, ShortType, \
    StringType, StructField, StructType  # type: ignore[import]

from repair.costs import UpdateCostFunction
from repair.errors import ConstraintErrorDetector, ErrorDetector, ErrorModel, RegExErrorDetector
from repair.train import build_model, compute_class_nrow_stdv, train_option_keys, rebalance_training_data
from repair.utils import argtype_check, elapsed_time, get_option_value, get_random_string, \
    setup_logger, spark_job_group, to_list_str


_logger = setup_logger()


class PoorModel():
    """Model to return the same value regardless of an input value.

    .. versionchanged:: 0.1.0
    """

    def __init__(self, v: Any) -> None:
        self.v = v

    @property
    def classes_(self) -> Any:
        return np.array([self.v])

    def predict(self, X: pd.DataFrame) -> Any:
        return [self.v] * len(X)

    def predict_proba(self, X: pd.DataFrame) -> Any:
        return [np.array([1.0])] * len(X)


class FunctionalDepModel():
    """
    Model class to mimic the scikit-learn APIs to predict values
    based on the rules of functional dependencies.

    .. versionchanged:: 0.1.0
    """

    def __init__(self, x: str, fd_map: Dict[str, str]) -> None:
        self.fd_map = fd_map
        self.classes = list(set(fd_map.values()))
        self.x = x

        # Creates a var to map keys into their indexes on `fd_map.keys()`
        self.fd_keypos_map = {}
        for index, c in enumerate(self.classes):
            self.fd_keypos_map[c] = index

    @property
    def classes_(self) -> Any:
        return np.array(self.classes)

    def predict(self, X: pd.DataFrame) -> Any:
        return list(map(lambda x: self.fd_map[x] if x in self.fd_map else None, X[self.x]))

    def predict_proba(self, X: pd.DataFrame) -> Any:
        pmf = []
        for x in X[self.x]:
            if x in self.fd_map.keys():
                probs = np.zeros(len(self.classes))
                probs[self.fd_keypos_map[self.fd_map[x]]] = 1.0
                pmf.append(probs)
            else:
                _logger.warning(f'Unknown "{self.x}" domain value found: {x}')
                pmf.append(None)  # type: ignore

        return pmf


class RepairModel():
    """
    Interface to detect error cells in given input data and build a statistical
    model to repair them.

    .. versionchanged:: 0.1.0
    """

    # List of internal configurations
    from collections import namedtuple
    _option = namedtuple('_option', 'key default_value type_class validator err_msg')

    _opt_max_training_row_num = \
        _option('model.max_training_row_num', 10000, int,
                lambda v: v >= 10, '`{}` should be greater than and equal to 10')
    _opt_max_training_column_num = \
        _option('model.max_training_column_num', 65536, int,
                lambda v: v >= 2, '`{}` should be greater than 1')
    _opt_small_domain_threshold = \
        _option('model.small_domain_threshold', 12, int,
                lambda v: v >= 3, '`{}` should be greater than 2')
    _opt_repair_by_regex_disabled = \
        _option('model.rule.repair_by_regex.disabled', True, bool,
                None, None)
    _opt_repair_by_nearest_values_disabled = \
        _option('model.rule.repair_by_nearest_values.disabled', True, bool,
                None, None)
    _opt_merge_threshold = \
        _option('model.rule.merge_threshold', 2.0, float,
                None, None)
    _opt_repair_by_functional_deps_disabled = \
        _option('model.rule.repair_by_functional_deps.disabled', False, bool,
                None, None)
    _opt_max_domain_size = \
        _option('model.rule.max_domain_size', 1000, int,
                lambda v: v > 10, '`{}` should be greater than 10')
    _opt_cost_weight = \
        _option('repair.pmf.cost_weight', 0.1, float,
                lambda v: v > 0.0, '`{}` should be positive')
    _opt_prob_threshold = \
        _option('repair.pmf.prob_threshold', 0.0, float,
                None, None)
    _opt_prob_top_k = \
        _option('repair.pmf.prob_top_k', 32, int,
                lambda v: v >= 3, '`{}` should be greater than 2')

    option_keys = set([
        _opt_max_training_row_num.key,
        _opt_max_training_column_num.key,
        _opt_small_domain_threshold.key,
        _opt_repair_by_regex_disabled.key,
        _opt_repair_by_nearest_values_disabled.key,
        _opt_merge_threshold.key,
        _opt_repair_by_functional_deps_disabled.key,
        _opt_max_domain_size.key,
        _opt_cost_weight.key,
        _opt_prob_threshold.key,
        _opt_prob_top_k.key,
        *ErrorModel.option_keys,
        *train_option_keys])

    def __init__(self) -> None:
        super().__init__()

        # Basic parameters
        self.db_name: str = ""
        self.input: Optional[Union[str, DataFrame]] = None
        self.row_id: Optional[str] = None
        self.targets: List[str] = []

        # Parameters for error detection
        self.error_cells: Optional[Union[str, DataFrame]] = None
        self.error_detectors: List[ErrorDetector] = []
        self.discrete_thres: int = 80

        # Parameters for repair model training
        self.parallel_stat_training_enabled: bool = False
        self.training_data_rebalancing_enabled: bool = False
        self.repair_by_rules: bool = False

        # Parameters for repairing
        self.repair_delta: Optional[int] = None
        self.repair_validation_enabled: bool = False

        # Defines a class to compute cost of updates.
        #
        # TODO: Needs a sophisticated way to compute update costs from a current value to a repair candidate.
        # For example, the HoloDetect paper [1] proposes a noisy channel model for the data augmentation
        # methodology of training data. This model consists of transformation rule and and data augmentation
        # policies (i.e., distribution over those data transformation).
        # This model might be able to represent this cost. For more details, see the section 5,
        # 'DATA AUGMENTATION LEARNING', in the paper.
        self.cf: Optional[UpdateCostFunction] = None

        # Options for internal behaviours
        self.opts: Dict[str, str] = {}

        # Temporary views to keep intermediate results; these views are automatically
        # created when repairing data, and then dropped finally.
        self._intermediate_views_on_runtime: List[str] = []

        # JVM interfaces for Data Repair/Graph APIs
        self._spark = SparkSession.builder.getOrCreate()
        self._jvm = self._spark.sparkContext._active_spark_context._jvm  # type: ignore
        self._repair_api = self._jvm.RepairApi

    @argtype_check  # type: ignore
    def setDbName(self, db_name: str) -> "RepairModel":
        """Specifies the database name for an input table.

        .. versionchanged:: 0.1.0

        Parameters
        ----------
        db_name : str
            database name (default: '').
        """
        if type(self.input) is DataFrame:
            raise ValueError("Can not specify a database name when input is `DataFrame`")

        self.db_name = db_name
        return self

    @argtype_check  # type: ignore
    def setTableName(self, table_name: str) -> "RepairModel":
        """Specifies the table or view name to repair data.

        .. versionchanged:: 0.1.0

        Parameters
        ----------
        table_name : str
            table or view name.
        """
        if not table_name:
            raise ValueError("`table_name` should have at least character")

        self.input = table_name
        return self

    @argtype_check  # type: ignore
    def setInput(self, input: Union[str, DataFrame]) -> "RepairModel":
        """Specifies the table/view name or :class:`DataFrame` to repair data.

        .. versionchanged:: 0.1.0

        Parameters
        ----------
        input: str, :class:`DataFrame`
            table/view name or :class:`DataFrame`.
        """
        if type(input) is str:
            self.setTableName(input)
        else:  # DataFrame
            self.db_name = ""
            self.input = input

        return self

    @argtype_check  # type: ignore
    def setRowId(self, row_id: str) -> "RepairModel":
        """Specifies the table name or :class:`DataFrame` to repair data.

        .. versionchanged:: 0.1.0

        Parameters
        ----------
        input: str
            the column where all values are different.
        """
        if not row_id:
            raise ValueError("`row_id` should have at least character")

        self.row_id = row_id
        return self

    @argtype_check  # type: ignore
    def setTargets(self, attrs: List[str]) -> "RepairModel":
        """Specifies target attributes to repair.

        .. versionchanged:: 0.1.0

        Parameters
        ----------
        attrs: list
            list of target attributes.
        """
        if len(attrs) == 0:
            raise ValueError("`attrs` should have at least one attribute")

        self.targets = attrs
        return self

    @argtype_check  # type: ignore
    def setErrorCells(self, error_cells: Union[str, DataFrame]) -> "RepairModel":
        """Specifies the table/view name or :class:`DataFrame` defining where error cells are.

        .. versionchanged:: 0.1.0

        Parameters
        ----------
        input: str, :class:`DataFrame`
            table/view name or :class:`DataFrame`.

        Examples
        --------
        >>> spark.table("error_cells").show()
        +---+---------+
        |tid|attribute|
        +---+---------+
        |  3|      Sex|
        | 12|      Age|
        | 16|   Income|
        +---+---------+

        >>> df = delphi.repair.setInput("adult").setRowId("tid")
        ...     .setErrorCells("error_cells").run()
        >>> df.show()
        +---+---------+-------------+-----------+
        |tid|attribute|current_value|   repaired|
        +---+---------+-------------+-----------+
        |  3|      Sex|         null|     Female|
        | 12|      Age|         null|      18-21|
        | 16|   Income|         null|MoreThan50K|
        +---+---------+-------------+-----------+
        """
        if type(error_cells) is str and not error_cells:
            raise ValueError("`error_cells` should have at least character")

        if self.row_id is None:
            raise ValueError("`setRowId` should be called before specifying error cells")

        df = error_cells if type(error_cells) is DataFrame else self._spark.table(str(error_cells))
        if not all(c in df.columns for c in [self._row_id, "attribute"]):  # type: ignore
            raise ValueError(f"Error cells should have `{self.row_id}` and `attribute` in columns")

        self.error_cells = error_cells
        return self

    @argtype_check  # type: ignore
    def setErrorDetectors(self, detectors: List[ErrorDetector]) -> "RepairModel":
        """
        Specifies the list of :class:`ErrorDetector` derived classes to implement
        a logic to detect error cells.

        .. versionchanged:: 0.1.0

        Parameters
        ----------
        detectors: list of :class:`ErrorDetector` derived classes
            specifies how to detect error cells. Available classes are as follows:

            * :class:`NullErrorDetector`: detects NULL cells for error cells.
            * :class:`DomainValues`: detects error cells based on specified domain values.
            * :class:`RegExErrorDetector`: detects error cells based on a regular expresson.
            * :class:`OutlierErrorDetector`: detects error cells based on the Gaussian distribution.
            * :class:`ConstraintErrorDetector`: detects error cells based on integrity rules
              defined by denial constraints.
        """
        self.error_detectors = detectors
        return self

    @argtype_check  # type: ignore
    def setDiscreteThreshold(self, thres: int) -> "RepairModel":
        """Specifies max domain size of discrete values.

        .. versionchanged:: 0.1.0

        Parameters
        ----------
        thres: int
            max domain size of discrete values. The values must be bigger than 1 and
            the default value is 80.
        """
        if int(thres) < 2:
            raise ValueError(f"`thres` should be bigger than 1, got {thres}")

        self.discrete_thres = thres
        return self

    @argtype_check  # type: ignore
    def setParallelStatTrainingEnabled(self, enabled: bool) -> "RepairModel":
        """Specifies whether to enable parallel training for stats repair models.

        .. versionchanged:: 0.1.0

        Parameters
        ----------
        enabled: bool
            If set to ``True``, runs multiples tasks to build stat repair models (default: ``False``).
        """
        self.parallel_stat_training_enabled = enabled
        return self

    @argtype_check  # type: ignore
    def setTrainingDataRebalancingEnabled(self, enabled: bool) -> "RepairModel":
        """Specifies whether to enable class rebalancing in training data.

        .. versionchanged:: 0.1.0

        Parameters
        ----------
        enabled: bool
            If set to ``True``, rebalance class labels in training data (default: ``False``).
        """
        self.training_data_rebalancing_enabled = enabled
        return self

    @argtype_check  # type: ignore
    def setRepairByRules(self, enabled: bool) -> "RepairModel":
        """Specifies whether to enable rule-based repair techniques, e.g., using functional
           dependencies and merging nearest values.

        .. versionchanged:: 0.1.0

        Parameters
        ----------
        enabled: bool
            If set to ``True``, uses rule-based ways to repair data if possible (default: ``False``).
        """
        self.repair_by_rules = enabled
        return self

    @argtype_check  # type: ignore
    def setRepairDelta(self, delta: int) -> "RepairModel":
        """Specifies the max number of applied repairs.

        .. versionchanged:: 0.1.0

        Parameters
        ----------
        delta: int
            delta value (default: None). The value must be positive.
        """
        if delta <= 0:
            raise ValueError(f"Repair delta should be positive, got {delta}")

        self.repair_delta = int(delta)
        return self

    @argtype_check  # type: ignore
    def setUpdateCostFunction(self, cf: UpdateCostFunction) -> "RepairModel":
        """
        Specifies the :class:`UpdateCostFunction` derived class to implement
        a logic to compute update costs for repairs.

        .. versionchanged:: 0.1.0

        Parameters
        ----------
        cf: derived class of :class:`UpdateCostFunction`.
        """
        self.cf = cf
        return self

    @argtype_check  # type: ignore
    def option(self, key: str, value: str) -> "RepairModel":
        """Sets an input option for internal functionalities (e.g., model learning).

        .. versionchanged:: 0.1.0
        """
        if key not in self.option_keys:
            raise ValueError(f'Non-existent key specified: key={key}')

        self.opts[key] = value
        return self

    def _get_option_value(self, *args) -> Any:  # type: ignore
        return get_option_value(self.opts, *args)

    @property
    def _row_id(self) -> str:
        return str(self.row_id)

    @property
    def _input_table(self) -> str:
        return self._create_temp_view(self.input, "input") if type(self.input) is DataFrame \
            else str(self.input)

    @property
    def _error_cells(self) -> Optional[str]:
        if self.error_cells:
            df = self.error_cells if type(self.error_cells) is DataFrame else self._spark.table(str(self.error_cells))
            df = df.selectExpr(f'`{self._row_id}`', 'attribute')  # type: ignore
            return self._create_temp_view(df, "error_cells")
        return None

    @property
    def _repair_by_regex_enabled(self) -> bool:
        return not bool(self._get_option_value(*self._opt_repair_by_regex_disabled)) \
            and self.repair_by_rules

    @property
    def _repair_by_nearest_values_enabled(self) -> bool:
        return not bool(self._get_option_value(*self._opt_repair_by_nearest_values_disabled)) \
            and self.repair_by_rules and self.cf is not None

    @property
    def _repair_by_functional_deps_enabled(self) -> bool:
        return not bool(self._get_option_value(*self._opt_repair_by_functional_deps_disabled)) \
            and self.repair_by_rules

    def _delete_view_on_exit(self, view_name: str) -> None:
        self._intermediate_views_on_runtime.append(view_name)

    def _create_temp_view(self, df: Any, prefix: str) -> str:
        assert isinstance(df, DataFrame)
        view_name = get_random_string(prefix)
        df.createOrReplaceTempView(view_name)
        self._delete_view_on_exit(view_name)
        return view_name

    def _release_resources(self) -> None:
        while self._intermediate_views_on_runtime:
            v = self._intermediate_views_on_runtime.pop()
            _logger.debug(f"Dropping an auto-generated view: {v}")
            self._spark.sql(f"DROP VIEW IF EXISTS {v}")

    def _detect_errors(self, input_table: str, continous_columns: List[str]) -> Any:  # type: ignore
        error_model_params = {
            'row_id': self._row_id,
            'targets': self.targets,
            'discrete_thres': self.discrete_thres,
            'error_detectors': self.error_detectors,
            'error_cells': self._error_cells,
            'opts': self.opts
        }
        error_model = ErrorModel(**error_model_params)  # type: ignore
        return error_model.detect(input_table, continous_columns)

    def _prepare_repair_base_cells(
            self, input_table: str, noisy_cells_df: DataFrame, target_columns: List[str]) -> DataFrame:
        # Sets NULL at the detected noisy cells
        input_df = self._spark.table(input_table)
        num_input_rows = input_df.count()
        num_attrs = len(input_df.columns) - 1
        _logger.debug("{}/{} noisy cells found, then converts them into NULL cells...".format(
            noisy_cells_df.count(), num_input_rows * num_attrs))
        noisy_cells = self._create_temp_view(noisy_cells_df, "noisy_cells_v2")
        ret_as_json = json.loads(self._repair_api.convertErrorCellsToNull(
            input_table, noisy_cells, self._row_id, ",".join(target_columns)))

        repair_base_cells = ret_as_json["repair_base_cells"]
        self._delete_view_on_exit(repair_base_cells)

        return self._spark.table(repair_base_cells)

    def _split_clean_and_dirty_rows(
            self, repair_base_df: DataFrame, error_cells_df: DataFrame) -> Tuple[DataFrame, DataFrame]:
        error_rows_df = error_cells_df.selectExpr(f"`{self._row_id}`")
        clean_rows_df = repair_base_df.join(error_rows_df, self._row_id, "left_anti")
        dirty_rows_df = repair_base_df.join(error_rows_df, self._row_id, "left_semi")
        return clean_rows_df, dirty_rows_df

    def _empty_dataframe(self, schema: StructType) -> DataFrame:
        return self._spark.createDataFrame(self._spark.sparkContext.emptyRDD(), schema)

    def _empty_repaired_cells_dataframe(self, row_id_field: StructField) -> DataFrame:
        field_names = ['attribute', 'current_value', 'repaired']
        fields = [row_id_field] + list(map(lambda n: StructField(n, StringType()), field_names))
        return self._empty_dataframe(StructType(fields))

    def _create_cost_func(self) -> Any:
        broadcasted_cf = self._spark.sparkContext.broadcast(self.cf)

        @functions.pandas_udf("array<double>", functions.PandasUDFType.SCALAR)
        def cost_func(s1: pd.Series, s2: pd.Series) -> pd.Series:
            cf = broadcasted_cf.value

            result: List[Optional[List[float]]] = []
            for target, candidates in zip(s1, s2):
                if target and candidates is not None:
                    result.append([cf.compute(target, c) for c in candidates])  # type: ignore
                else:
                    result.append(None)

            return pd.Series(result)

        return cost_func

    def _repair_by_nearest_values(self, repair_base_df: DataFrame,
                                  error_cells_df: DataFrame,
                                  target_columns: List[str]) -> Tuple[DataFrame, DataFrame]:
        assert self.cf is not None

        cf_targets = self.cf.targets  # type: ignore
        targets = list(filter(lambda c: c in cf_targets, target_columns)) \
            if cf_targets else target_columns
        if not targets:
            row_id_field = error_cells_df.schema[self._row_id]
            return error_cells_df, self._empty_repaired_cells_dataframe(row_id_field)

        cost_func = self._create_cost_func()
        compute_dvs = lambda c: repair_base_df.where(f'`{c}` IS NOT NULL') \
            .selectExpr(f'"{c}" attribute', f'collect_set(`{c}`) dvs')
        domain_df = functools.reduce(lambda x, y: x.union(y), map(compute_dvs, targets))

        repair_merge_threshold = self._get_option_value(*self._opt_merge_threshold)

        compare_dvs = lambda x, y: \
            f"case when {x}.cost > {y}.cost then 1 " \
            f"when {x}.cost < {y}.cost then -1 " \
            "else 0 end"
        sorted_domain_value_expr = 'array_sort(dvs, ' \
            f'(left, right) -> {compare_dvs("left", "right")}) dvs'
        repair_expr = f'if(dvs[0].cost <= {repair_merge_threshold} AND dvs[0].cost < dvs[1].cost, ' \
            'dvs[0].value, null) repaired'
        error_cells_df = error_cells_df.join(domain_df, 'attribute', 'left_outer') \
            .withColumn("costs", cost_func(col("current_value"), col("dvs"))) \
            .selectExpr(f"`{self._row_id}`", "attribute", "current_value", 'dvs value', 'costs cost') \
            .selectExpr(f"`{self._row_id}`", "attribute", "current_value", 'arrays_zip(value, cost) dvs') \
            .selectExpr(f"`{self._row_id}`", "attribute", "current_value", sorted_domain_value_expr) \
            .selectExpr(f"`{self._row_id}`", "attribute", "current_value", repair_expr)

        repaired_cells_df = error_cells_df.where('repaired IS NOT NULL') \
            .selectExpr(f"`{self._row_id}`", "attribute", "current_value", "repaired")
        error_cells_df = error_cells_df.where('repaired IS NULL') \
            .selectExpr(f"`{self._row_id}`", "attribute", "current_value")

        return error_cells_df, repaired_cells_df

    def _repair_by_regex(self, regex: str, target: str, error_cells_df: DataFrame) -> DataFrame:
        error_cells = self._create_temp_view(error_cells_df, 'error_cells')
        jdf = self._repair_api.repairByRegularExpression(regex, target, error_cells, self._row_id)
        return DataFrame(jdf, self._spark._wrapped)  # type: ignore

    def _repair_by_regexs(self, repair_base_df: DataFrame,
                          error_cells_df: DataFrame,
                          target_columns: List[str]) -> Tuple[DataFrame, DataFrame]:
        regex_detectors = list(filter(lambda x: isinstance(x, RegExErrorDetector), self.error_detectors))
        if not regex_detectors:
            row_id_field = error_cells_df.schema[self._row_id]
            return error_cells_df, self._empty_repaired_cells_dataframe(row_id_field)

        regexs = list(map(lambda d: (d.attr, d.regex), regex_detectors))  # type: ignore
        _logger.info(f'[Repairing Phase] Repairing data using regexs: {to_list_str(regexs)}')

        dfs: List[DataFrame] = []
        for attr, regex in regexs:
            target_error_cells_df = error_cells_df.where(f"attribute = '{attr}'")
            df = self._repair_by_regex(regex, attr, target_error_cells_df)
            dfs.append(df.where('repaired IS NOT NULL'))

        # TODO: Might need to check if edit distances between `current_value` and `repaired`
        # are enough minimal for repairs.
        repaired_cells_df = functools.reduce(lambda x, y: x.union(y), dfs)
        error_cells_df = error_cells_df.join(repaired_cells_df, [self._row_id, 'attribute'], "left_anti")

        return error_cells_df, repaired_cells_df

    def _repair_by_rules(self, repair_base_df: DataFrame,
                         error_cells_df: DataFrame,
                         target_columns: List[str]) -> Tuple[DataFrame, DataFrame]:
        repaired_cells_dfs: List[DataFrame] = []

        # Adds an empty dataframe for unioning result repaired dataframes
        row_id_field = error_cells_df.schema[self._row_id]
        repaired_cells_dfs.append(self._empty_repaired_cells_dataframe(row_id_field))

        if self._repair_by_regex_enabled:
            error_cells_df, repaired_by_regex_df = \
                self._repair_by_regexs(repair_base_df, error_cells_df, target_columns)
            repaired_cells_dfs.append(repaired_by_regex_df)

        if self._repair_by_nearest_values_enabled:
            error_cells_df, repaired_by_nv_df = \
                self._repair_by_nearest_values(repair_base_df, error_cells_df, target_columns)
            repaired_cells_dfs.append(repaired_by_nv_df)

        repaired_by_rules_df = functools.reduce(lambda x, y: x.union(y), repaired_cells_dfs)
        return error_cells_df, repaired_by_rules_df

    # Selects relevant features if necessary. To reduce model training time,
    # it is important to drop non-relevant in advance.
    def _select_features(self, pairwise_attr_stats: Dict[str, str], y: str, features: List[str]) -> List[str]:
        max_training_column_num = int(self._get_option_value(*self._opt_max_training_column_num))

        if max_training_column_num < len(features) and \
                y in pairwise_attr_stats:
            heap: List[Tuple[float, str]] = []
            for f, corr in map(lambda x: tuple(x), pairwise_attr_stats[y]):  # type: ignore
                if f in features:
                    heapq.heappush(heap, (float(corr), f))

            fts = [heapq.heappop(heap) for i in range(len(heap))]
            top_k_fts: List[Tuple[float, str]] = []
            for corr, f in fts:  # type: ignore
                # TODO: Parameterize a correlation threshold to filter out irrelevant features
                if len(top_k_fts) <= 1 or (float(corr) >= 0.0 and len(top_k_fts) < max_training_column_num):
                    top_k_fts.append((float(corr), f))

            _logger.info("[Repair Model Training Phase] {} features ({}) selected from {} features".format(
                len(top_k_fts), to_list_str(list(map(lambda f: f"{f[1]}:{f[0]}", top_k_fts))), len(features)))

            features = list(map(lambda f: f[1], top_k_fts))

        return features

    def _create_transformers(self, domain_stats: Dict[str, str], features: List[str],
                             continous_columns: List[str]) -> List[Any]:
        # Transforms discrete attributes with some categorical encoders if necessary
        import category_encoders as ce  # type: ignore[import]
        discrete_columns = [c for c in features if c not in continous_columns]
        transformers = []

        small_domain_threshold = int(self._get_option_value(*self._opt_small_domain_threshold))

        if len(discrete_columns) != 0:
            # TODO: Needs to reconsider feature transformation in this part, e.g.,
            # we can use `ce.OrdinalEncoder` for small domain features. For the other category
            # encoders, see https://github.com/scikit-learn-contrib/category_encoders
            small_domain_columns = [
                c for c in discrete_columns
                if int(domain_stats[c]) < small_domain_threshold]  # type: ignore
            discrete_columns = [
                c for c in discrete_columns if c not in small_domain_columns]
            if len(small_domain_columns) > 0:
                transformers.append(ce.SumEncoder(
                    cols=small_domain_columns, handle_unknown='impute'))
            if len(discrete_columns) > 0:
                transformers.append(ce.OrdinalEncoder(
                    cols=discrete_columns, handle_unknown='impute'))

        # TODO: Even when using a GDBT, it might be better to standardize
        # continous values.

        return transformers

    def _build_rule_model(self, train_df: DataFrame, target_columns: List[str], x: str, y: str) -> Any:
        # TODO: For attributes having large domain size, we need to rewrite it as a join query to repair data
        input_view = self._create_temp_view(train_df, 'rule_model_input')
        func_deps = json.loads(self._repair_api.computeFunctionalDepMap(input_view, x, y))
        return FunctionalDepModel(x, func_deps)

    def _get_functional_deps(self, train_df: DataFrame, target_columns: List[str],
                             continous_columns: List[str]) -> Optional[Dict[str, List[str]]]:
        constraint_detectors = list(filter(lambda x: isinstance(x, ConstraintErrorDetector), self.error_detectors))
        # TODO: Supports the case where `self.error_detectors` has multiple `ConstraintErrorDetector`s
        if len(constraint_detectors) == 1:
            input_view = self._create_temp_view(train_df, 'input_to_compute_fdeps')
            ced = constraint_detectors[0]
            constraint_targets = list(filter(lambda c: c in ced.targets, target_columns)) \
                if ced.targets else target_columns
            func_deps = json.loads(self._repair_api.computeFunctionalDeps(
                input_view, ced.constraint_path, ced.constraints, ",".join(constraint_targets)))  # type: ignore
            return func_deps
        elif len(constraint_detectors) >= 1:
            _logger.warning(f'Multiple constraint classes not supported for detecting functional deps')
            return None
        else:
            return None

    def _sample_training_data_from(self, df: DataFrame, training_data_num: int) -> DataFrame:
        # The value of `_opt_max_training_row_num` highly depends on
        # the performance of pandas and LightGBM.
        max_training_row_num = int(self._get_option_value(*self._opt_max_training_row_num))
        sampling_ratio = 1.0
        if training_data_num > max_training_row_num:
            sampling_ratio = float(max_training_row_num) / training_data_num
            _logger.info(f'To reduce training data, extracts {sampling_ratio * 100.0}% samples '
                         f'from {training_data_num} rows')

        # TODO: Needs more smart sampling, e.g., stratified sampling
        return df.sample(sampling_ratio)

    def _build_repair_stat_models_in_series(
            self, models: Dict[str, Any], train_df: DataFrame,
            target_columns: List[str], continous_columns: List[str],
            num_class_map: Dict[str, int],
            feature_map: Dict[str, List[str]],
            transformer_map: Dict[str, List[Any]]) -> Dict[str, Any]:
        for y in [c for c in target_columns if c not in models]:
            index = len(models) + 1
            df = train_df.where(f"`{y}` IS NOT NULL")
            training_data_num = df.count()
            # Number of training data must be positive
            if training_data_num == 0:
                _logger.info("Skipping {}/{} model... type=classfier y={} num_class={}".format(
                    index, len(target_columns), y, num_class_map[y]))
                models[y] = (PoorModel(None), feature_map[y], None)
                continue

            train_pdf = self._sample_training_data_from(df, training_data_num).toPandas()
            is_discrete = y not in continous_columns
            model_type = "classfier" if is_discrete else "regressor"

            X = train_pdf[feature_map[y]]  # type: ignore
            for transformer in transformer_map[y]:
                X = transformer.fit_transform(X)
            _logger.debug("{} encoders transform ({})=>({})".format(
                len(transformer_map[y]), to_list_str(feature_map[y]), to_list_str(X.columns)))

            # Re-balance target classes in training data
            X, y_ = rebalance_training_data(X, train_pdf[y], y) \
                if is_discrete and self.training_data_rebalancing_enabled \
                else (X, train_pdf[y])

            _logger.info("Building {}/{} model... type={} y={} features={} #rows={}{}".format(
                index, len(target_columns), model_type,
                y, to_list_str(feature_map[y]),
                len(train_pdf),
                f" #class={num_class_map[y]}" if num_class_map[y] > 0 else ""))
            (model, score), elapsed_time = build_model(X, y_, is_discrete, num_class_map[y], n_jobs=-1, opts=self.opts)
            if model is None:
                model = PoorModel(None)

            class_nrow_stdv = compute_class_nrow_stdv(y_, is_discrete)
            _logger.info("Finishes building '{}' model...  score={} elapsed={}s".format(
                y, score, elapsed_time))

            models[y] = (model, feature_map[y], transformer_map[y])

        return models

    def _build_repair_stat_models_in_parallel(
            self, models: Dict[str, Any], train_df: DataFrame,
            target_columns: List[str], continous_columns: List[str],
            num_class_map: Dict[str, int],
            feature_map: Dict[str, List[str]],
            transformer_map: Dict[str, List[Any]]) -> Dict[str, Any]:
        # To build repair models in parallel, it assigns each model training into a single task
        train_dfs_per_target: List[DataFrame] = []
        target_column = get_random_string("target_column")

        for y in [c for c in target_columns if c not in models]:
            index = len(models) + len(train_dfs_per_target) + 1
            df = train_df.where(f"`{y}` IS NOT NULL")
            training_data_num = df.count()
            # Number of training data must be positive
            if training_data_num == 0:
                _logger.info("Skipping {}/{} model... type=classfier y={} num_class={}".format(
                    index, len(target_columns), y, num_class_map[y]))
                models[y] = (PoorModel(None), feature_map[y], None)
                continue

            df = self._sample_training_data_from(df, training_data_num)
            train_dfs_per_target.append(df.withColumn(target_column, functions.lit(y)))

            # TODO: Removes duplicate feature transformations
            train_pdf = df.toPandas()
            X = train_pdf[feature_map[y]]  # type: ignore
            transformers = transformer_map[y]
            for transformer in transformers:
                X = transformer.fit_transform(X)
            _logger.debug("{} encoders transform ({})=>({})".format(
                len(transformers), to_list_str(feature_map[y]), to_list_str(X.columns)))

            _logger.info("Start building {}/{} model in parallel... type={} y={} features={} #rows={}{}".format(
                index, len(target_columns),
                "classfier" if y not in continous_columns else "regressor",
                y, to_list_str(feature_map[y]),
                len(train_pdf),
                f" #class={num_class_map[y]}" if num_class_map[y] > 0 else ""))

        num_tasks = len(train_dfs_per_target)
        if num_tasks == 0:
            return models

        # TODO: A larger `training_n_jobs` value can cause high pressure on executors
        def _num_cores_per_executor() -> int:
            try:
                num_parallelism = self._spark.sparkContext.defaultParallelism
                num_executors = self._spark._jsc.sc().getExecutorMemoryStatus().size()  # type: ignore
                return max(1, num_parallelism / num_executors)
            except:
                return 1

        training_n_jobs = max(1, int(_num_cores_per_executor() / num_tasks))
        _logger.debug(f"Setting {training_n_jobs} to `n_jobs` for training in parallel")

        broadcasted_target_column = self._spark.sparkContext.broadcast(target_column)
        broadcasted_continous_columns = self._spark.sparkContext.broadcast(continous_columns)
        broadcasted_feature_map = self._spark.sparkContext.broadcast(feature_map)
        broadcasted_transformer_map = self._spark.sparkContext.broadcast(transformer_map)
        broadcasted_num_class_map = self._spark.sparkContext.broadcast(num_class_map)
        broadcasted_training_data_rebalancing_enabled = \
            self._spark.sparkContext.broadcast(self.training_data_rebalancing_enabled)
        broadcasted_n_jobs = self._spark.sparkContext.broadcast(training_n_jobs)
        broadcasted_opts = self._spark.sparkContext.broadcast(self.opts)

        @functions.pandas_udf("target: STRING, model: BINARY, score: DOUBLE, elapsed: DOUBLE, nrows: INT, stdv: DOUBLE",
                              functions.PandasUDFType.GROUPED_MAP)
        def train(pdf: pd.DataFrame) -> pd.DataFrame:
            target_column = broadcasted_target_column.value
            y = pdf.at[0, target_column]
            continous_columns = broadcasted_continous_columns.value
            features = broadcasted_feature_map.value[y]
            transformers = broadcasted_transformer_map.value[y]
            is_discrete = y not in continous_columns
            num_class = broadcasted_num_class_map.value[y]
            training_data_rebalancing_enabled = broadcasted_training_data_rebalancing_enabled.value
            n_jobs = broadcasted_n_jobs.value
            opts = broadcasted_opts.value

            X = pdf[features]
            for transformer in transformers:
                X = transformer.transform(X)

            # Re-balance target classes in training data
            X, y_ = rebalance_training_data(X, pdf[y], y) if is_discrete and training_data_rebalancing_enabled \
                else (X, pdf[y])

            ((model, score), elapsed_time) = build_model(X, y_, is_discrete, num_class, n_jobs, opts)
            if model is None:
                model = PoorModel(None)

            class_nrow_stdv = compute_class_nrow_stdv(y_, is_discrete)
            row = [y, pickle.dumps(model), score, elapsed_time, len(X), class_nrow_stdv]
            return pd.DataFrame([row])

        # TODO: Any smart way to distribute tasks in different physical machines?
        built_models = functools.reduce(lambda x, y: x.union(y), train_dfs_per_target) \
            .groupBy(target_column).apply(train).collect()
        for row in built_models:
            tpe = "classfier" if row.target not in continous_columns else "regressor"
            _logger.info("Finishes building '{}' model... score={} elapsed={}s".format(
                row.target, row.score, row.elapsed))

            model = pickle.loads(row.model)
            features = feature_map[row.target]
            transformers = transformer_map[row.target]
            models[row.target] = (model, features, transformers)

        return models

    def _resolve_prediction_order(self, models: Dict[str, Any], target_columns: List[str]) -> List[Any]:
        pred_ordered_models = []
        error_columns = copy.deepcopy(target_columns)

        # Appends no order-dependent models first
        for y in target_columns:
            (model, x, transformers) = models[y]
            if not isinstance(model, FunctionalDepModel):
                pred_ordered_models.append((y, models[y]))
                error_columns.remove(y)

        # Resolves an order for predictions
        while len(error_columns) > 0:
            columns = copy.deepcopy(error_columns)
            for y in columns:
                (model, x, transformers) = models[y]
                if x[0] not in error_columns:
                    pred_ordered_models.append((y, models[y]))
                    error_columns.remove(y)

            assert len(error_columns) < len(columns)

        _logger.info("Resolved prediction order dependencies: {}".format(
            to_list_str(list(map(lambda x: x[0], pred_ordered_models)))))
        assert len(pred_ordered_models) == len(target_columns)
        return pred_ordered_models

    @spark_job_group(name="repair model training")
    def _build_repair_models(self, train_df: DataFrame, target_columns: List[str], continous_columns: List[str],
                             domain_stats: Dict[str, str],
                             pairwise_attr_stats: Dict[str, str]) -> List[Any]:
        # We now employ a simple repair model based on the SCARE paper [2] for scalable processing
        # on Apache Spark. In the paper, given a database tuple t = ce (c: correct attribute values,
        # e: error attribute values), the conditional probability of each combination of the
        # error attribute values c can be computed using the product rule:
        #
        #  P(e\|c)=P(e[E_{1}]\|c)\prod_{i=2}^{|e|}P(e[E_{i}]\|c, r_{1}, ..., r_{i-1})
        #      , where r_{j} = if j = 1 then \arg\max_{e[E_{j}]} P(e[E_{j}]\|c)
        #                      else \arg\max_{e[E_{j}]} P(e[E_{j}]\|c, r_{1}, ..., r_{j-1})
        #
        # {E_{1}, ..., E_{|e|}} is an order to repair error attributes and it is determined by
        # a dependency graph of attributes. The SCARE repair model splits a database instance
        # into two parts: a subset D_{c} \subset D of clean (or correct) tuples and
        # D_{e} = D − D_{c} represents the remaining possibly dirty tuples.
        # Then, it trains the repair model P(e\|c) by using D_{c} and the model is used
        # to predict error attribute values in D_{e}.
        #
        # In our repair model, two minor improvements below are applied to enhance
        # precision and training speeds:
        #
        # - (1) Use NULL/weak-labeled cells for repair model training
        # - (2) Use functional dependency if possible
        #
        # In our model, we strongly assume error detectors can enumerate all the error cells,
        # that is, we can assume that non-blank cells are clean. Therefore, if c[x] -> e[y] in P(e[y]\|c)
        # and c[x] \in c (the value e[y] is determined by the value c[x]), we simply folow
        # this rule to skip expensive training costs.
        train_df = train_df.drop(self._row_id).cache()

        # If `self.repair_by_rules` is `True`, try to analyze functional deps on training data.
        # TODO: Moves this block into `self._repair_by_rules``
        functional_deps = self._get_functional_deps(train_df, target_columns, continous_columns) \
            if self._repair_by_functional_deps_enabled else None
        if functional_deps is not None:
            _logger.debug(f"Functional deps found: {functional_deps}")

        # Builds multiple repair models to repair error cells
        _logger.info("[Repair Model Training Phase] Building {} models "
                     "to repair the cells in {}".format(len(target_columns), to_list_str(target_columns)))

        models: Dict[str, Any] = {}
        num_class_map: Dict[str, int] = {}

        for y in target_columns:
            index = len(models) + 1
            input_columns = [c for c in train_df.columns if c != y]  # type: ignore
            is_discrete = y not in continous_columns
            num_class_map[y] = train_df.selectExpr(f"count(distinct `{y}`) cnt").collect()[0].cnt \
                if is_discrete else 0

            # Skips building a model if num_class <= 1
            if is_discrete and num_class_map[y] <= 1:
                _logger.info("Skipping {}/{} model... type=rule y={} num_class={}".format(
                    index, len(target_columns), y, num_class_map[y]))
                v = train_df.selectExpr(f"first(`{y}`) value").collect()[0].value \
                    if num_class_map[y] == 1 else None
                models[y] = (PoorModel(v), input_columns, None)

            # If `y` is functionally-dependent on one of clean attributes,
            # builds a model based on the rule.
            if y not in models and functional_deps is not None and y in functional_deps:
                def _qualified(x: str) -> bool:
                    # Checks if the domain size of `x` is small enough
                    return int(domain_stats[x]) < int(self._get_option_value(*self._opt_max_domain_size))

                fx = list(filter(lambda x: _qualified(x), functional_deps[y]))
                if len(fx) > 0:
                    _logger.info("Building {}/{} model... type=rule(FD: X->y)  y={}(|y|={}) X={}(|X|={})".format(
                        index, len(target_columns), y, num_class_map[y], fx[0], domain_stats[fx[0]]))
                    model = self._build_rule_model(train_df, target_columns, fx[0], y)
                    models[y] = (model, [fx[0]], None)

        if len(models) != len(target_columns):
            # Selects features among input columns if necessary
            feature_map: Dict[str, List[str]] = {}
            transformer_map: Dict[str, List[Any]] = {}
            for y in [c for c in target_columns if c not in models]:
                input_columns = [c for c in train_df.columns if c != y]  # type: ignore
                features = self._select_features(pairwise_attr_stats, y, input_columns)  # type: ignore
                feature_map[y] = features
                transformer_map[y] = self._create_transformers(domain_stats, features, continous_columns)

            build_stat_models = self._build_repair_stat_models_in_parallel \
                if self.parallel_stat_training_enabled else self._build_repair_stat_models_in_series
            models = build_stat_models(
                models, train_df, target_columns, continous_columns,
                num_class_map, feature_map, transformer_map)

        assert len(models) == len(target_columns)

        # Resolve the conflict dependencies of the predictions
        if any(isinstance(m, FunctionalDepModel) for m, _, _ in models.values()):
            return self._resolve_prediction_order(models, target_columns)

        return list(models.items())

    def _group_apply(self, df: DataFrame, udf: Any) -> DataFrame:
        num_parallelism = self._spark.sparkContext.defaultParallelism
        grouping_key = get_random_string("grouping_key")
        return df.withColumn(grouping_key, (functions.rand() * functions.lit(num_parallelism)).cast("int")) \
            .groupBy(grouping_key).apply(udf)

    # TODO: What is the best way to repair appended new data if we have already
    # clean (or repaired) data?
    @spark_job_group(name="repairing")
    def _repair(self, models: List[Any], continous_columns: List[str],
                dirty_rows_df: DataFrame, error_cells_df: DataFrame,
                compute_repair_candidate_prob: bool, maximal_likelihood_repair: bool) -> pd.DataFrame:
        # Shares all the variables for the learnt models in a Spark cluster
        broadcasted_columns = self._spark.sparkContext.broadcast(dirty_rows_df.columns)
        broadcasted_continous_columns = self._spark.sparkContext.broadcast(continous_columns)
        broadcasted_models = self._spark.sparkContext.broadcast(models)
        broadcasted_compute_repair_candidate_prob = \
            self._spark.sparkContext.broadcast(compute_repair_candidate_prob)
        broadcasted_maximal_likelihood_repair = \
            self._spark.sparkContext.broadcast(maximal_likelihood_repair)

        # Creates a dict that checks if a column's type is integral or not
        def _create_integral_column_map(schema) -> Dict[str, Any]:  # type: ignore
            def _to_np_types(dt):  # type: ignore
                if dt == ByteType():
                    return np.int8
                elif dt == ShortType():
                    return np.int16
                elif dt == IntegerType():
                    return np.int32
                elif dt == LongType():
                    return np.int64
                return None

            cols = map(lambda f: (f.name, _to_np_types(f.dataType)), schema.fields)
            return dict(filter(lambda f: f[1] is not None, cols))

        integral_column_map = _create_integral_column_map(dirty_rows_df.schema)
        broadcasted_integral_column_map = self._spark.sparkContext.broadcast(integral_column_map)

        # TODO: Runs the `repair` UDF based on checkpoint files
        @functions.pandas_udf(dirty_rows_df.schema, functions.PandasUDFType.GROUPED_MAP)
        def repair(pdf: pd.DataFrame) -> pd.DataFrame:
            columns = broadcasted_columns.value
            continous_columns = broadcasted_continous_columns.value
            integral_column_map = broadcasted_integral_column_map.value
            models = broadcasted_models.value
            compute_repair_candidate_prob = broadcasted_compute_repair_candidate_prob.value
            maximal_likelihood_repair = broadcasted_maximal_likelihood_repair.value

            # An internal PMF format is like '{"classes": ["dog", "cat"], "probs": [0.76, 0.24]}'
            need_to_compute_pmf = compute_repair_candidate_prob or maximal_likelihood_repair

            for m in models:
                (y, (model, features, transformers)) = m

                # Preprocesses the input row for prediction
                X = pdf[features]

                # Transforms an input row to a feature
                if transformers:
                    for transformer in transformers:
                        X = transformer.transform(X)

                if need_to_compute_pmf and y not in continous_columns:
                    # TODO: Filters out top-k values to reduce the amount of data
                    predicted = model.predict_proba(X)

                    def _to_dict(probs):  # type: ignore
                        return {"classes": model.classes_.tolist(), "probs": probs.tolist()} if probs is not None \
                            else {"classes": [], "probs": []}

                    pmf = map(lambda p: _to_dict(p), predicted)
                    pmf = map(lambda p: json.dumps(p), pmf)  # type: ignore
                    pdf[y] = pdf[y].where(pdf[y].notna(), list(pmf))
                else:
                    predicted = model.predict(X)
                    predicted = predicted if y not in integral_column_map \
                        else np.round(predicted).astype(integral_column_map[y])
                    pdf[y] = pdf[y].where(pdf[y].notna(), predicted)

            return pdf[columns]

        # Predicts the remaining error cells based on the trained models.
        # TODO: Might need to compare repair costs (cost of an update, c) to
        # the likelihood benefits of the updates (likelihood benefit of an update, l).
        _logger.info(f"[Repairing Phase] Computing {error_cells_df.count()} repair updates in "
                     f"{dirty_rows_df.count()} rows...")
        repaired_df = self._group_apply(dirty_rows_df, repair)
        return repaired_df

    def _compute_weighted_probs(self, pmf_df: DataFrame) -> DataFrame:
        assert self.cf is not None

        pmf_weight = float(self._get_option_value(*self._opt_cost_weight))
        cost_func = self._create_cost_func()
        # TODO: Rethinks the way to compute weighted probs here
        to_weighted_probs = "if(costs IS NOT NULL, zip_with(probs, costs, " \
            f"(p, c) -> p * (1.0 / (1.0 + {pmf_weight} * c))), probs)"
        if self.cf.targets:  # type: ignore
            _logger.info(f'[Repairing Phase] {self.cf} computing weighting probs...')
            cf_targets = to_list_str(self.cf.targets, quote=True)  # type: ignore
            to_weighted_probs = f"if(attribute IN ({cf_targets}), {to_weighted_probs}, probs)"

        sum_probs = "aggregate(probs, double(0.0), (acc, x) -> acc + x) norm"
        normalize_probs = "transform(probs, p -> p / norm) probs"
        weighted_pmf_df = pmf_df.withColumn("costs", cost_func(col("current_value"), col("classes"))) \
            .selectExpr(f"`{self._row_id}`", "attribute", "current_value", "classes", f"{to_weighted_probs} probs") \
            .selectExpr(f"`{self._row_id}`", "attribute", "current_value", "classes", "probs", sum_probs) \
            .selectExpr(f"`{self._row_id}`", "attribute", "current_value", "classes", normalize_probs)

        return weighted_pmf_df

    def _flatten(self, input_table: str) -> DataFrame:
        jdf = self._jvm.RepairMiscApi.flattenTable("", input_table, self._row_id)
        return DataFrame(jdf, self._spark._wrapped)  # type: ignore

    def _filter_columns_from(self, df: DataFrame, targets: List[str], negate: bool = False) -> DataFrame:
        return df.where("attribute {} ({})".format("NOT IN" if negate else "IN", to_list_str(targets, quote=True)))

    def _compute_repair_pmf(self, repaired_rows_df: DataFrame, error_cells_df: DataFrame,
                            continous_columns: List[str]) -> DataFrame:
        # Extracts predicted cells from `repaired_rows_df`
        repaired_cells_df = self._flatten(self._create_temp_view(repaired_rows_df, 'repaired')) \
            .join(error_cells_df, [self._row_id, "attribute"], "inner")

        # Since we cannot compute pmfs for continouos values, their columns need
        # to be filtered out first.
        discrete_repaired_cells_df = repaired_cells_df if len(continous_columns) == 0 \
            else self._filter_columns_from(repaired_cells_df, continous_columns, negate=True)

        parse_pmf_json_expr = "from_json(value, 'classes array<string>, probs array<double>') pmf"
        slice_probs = "slice(pmf.probs, 1, size(pmf.classes)) probs"
        pmf_df = discrete_repaired_cells_df \
            .selectExpr(f"`{self._row_id}`", "attribute", "current_value", parse_pmf_json_expr) \
            .selectExpr(f"`{self._row_id}`", "attribute", "current_value", "pmf.classes classes", slice_probs)

        # If `self.cf` defined, computes weighted probs using it
        if self.cf is not None:
            pmf_df = self._compute_weighted_probs(pmf_df)

        # Concatenates `classes` and `probs` for pmfs then sorts pmfs by their probs
        to_current_expr = "named_struct('value', current_value, 'prob', " \
            "coalesce(prob[array_position(class, current_value) - 1], 0.0)) current_value"
        compare_probs = lambda x, y: \
            f"case when {x}.prob < {y}.prob then 1 " \
            f"when {x}.prob > {y}.prob then -1 " \
            "else 0 end"
        sorted_pmf_expr = f'array_sort(pmf, (left, right) -> {compare_probs("left", "right")}) pmf'
        pmf_df = pmf_df.selectExpr(f"`{self._row_id}`", "attribute", 'current_value', 'classes class', 'probs prob') \
            .selectExpr(f"`{self._row_id}`", "attribute", to_current_expr, 'arrays_zip(class, prob) pmf') \
            .selectExpr(f"`{self._row_id}`", "attribute", "current_value", sorted_pmf_expr)

        # Filters less-confident candidates in `pmf`
        pmf_threshold = self._get_option_value(*self._opt_prob_threshold)
        pmf_top_k = self._get_option_value(*self._opt_prob_top_k)
        filtered_prob_expr = f"slice(filter(pmf, x -> x.prob > {pmf_threshold}), 1, {pmf_top_k}) pmf"
        pmf_df = pmf_df.selectExpr(
            f"`{self._row_id}`", "attribute", "current_value",
            filtered_prob_expr)

        # Appends rows for continous values if necessary
        if len(continous_columns) > 0:
            continous_repaired_cells_df = self._filter_columns_from(repaired_cells_df, continous_columns, negate=False)
            continous_to_pmf_expr = "array(named_struct('class', value, 'prob', 1.0D)) pmf"
            to_current_expr = "named_struct('value', current_value, 'prob', 0.0D) current_value"
            continous_pmf_df = continous_repaired_cells_df \
                .selectExpr(f"`{self._row_id}`", "attribute", to_current_expr, continous_to_pmf_expr)
            pmf_df = pmf_df.union(continous_pmf_df)

        assert pmf_df.count() == error_cells_df.count()
        return pmf_df

    def _compute_score(self, pmf_df: DataFrame, error_cells_df: DataFrame) -> DataFrame:
        assert self.cf is not None

        broadcasted_cf = self._spark.sparkContext.broadcast(self.cf)

        @functions.pandas_udf("double")  # type: ignore
        def cost_func(xs: pd.Series, ys: pd.Series) -> pd.Series:
            cf = broadcasted_cf.value
            dists = [cf.compute(x, y) for x, y in zip(xs, ys)]
            return pd.Series(dists)

        maximal_likelihood_repair_expr = "named_struct('value', pmf[0].class, 'prob', pmf[0].prob) repaired"
        current_expr = "IF(ISNOTNULL(current_value.value), current_value.value, repaired.value)"
        score_expr = "ln(repaired.prob / IF(current_value.prob > 0.0, current_value.prob, 1e-6)) *" \
            "(1.0 / (1.0 + coalesce(cost, 256.0))) score"
        score_df = pmf_df \
            .selectExpr(f"`{self._row_id}`", "attribute", "current_value", maximal_likelihood_repair_expr) \
            .withColumn("cost", cost_func(expr(current_expr), col("repaired.value"))) \
            .selectExpr(f"`{self._row_id}`", "attribute", "current_value.value current_value",
                        "repaired.value repaired", score_expr)

        return score_df

    def _repair_attrs(self, repair_updates: Union[str, DataFrame], base_table: Union[str, DataFrame]) -> DataFrame:
        repair_updates = self._create_temp_view(repair_updates, "repair_updates") \
            if type(repair_updates) is DataFrame else repair_updates
        base_table = self._create_temp_view(base_table, "base_table") \
            if type(base_table) is DataFrame else base_table
        jdf = self._jvm.RepairMiscApi.repairAttrsFrom(
            repair_updates, "", base_table, self._row_id)
        return DataFrame(jdf, self._spark._wrapped)  # type: ignore

    def _maximal_likelihood_repair(self, score_df: DataFrame, error_cells_df: DataFrame) -> DataFrame:
        # A “Maximal Likelihood Repair” problem defined in the SCARE [2] paper is as follows;
        # Given a scalar \delta and a database D = D_{e} \cup D_{c}, the problem is to
        # find another database instance D' = D'_{e} \cup D_{c} such that L(D'_{e} \| D_{c})
        # is maximum subject to the constraint Cost(D, D') <= \delta.
        # L is a likelihood function and Cost is an arbitrary update cost function
        # (e.g., edit distances) between the two database instances D and D'.
        assert self.repair_delta is not None
        num_error_cells = error_cells_df.count()
        percent = min(1.0, 1.0 - self.repair_delta / num_error_cells)
        percentile = score_df.selectExpr(f"percentile(score, {percent}) thres").collect()[0]
        top_delta_repairs_df = score_df.where(f"score >= {percentile.thres}").drop("score")
        _logger.info("[Repairing Phase] {} repair updates (delta={}) selected "
                     "among {} candidates".format(
                         top_delta_repairs_df.count(),
                         self.repair_delta,
                         num_error_cells))

        return top_delta_repairs_df

    # Since statistical models notoriously ignore specified integrity constraints,
    # this methods checks if constraints hold in the repair candidates.
    @spark_job_group(name="validating")
    def _validate_repairs(self, repair_candidates: DataFrame, clean_rows: DataFrame) -> DataFrame:
        _logger.info("[Validation Phase] Validating {} repair candidates...".format(repair_candidates.count()))
        # TODO: Implements a logic to check if constraints hold on the repair candidates
        return repair_candidates

    @elapsed_time  # type: ignore
    def _run(self, input_table: str, continous_columns: List[str], detect_errors_only: bool,
             compute_repair_candidate_prob: bool,
             compute_repair_prob: bool, compute_repair_score: bool,
             repair_data: bool, maximal_likelihood_repair: bool) -> DataFrame:

        #################################################################################
        # 1. Error Detection Phase
        #################################################################################
        _logger.info(f'[Error Detection Phase] Detecting errors in a table `{input_table}`... ')

        error_cells_df, target_columns, pairwise_attr_stats, domain_stats = \
            self._detect_errors(input_table, continous_columns)

        # If `detect_errors_only` is True, returns found error cells
        if detect_errors_only:
            return error_cells_df

        # If no error found, we don't need to do nothing
        if error_cells_df.count() == 0:  # type: ignore
            _logger.info("Any error cell not found, so the input data is already clean")
            return self._spark.table(input_table) if repair_data \
                else self._empty_dataframe(error_cells_df.schema)

        if len(target_columns) == 0:
            raise ValueError("At least one valid discretizable feature is needed to repair error cells, "
                             "but no such feature found")

        # Filters out non-repairable columns from `error_cells_df`
        error_cells_df = self._filter_columns_from(error_cells_df, target_columns)

        #################################################################################
        # 2. Repair Model Training Phase
        #################################################################################

        # Clear out error cells (to NULL) first
        repair_base_df = self._prepare_repair_base_cells(input_table, error_cells_df, target_columns)

        # Refines the repair base table to extract more clean data using a specified cost function
        if self.repair_by_rules:
            error_cells_df, repaired_by_rules_df = self._repair_by_rules(repair_base_df, error_cells_df, target_columns)
            repair_base_df = self._repair_attrs(repaired_by_rules_df, repair_base_df)

        # Selects rows for training, building models, and repairing cells
        clean_rows_df, dirty_rows_df = \
            self._split_clean_and_dirty_rows(repair_base_df, error_cells_df)

        models = self._build_repair_models(
            repair_base_df, target_columns, continous_columns,
            domain_stats, pairwise_attr_stats)

        #################################################################################
        # 3. Repair Phase
        #################################################################################

        # TODO: Could we refine repair candidates by considering given integrity constraints? (See [15])
        repaired_rows_df = self._repair(
            models, continous_columns, dirty_rows_df, error_cells_df,
            compute_repair_candidate_prob,
            maximal_likelihood_repair)

        # If `compute_repair_candidate_prob` is True, returns probability mass function
        # of repair candidates.
        if compute_repair_candidate_prob and not maximal_likelihood_repair:
            assert not self._repair_by_nearest_values_enabled, \
                'repairing data by nearest values not supported in this path'

            pmf_df = self._compute_repair_pmf(repaired_rows_df, error_cells_df, continous_columns)
            pmf_df = pmf_df.selectExpr(f"`{self._row_id}`", "attribute", "current_value.value AS current_value", "pmf")

            # If `compute_repair_prob` is true, returns a predicted repair with
            # the highest probability only.
            if compute_repair_prob:
                return pmf_df.selectExpr(
                    f"`{self._row_id}`", "attribute", "current_value",
                    "pmf[0].class AS repaired",
                    "pmf[0].prob AS prob")

            return pmf_df

        # If any discrete target columns and its probability distribution given,
        # computes scores to decide which cells should be repaired to follow the
        # “Maximal Likelihood Repair” problem.
        if maximal_likelihood_repair:
            assert len(continous_columns) == 0
            assert len(self.cf.targets) == 0  # type: ignore

            assert not self._repair_by_nearest_values_enabled, \
                'repairing data by nearest values not supported in this path'

            pmf_df = self._compute_repair_pmf(repaired_rows_df, error_cells_df, [])
            score_df = self._compute_score(pmf_df, error_cells_df)
            if compute_repair_score:
                return score_df

            top_delta_repairs_df = self._maximal_likelihood_repair(score_df, error_cells_df)
            if not repair_data:
                return top_delta_repairs_df

            # If `repair_data` is True, applys the selected repair updates into `dirty_rows`
            repaired_rows_df = self._repair_attrs(
                self._create_temp_view(top_delta_repairs_df, "top_delta_repairs"),
                self._create_temp_view(dirty_rows_df, "dirty_rows"))

        if repair_data:
            clean_df = clean_rows_df.union(repaired_rows_df)
            assert clean_df.count() == self._spark.table(input_table).count()
            return clean_df.cache()

        # If `repair_data` is False, returns repair candidates whoes
        # value is not the same with `current_value`.
        repair_candidates_df = self._flatten(self._create_temp_view(repaired_rows_df, 'repaired')) \
            .join(error_cells_df, [self._row_id, "attribute"], "inner") \
            .selectExpr(f"`{self._row_id}`", "attribute", "current_value", "value repaired") \
            .where("repaired IS NULL OR NOT(current_value <=> repaired)")

        repair_candidates_df = repair_candidates_df.union(repaired_by_rules_df) \
            if self.repair_by_rules else repair_candidates_df
        repair_candidates_df = self._validate_repairs(repair_candidates_df, clean_rows_df) \
            if self.repair_validation_enabled else repair_candidates_df

        return repair_candidates_df.cache()

    def _check_input_table(self) -> Tuple[str, List[str]]:
        ret_as_json = json.loads(self._repair_api.checkInputTable(self.db_name, self._input_table, self._row_id))
        input_table = ret_as_json["input_table"]
        continous_columns = ret_as_json["continous_attrs"].split(",")

        _logger.info("input_table: {} ({} rows x {} columns)".format(
            input_table, self._spark.table(input_table).count(),
            len(self._spark.table(input_table).columns) - 1))

        return input_table, continous_columns if continous_columns != [""] else []

    def run(self, detect_errors_only: bool = False, compute_repair_candidate_prob: bool = False,
            compute_repair_prob: bool = False, compute_repair_score: bool = False,
            repair_data: bool = False, maximal_likelihood_repair: bool = False) -> DataFrame:
        """
        Starts processing to detect error cells in given input data and build a statistical
        model to repair them.

        .. versionchanged:: 0.1.0

        Parameters
        ----------
        detect_errors_only : bool
            If set to ``True``, returns detected error cells (default: ``False``).
        compute_repair_candidate_prob : bool
            If set to ``True``, returns probabiity mass function of candidate
            repairs (default: ``False``).
        compute_repair_prob : bool
            If set to ``True``, returns probabiity of predicted repairs (default: ``False``).
        repair_data : bool
            If set to ``True``, returns repaired input data (default: ``False``).
        maximal_likelihood_repair : bool
            If set to ``True``, returns maximal likelihood repairs (default: ``False``).

        Examples
        --------
        >>> df = delphi.repair.setInput(spark.table("adult")).setRowId("tid").run()
        >>> df.show()
        +---+---------+-------------+-----------+
        |tid|attribute|current_value|   repaired|
        +---+---------+-------------+-----------+
        | 12|      Age|         null|      18-21|
        | 12|      Sex|         null|     Female|
        |  7|      Sex|         null|     Female|
        |  3|      Sex|         null|     Female|
        |  5|      Age|         null|      18-21|
        |  5|   Income|         null|MoreThan50K|
        | 16|   Income|         null|MoreThan50K|
        +---+---------+-------------+-----------+

        >>> df = delphi.repair.setInput(spark.table("adult")).setRowId("tid")
        ...    .run(compute_repair_prob=True)
        >>> df.show()
        +---+---------+-------------+-----------+-------------------+
        |tid|attribute|current_value|   repaired|               prob|
        +---+---------+-------------+-----------+-------------------+
        |  5|      Age|         null|      31-50| 0.5142776979219954|
        |  5|   Income|         null|LessThan50K| 0.9397100503416668|
        |  3|      Sex|         null|     Female| 0.6664498420338913|
        |  7|      Sex|         null|       Male| 0.7436767447201434|
        | 12|      Age|         null|        >50|0.40970902247819213|
        | 12|      Sex|         null|       Male| 0.7436767447201434|
        | 16|   Income|         null|LessThan50K| 0.9446392404617634|
        +---+---------+-------------+-----------+-------------------+
        """
        if self.input is None or self.row_id is None:
            raise ValueError("`setInput` and `setRowId` should be called before repairing")

        if maximal_likelihood_repair and self.repair_delta is None:
            raise ValueError("`setRepairDelta` should be called when enabling "
                             "maximal likelihood repairing")
        if maximal_likelihood_repair and self.cf is None:
            raise ValueError("`setUpdateCostFunction` should be called when enabling "
                             "maximal likelihood repairing")
        if maximal_likelihood_repair and len(self.cf.targets) > 0:  # type: ignore
            raise ValueError("`UpdateCostFunction.targets` cannot be used when enabling "
                             "maximal likelihood repairing")

        exclusive_param_list = [
            ("detect_errors_only", detect_errors_only),
            ("compute_repair_candidate_prob", compute_repair_candidate_prob),
            ("compute_repair_prob", compute_repair_prob),
            ("compute_repair_score", compute_repair_score),
            ("repair_data", repair_data)
        ]
        selected_param = list(map(lambda x: x[0], filter(lambda x: x[1], exclusive_param_list)))
        if len(selected_param) > 1:
            raise ValueError("{} cannot be set to true simultaneously".format(
                to_list_str(selected_param, sep='/', quote=True)))

        # TODO: Support these mixed modes in future
        if self._repair_by_nearest_values_enabled and \
            (maximal_likelihood_repair or compute_repair_candidate_prob or
                compute_repair_prob or compute_repair_score):
            raise ValueError("Cannot repair data by nearest values when enabling "
                             "`maximal_likelihood_repair`, `compute_repair_candidate_prob`, "
                             "`compute_repair_prob`, or `compute_repair_score`")

        # To compute scores or the probabiity of predicted repairs, we need to compute
        # the probabiity mass function of candidate repairs.
        if compute_repair_prob or compute_repair_score:
            compute_repair_candidate_prob = True

        # `maximal_likelihood_repair` needs to be set to true for `compute_repair_score`
        if compute_repair_score:
            maximal_likelihood_repair = True

        try:
            # Validates input data
            input_table, continous_columns = self._check_input_table()

            if maximal_likelihood_repair and len(continous_columns) != 0:
                raise ValueError("Cannot enable the maximal likelihood repair mode "
                                 "when continous attributes found")

            if self.targets and len(set(self.targets) & set(self._spark.table(input_table).columns)) == 0:
                raise ValueError(f"Target attributes not found in {input_table}: {to_list_str(self.targets)}")

            df, elapsed_time = self._run(
                input_table, continous_columns, detect_errors_only, compute_repair_candidate_prob,
                compute_repair_prob, compute_repair_score, repair_data,
                maximal_likelihood_repair)

            _logger.info(f"!!!Total Processing time is {elapsed_time}(s)!!!")

            return df
        finally:
            self._release_resources()
