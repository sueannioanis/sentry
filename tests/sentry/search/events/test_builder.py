import datetime
import re

import pytest
from django.utils import timezone
from snuba_sdk.aliased_expression import AliasedExpression
from snuba_sdk.column import Column
from snuba_sdk.conditions import Condition, Op, Or
from snuba_sdk.function import Function
from snuba_sdk.orderby import Direction, LimitBy, OrderBy

from sentry.exceptions import IncompatibleMetricsQuery, InvalidSearchQuery
from sentry.search.events import constants
from sentry.search.events.builder import (
    MetricsQueryBuilder,
    QueryBuilder,
    TimeseriesMetricQueryBuilder,
)
from sentry.sentry_metrics import indexer
from sentry.sentry_metrics.indexer.postgres import PGStringIndexer
from sentry.testutils.cases import SessionMetricsTestCase, TestCase
from sentry.utils.snuba import Dataset, QueryOutsideRetentionError


class QueryBuilderTest(TestCase):
    def setUp(self):
        self.start = datetime.datetime(2015, 5, 18, 10, 15, 1, tzinfo=timezone.utc)
        self.end = datetime.datetime(2015, 5, 19, 10, 15, 1, tzinfo=timezone.utc)
        self.projects = [1, 2, 3]
        self.params = {
            "project_id": self.projects,
            "start": self.start,
            "end": self.end,
        }
        # These conditions should always be on a query when self.params is passed
        self.default_conditions = [
            Condition(Column("timestamp"), Op.GTE, self.start),
            Condition(Column("timestamp"), Op.LT, self.end),
            Condition(Column("project_id"), Op.IN, self.projects),
        ]

    def test_simple_query(self):
        query = QueryBuilder(
            Dataset.Discover,
            self.params,
            "user.email:foo@example.com release:1.2.1",
            ["user.email", "release"],
        )

        self.assertCountEqual(
            query.where,
            [
                Condition(Column("email"), Op.EQ, "foo@example.com"),
                Condition(Column("release"), Op.IN, ["1.2.1"]),
                *self.default_conditions,
            ],
        )
        self.assertCountEqual(
            query.columns,
            [
                AliasedExpression(Column("email"), "user.email"),
                Column("release"),
            ],
        )
        query.get_snql_query().validate()

    def test_simple_orderby(self):
        query = QueryBuilder(
            Dataset.Discover,
            self.params,
            selected_columns=["user.email", "release"],
            orderby=["user.email"],
        )

        self.assertCountEqual(query.where, self.default_conditions)
        self.assertCountEqual(
            query.orderby,
            [OrderBy(Column("email"), Direction.ASC)],
        )
        query.get_snql_query().validate()

        query = QueryBuilder(
            Dataset.Discover,
            self.params,
            selected_columns=["user.email", "release"],
            orderby=["-user.email"],
        )

        self.assertCountEqual(query.where, self.default_conditions)
        self.assertCountEqual(
            query.orderby,
            [OrderBy(Column("email"), Direction.DESC)],
        )
        query.get_snql_query().validate()

    def test_orderby_duplicate_columns(self):
        query = QueryBuilder(
            Dataset.Discover,
            self.params,
            selected_columns=["user.email", "user.email"],
            orderby=["user.email"],
        )
        self.assertCountEqual(
            query.orderby,
            [OrderBy(Column("email"), Direction.ASC)],
        )

    def test_simple_limitby(self):
        query = QueryBuilder(
            dataset=Dataset.Discover,
            params=self.params,
            query="",
            selected_columns=["message"],
            orderby="message",
            limitby=("message", 1),
            limit=4,
        )

        assert query.limitby == LimitBy([Column("message")], 1)

    def test_environment_filter(self):
        query = QueryBuilder(
            Dataset.Discover,
            self.params,
            "environment:prod",
            ["environment"],
        )

        self.assertCountEqual(
            query.where,
            [
                Condition(Column("environment"), Op.EQ, "prod"),
                *self.default_conditions,
            ],
        )
        query.get_snql_query().validate()

        query = QueryBuilder(
            Dataset.Discover,
            self.params,
            "environment:[dev, prod]",
            ["environment"],
        )

        self.assertCountEqual(
            query.where,
            [
                Condition(Column("environment"), Op.IN, ["dev", "prod"]),
                *self.default_conditions,
            ],
        )
        query.get_snql_query().validate()

    def test_environment_param(self):
        self.params["environment"] = ["", "prod"]
        query = QueryBuilder(Dataset.Discover, self.params, selected_columns=["environment"])

        self.assertCountEqual(
            query.where,
            [
                *self.default_conditions,
                Or(
                    [
                        Condition(Column("environment"), Op.IS_NULL),
                        Condition(Column("environment"), Op.EQ, "prod"),
                    ]
                ),
            ],
        )
        query.get_snql_query().validate()

        self.params["environment"] = ["dev", "prod"]
        query = QueryBuilder(Dataset.Discover, self.params, selected_columns=["environment"])

        self.assertCountEqual(
            query.where,
            [
                *self.default_conditions,
                Condition(Column("environment"), Op.IN, ["dev", "prod"]),
            ],
        )
        query.get_snql_query().validate()

    def test_project_in_condition_filters(self):
        # TODO(snql-boolean): Update this to match the corresponding test in test_filter
        project1 = self.create_project()
        project2 = self.create_project()
        self.params["project_id"] = [project1.id, project2.id]
        query = QueryBuilder(
            Dataset.Discover,
            self.params,
            f"project:{project1.slug}",
            selected_columns=["environment"],
        )

        self.assertCountEqual(
            query.where,
            [
                # generated by the search query on project
                Condition(Column("project_id"), Op.EQ, project1.id),
                Condition(Column("timestamp"), Op.GTE, self.start),
                Condition(Column("timestamp"), Op.LT, self.end),
                # default project filter from the params
                Condition(Column("project_id"), Op.IN, [project1.id, project2.id]),
            ],
        )

    def test_project_in_condition_filters_not_in_project_filter(self):
        # TODO(snql-boolean): Update this to match the corresponding test in test_filter
        project1 = self.create_project()
        project2 = self.create_project()
        # params is assumed to be validated at this point, so this query should be invalid
        self.params["project_id"] = [project2.id]
        with self.assertRaisesRegex(
            InvalidSearchQuery,
            re.escape(
                f"Invalid query. Project(s) {str(project1.slug)} do not exist or are not actively selected."
            ),
        ):
            QueryBuilder(
                Dataset.Discover,
                self.params,
                f"project:{project1.slug}",
                selected_columns=["environment"],
            )

    def test_project_alias_column(self):
        # TODO(snql-boolean): Update this to match the corresponding test in test_filter
        project1 = self.create_project()
        project2 = self.create_project()
        self.params["project_id"] = [project1.id, project2.id]
        query = QueryBuilder(Dataset.Discover, self.params, selected_columns=["project"])

        self.assertCountEqual(
            query.where,
            [
                Condition(Column("project_id"), Op.IN, [project1.id, project2.id]),
                Condition(Column("timestamp"), Op.GTE, self.start),
                Condition(Column("timestamp"), Op.LT, self.end),
            ],
        )
        self.assertCountEqual(
            query.columns,
            [
                Function(
                    "transform",
                    [
                        Column("project_id"),
                        [project1.id, project2.id],
                        [project1.slug, project2.slug],
                        "",
                    ],
                    "project",
                )
            ],
        )

    def test_project_alias_column_with_project_condition(self):
        project1 = self.create_project()
        project2 = self.create_project()
        self.params["project_id"] = [project1.id, project2.id]
        query = QueryBuilder(
            Dataset.Discover, self.params, f"project:{project1.slug}", selected_columns=["project"]
        )

        self.assertCountEqual(
            query.where,
            [
                # generated by the search query on project
                Condition(Column("project_id"), Op.EQ, project1.id),
                Condition(Column("timestamp"), Op.GTE, self.start),
                Condition(Column("timestamp"), Op.LT, self.end),
                # default project filter from the params
                Condition(Column("project_id"), Op.IN, [project1.id, project2.id]),
            ],
        )
        # Because of the condition on project there should only be 1 project in the transform
        self.assertCountEqual(
            query.columns,
            [
                Function(
                    "transform",
                    [
                        Column("project_id"),
                        [project1.id],
                        [project1.slug],
                        "",
                    ],
                    "project",
                )
            ],
        )

    def test_count_if(self):
        query = QueryBuilder(
            Dataset.Discover,
            self.params,
            "",
            selected_columns=[
                "count_if(event.type,equals,transaction)",
                'count_if(event.type,notEquals,"transaction")',
            ],
        )
        self.assertCountEqual(query.where, self.default_conditions)
        self.assertCountEqual(
            query.aggregates,
            [
                Function(
                    "countIf",
                    [
                        Function("equals", [Column("type"), "transaction"]),
                    ],
                    "count_if_event_type_equals_transaction",
                ),
                Function(
                    "countIf",
                    [
                        Function("notEquals", [Column("type"), "transaction"]),
                    ],
                    "count_if_event_type_notEquals__transaction",
                ),
            ],
        )

    def test_count_if_with_tags(self):
        query = QueryBuilder(
            Dataset.Discover,
            self.params,
            "",
            selected_columns=[
                "count_if(foo,equals,bar)",
                'count_if(foo,notEquals,"baz")',
            ],
        )
        self.assertCountEqual(query.where, self.default_conditions)
        self.assertCountEqual(
            query.aggregates,
            [
                Function(
                    "countIf",
                    [
                        Function("equals", [Column("tags[foo]"), "bar"]),
                    ],
                    "count_if_foo_equals_bar",
                ),
                Function(
                    "countIf",
                    [
                        Function("notEquals", [Column("tags[foo]"), "baz"]),
                    ],
                    "count_if_foo_notEquals__baz",
                ),
            ],
        )

    def test_array_join(self):
        query = QueryBuilder(
            Dataset.Discover,
            self.params,
            "",
            selected_columns=["array_join(measurements_key)", "count()"],
            functions_acl=["array_join"],
        )
        array_join_column = Function(
            "arrayJoin",
            [Column("measurements.key")],
            "array_join_measurements_key",
        )
        self.assertCountEqual(query.columns, [array_join_column, Function("count", [], "count")])
        # make sure the the array join columns are present in gropuby
        self.assertCountEqual(query.groupby, [array_join_column])

    def test_retention(self):
        with self.options({"system.event-retention-days": 10}):
            with self.assertRaises(QueryOutsideRetentionError):
                QueryBuilder(
                    Dataset.Discover,
                    self.params,
                    "",
                    selected_columns=[],
                )

    def test_array_combinator(self):
        query = QueryBuilder(
            Dataset.Discover,
            self.params,
            "",
            selected_columns=["sumArray(measurements_value)"],
            functions_acl=["sumArray"],
        )
        self.assertCountEqual(
            query.columns,
            [
                Function(
                    "sum",
                    [Function("arrayJoin", [Column("measurements.value")])],
                    "sumArray_measurements_value",
                )
            ],
        )

    def test_array_combinator_is_private(self):
        with self.assertRaisesRegex(InvalidSearchQuery, "sum: no access to private function"):
            QueryBuilder(
                Dataset.Discover,
                self.params,
                "",
                selected_columns=["sumArray(measurements_value)"],
            )

    def test_array_combinator_with_non_array_arg(self):
        with self.assertRaisesRegex(InvalidSearchQuery, "stuff is not a valid array column"):
            QueryBuilder(
                Dataset.Discover,
                self.params,
                "",
                selected_columns=["sumArray(stuff)"],
                functions_acl=["sumArray"],
            )

    def test_spans_columns(self):
        query = QueryBuilder(
            Dataset.Discover,
            self.params,
            "",
            selected_columns=[
                "array_join(spans_op)",
                "array_join(spans_group)",
                "sumArray(spans_exclusive_time)",
            ],
            functions_acl=["array_join", "sumArray"],
        )
        self.assertCountEqual(
            query.columns,
            [
                Function("arrayJoin", [Column("spans.op")], "array_join_spans_op"),
                Function("arrayJoin", [Column("spans.group")], "array_join_spans_group"),
                Function(
                    "sum",
                    [Function("arrayJoin", [Column("spans.exclusive_time")])],
                    "sumArray_spans_exclusive_time",
                ),
            ],
        )

    def test_array_join_clause(self):
        query = QueryBuilder(
            Dataset.Discover,
            self.params,
            "",
            selected_columns=[
                "spans_op",
                "count()",
            ],
            array_join="spans_op",
        )
        self.assertCountEqual(
            query.columns,
            [
                AliasedExpression(Column("spans.op"), "spans_op"),
                Function("count", [], "count"),
            ],
        )

        assert query.array_join == [Column("spans.op")]
        query.get_snql_query().validate()

    def test_sample_rate(self):
        query = QueryBuilder(
            Dataset.Discover,
            self.params,
            "",
            selected_columns=[
                "count()",
            ],
            sample_rate=0.1,
        )
        assert query.sample_rate == 0.1
        snql_query = query.get_snql_query()
        snql_query.validate()
        assert snql_query.match.sample == 0.1

    def test_turbo(self):
        query = QueryBuilder(
            Dataset.Discover,
            self.params,
            "",
            selected_columns=[
                "count()",
            ],
            turbo=True,
        )
        assert query.turbo.value
        snql_query = query.get_snql_query()
        snql_query.validate()
        assert snql_query.turbo.value

    def test_auto_aggregation(self):
        query = QueryBuilder(
            Dataset.Discover,
            self.params,
            "count_unique(user):>10",
            selected_columns=[
                "count()",
            ],
            auto_aggregations=True,
            use_aggregate_conditions=True,
        )
        snql_query = query.get_snql_query()
        snql_query.validate()
        self.assertCountEqual(
            snql_query.having,
            [
                Condition(Function("uniq", [Column("user")], "count_unique_user"), Op.GT, 10),
            ],
        )
        self.assertCountEqual(
            snql_query.select,
            [
                Function("uniq", [Column("user")], "count_unique_user"),
                Function("count", [], "count"),
            ],
        )

    def test_auto_aggregation_with_boolean(self):
        query = QueryBuilder(
            Dataset.Discover,
            self.params,
            # Nonsense query but doesn't matter
            "count_unique(user):>10 OR count_unique(user):<10",
            selected_columns=[
                "count()",
            ],
            auto_aggregations=True,
            use_aggregate_conditions=True,
        )
        snql_query = query.get_snql_query()
        snql_query.validate()
        self.assertCountEqual(
            snql_query.having,
            [
                Or(
                    [
                        Condition(
                            Function("uniq", [Column("user")], "count_unique_user"), Op.GT, 10
                        ),
                        Condition(
                            Function("uniq", [Column("user")], "count_unique_user"), Op.LT, 10
                        ),
                    ]
                )
            ],
        )
        self.assertCountEqual(
            snql_query.select,
            [
                Function("uniq", [Column("user")], "count_unique_user"),
                Function("count", [], "count"),
            ],
        )

    def test_disable_auto_aggregation(self):
        query = QueryBuilder(
            Dataset.Discover,
            self.params,
            "count_unique(user):>10",
            selected_columns=[
                "count()",
            ],
            auto_aggregations=False,
            use_aggregate_conditions=True,
        )
        # With count_unique only in a condition and no auto_aggregations this should raise a invalid search query
        with self.assertRaises(InvalidSearchQuery):
            query.get_snql_query()


def _metric_percentile_definition(quantile, field="transaction.duration") -> Function:
    return Function(
        "arrayElement",
        [
            Function(
                f"quantilesMergeIf(0.{quantile.rstrip('0')})",
                [
                    Column("percentiles"),
                    Function(
                        "equals",
                        [Column("metric_id"), indexer.resolve(constants.METRICS_MAP[field])],
                    ),
                ],
            ),
            1,
        ],
        f"p{quantile}_{field.replace('.', '_')}",
    )


class MetricBuilderBaseTest(TestCase, SessionMetricsTestCase):
    TYPE_MAP = {
        "metrics_distributions": "d",
        "metrics_sets": "s",
        "metrics_counters": "c",
    }

    def setUp(self):
        self.start = datetime.datetime(2015, 1, 1, 10, 15, 0, tzinfo=timezone.utc)
        self.end = datetime.datetime(2015, 1, 19, 10, 15, 0, tzinfo=timezone.utc)
        self.projects = [self.project.id]
        self.organization_id = 1
        self.params = {
            "organization_id": self.organization_id,
            "project_id": self.projects,
            "start": self.start,
            "end": self.end,
        }
        # These conditions should always be on a query when self.params is passed
        self.default_conditions = [
            Condition(Column("timestamp"), Op.GTE, self.start),
            Condition(Column("timestamp"), Op.LT, self.end),
            Condition(Column("project_id"), Op.IN, self.projects),
            Condition(Column("org_id"), Op.EQ, self.organization_id),
        ]
        PGStringIndexer().bulk_record(
            strings=[
                "transaction",
                "transaction.status",
                "ok",
                "cancelled",
                "internal_error",
                "unknown",
                "foo_transaction",
                "bar_transaction",
                "baz_transaction",
            ]
            + list(constants.METRICS_MAP.values())
        )

    def store_metric(
        self,
        value,
        metric=constants.METRICS_MAP["transaction.duration"],
        entity="metrics_distributions",
        tags=None,
        timestamp=None,
    ):
        if tags is None:
            tags = {}
        else:
            tags = {indexer.resolve(key): indexer.resolve(value) for key, value in tags.items()}
        if timestamp is None:
            timestamp = (self.start + datetime.timedelta(minutes=1)).timestamp()
        else:
            timestamp = timestamp.timestamp()
        if not isinstance(value, list):
            value = [value]
        self._send_buckets(
            [
                {
                    "org_id": self.organization_id,
                    "project_id": self.project.id,
                    "metric_id": indexer.resolve(metric),
                    "timestamp": timestamp,
                    "tags": tags,
                    "type": self.TYPE_MAP[entity],
                    "value": value,
                    "retention_days": 90,
                }
            ],
            entity=entity,
        )

    def setup_orderby_data(self):
        self.store_metric(100, tags={"transaction": "foo_transaction"})
        self.store_metric(
            1,
            metric=constants.METRICS_MAP["user"],
            entity="metrics_sets",
            tags={"transaction": "foo_transaction"},
        )
        self.store_metric(50, tags={"transaction": "bar_transaction"})
        self.store_metric(
            1,
            metric=constants.METRICS_MAP["user"],
            entity="metrics_sets",
            tags={"transaction": "bar_transaction"},
        )
        self.store_metric(
            2,
            metric=constants.METRICS_MAP["user"],
            entity="metrics_sets",
            tags={"transaction": "bar_transaction"},
        )


class MetricQueryBuilderTest(MetricBuilderBaseTest):
    def test_default_conditions(self):
        query = MetricsQueryBuilder(self.params, "", selected_columns=[])
        self.assertCountEqual(query.where, self.default_conditions)

    def test_simple_aggregates(self):
        query = MetricsQueryBuilder(
            self.params,
            "",
            selected_columns=[
                "p50(transaction.duration)",
                "p75(measurements.lcp)",
                "p90(measurements.fcp)",
                "p95(measurements.cls)",
                "p99(measurements.fid)",
            ],
        )
        self.assertCountEqual(query.where, self.default_conditions)
        self.assertCountEqual(
            query.distributions,
            [
                _metric_percentile_definition("50"),
                _metric_percentile_definition("75", "measurements.lcp"),
                _metric_percentile_definition("90", "measurements.fcp"),
                _metric_percentile_definition("95", "measurements.cls"),
                _metric_percentile_definition("99", "measurements.fid"),
            ],
        )

    def test_grouping(self):
        query = MetricsQueryBuilder(
            self.params,
            "",
            selected_columns=["transaction", "project", "p95(transaction.duration)"],
        )
        self.assertCountEqual(query.where, self.default_conditions)
        transaction_index = indexer.resolve("transaction")
        transaction = AliasedExpression(
            Column(f"tags[{transaction_index}]"),
            "transaction",
        )
        project = Function(
            "transform",
            [
                Column("project_id"),
                [self.project.id],
                [self.project.slug],
                "",
            ],
            "project",
        )
        self.assertCountEqual(
            query.groupby,
            [
                transaction,
                project,
            ],
        )
        self.assertCountEqual(query.distributions, [_metric_percentile_definition("95")])

    def test_transaction_filter(self):
        query = MetricsQueryBuilder(
            self.params,
            "transaction:foo_transaction",
            selected_columns=["transaction", "project", "p95(transaction.duration)"],
        )
        transaction_index = indexer.resolve("transaction")
        transaction_name = indexer.resolve("foo_transaction")
        transaction = Column(f"tags[{transaction_index}]")
        self.assertCountEqual(
            query.where, [*self.default_conditions, Condition(transaction, Op.EQ, transaction_name)]
        )

    def test_transaction_in_filter(self):
        query = MetricsQueryBuilder(
            self.params,
            "transaction:[foo_transaction, bar_transaction]",
            selected_columns=["transaction", "project", "p95(transaction.duration)"],
        )
        transaction_index = indexer.resolve("transaction")
        transaction_name1 = indexer.resolve("foo_transaction")
        transaction_name2 = indexer.resolve("bar_transaction")
        transaction = Column(f"tags[{transaction_index}]")
        self.assertCountEqual(
            query.where,
            [
                *self.default_conditions,
                Condition(transaction, Op.IN, [transaction_name1, transaction_name2]),
            ],
        )

    def test_missing_transaction_index(self):
        with self.assertRaisesRegex(
            InvalidSearchQuery,
            re.escape("Tag value was not found"),
        ):
            MetricsQueryBuilder(
                self.params,
                "transaction:something_else",
                selected_columns=["transaction", "project", "p95(transaction.duration)"],
            )

    def test_missing_transaction_index_in_filter(self):
        with self.assertRaisesRegex(
            InvalidSearchQuery,
            re.escape("Tag value was not found"),
        ):
            MetricsQueryBuilder(
                self.params,
                "transaction:[something_else, something_else2]",
                selected_columns=["transaction", "project", "p95(transaction.duration)"],
            )

    def test_project_filter(self):
        query = MetricsQueryBuilder(
            self.params,
            f"project:{self.project.slug}",
            selected_columns=["transaction", "project", "p95(transaction.duration)"],
        )
        self.assertCountEqual(
            query.where,
            [*self.default_conditions, Condition(Column("project_id"), Op.EQ, self.project.id)],
        )

    def test_limit_validation(self):
        # 51 is ok
        MetricsQueryBuilder(self.params, limit=51)
        # None is ok, defaults to 50
        query = MetricsQueryBuilder(self.params)
        assert query.limit.limit == 50
        # anything higher should throw an error
        with self.assertRaises(IncompatibleMetricsQuery):
            MetricsQueryBuilder(self.params, limit=10_000)

    def test_granularity(self):
        # Need to pick granularity based on the period
        def get_granularity(start, end):
            params = {
                "organization_id": self.organization_id,
                "project_id": self.projects,
                "start": start,
                "end": end,
            }
            query = MetricsQueryBuilder(params)
            return query.granularity.granularity

        # If we're doing atleast day and its midnight we should use the daily bucket
        start = datetime.datetime(2015, 5, 18, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime.datetime(2015, 5, 18, 0, 0, 0, tzinfo=timezone.utc)
        assert get_granularity(start, end) == 86400, "A day at midnight"

        # If we're on the start of the hour we should use the hour granularity
        start = datetime.datetime(2015, 5, 18, 23, 0, 0, tzinfo=timezone.utc)
        end = datetime.datetime(2015, 5, 20, 1, 0, 0, tzinfo=timezone.utc)
        assert get_granularity(start, end) == 3600, "On the hour"

        # Even though this is >24h of data, because its a random hour in the middle of the day to the next we use minute
        # granularity
        start = datetime.datetime(2015, 5, 18, 10, 15, 1, tzinfo=timezone.utc)
        end = datetime.datetime(2015, 5, 19, 15, 15, 1, tzinfo=timezone.utc)
        assert get_granularity(start, end) == 60, "A few hours, but random minute"

        # Less than a minute, no reason to work hard for such a small window, just use a minute
        start = datetime.datetime(2015, 5, 18, 10, 15, 1, tzinfo=timezone.utc)
        end = datetime.datetime(2015, 5, 19, 10, 15, 34, tzinfo=timezone.utc)
        assert get_granularity(start, end) == 60, "less than a minute"

    def test_run_query(self):
        self.store_metric(100, tags={"transaction": "foo_transaction"})
        query = MetricsQueryBuilder(
            self.params,
            f"project:{self.project.slug}",
            selected_columns=[
                "transaction",
                "p95(transaction.duration)",
            ],
        )
        result = query.run_query("test_query")
        assert len(result["data"]) == 1
        assert result["data"][0] == {
            "transaction": indexer.resolve("foo_transaction"),
            "p95_transaction_duration": 100,
        }
        self.assertCountEqual(
            result["meta"],
            [
                {"name": "transaction", "type": "UInt64"},
                {"name": "p95_transaction_duration", "type": "Float64"},
            ],
        )

    def test_run_query_multiple_tables(self):
        self.store_metric(100, tags={"transaction": "foo_transaction"})
        self.store_metric(
            1,
            metric=constants.METRICS_MAP["user"],
            entity="metrics_sets",
            tags={"transaction": "foo_transaction"},
        )
        query = MetricsQueryBuilder(
            self.params,
            f"project:{self.project.slug}",
            selected_columns=[
                "transaction",
                "p95(transaction.duration)",
                "count_unique(user)",
            ],
        )
        result = query.run_query("test_query")
        assert len(result["data"]) == 1
        assert result["data"][0] == {
            "transaction": indexer.resolve("foo_transaction"),
            "p95_transaction_duration": 100,
            "count_unique_user": 1,
        }
        self.assertCountEqual(
            result["meta"],
            [
                {"name": "transaction", "type": "UInt64"},
                {"name": "p95_transaction_duration", "type": "Float64"},
                {"name": "count_unique_user", "type": "UInt64"},
            ],
        )

    def test_run_query_with_multiple_groupby_orderby_distribution(self):
        self.setup_orderby_data()
        query = MetricsQueryBuilder(
            self.params,
            f"project:{self.project.slug}",
            selected_columns=[
                "transaction",
                "project",
                "p95(transaction.duration)",
                "count_unique(user)",
            ],
            orderby="-p95(transaction.duration)",
        )
        result = query.run_query("test_query")
        assert len(result["data"]) == 2
        assert result["data"][0] == {
            "transaction": indexer.resolve("foo_transaction"),
            "project": self.project.slug,
            "p95_transaction_duration": 100,
            "count_unique_user": 1,
        }
        assert result["data"][1] == {
            "transaction": indexer.resolve("bar_transaction"),
            "project": self.project.slug,
            "p95_transaction_duration": 50,
            "count_unique_user": 2,
        }
        self.assertCountEqual(
            result["meta"],
            [
                {"name": "transaction", "type": "UInt64"},
                {"name": "project", "type": "String"},
                {"name": "p95_transaction_duration", "type": "Float64"},
                {"name": "count_unique_user", "type": "UInt64"},
            ],
        )

    def test_run_query_with_multiple_groupby_orderby_set(self):
        self.setup_orderby_data()
        query = MetricsQueryBuilder(
            self.params,
            f"project:{self.project.slug}",
            selected_columns=[
                "transaction",
                "project",
                "p95(transaction.duration)",
                "count_unique(user)",
            ],
            orderby="-count_unique(user)",
        )
        result = query.run_query("test_query")
        assert len(result["data"]) == 2
        assert result["data"][0] == {
            "transaction": indexer.resolve("bar_transaction"),
            "project": self.project.slug,
            "p95_transaction_duration": 50,
            "count_unique_user": 2,
        }
        assert result["data"][1] == {
            "transaction": indexer.resolve("foo_transaction"),
            "project": self.project.slug,
            "p95_transaction_duration": 100,
            "count_unique_user": 1,
        }
        self.assertCountEqual(
            result["meta"],
            [
                {"name": "transaction", "type": "UInt64"},
                {"name": "project", "type": "String"},
                {"name": "p95_transaction_duration", "type": "Float64"},
                {"name": "count_unique_user", "type": "UInt64"},
            ],
        )

    # TODO: multiple groupby with counter

    def test_run_query_with_events_per_aggregates(self):
        for i in range(5):
            self.store_metric(100, timestamp=self.start + datetime.timedelta(minutes=i * 15))
        query = MetricsQueryBuilder(
            self.params,
            "",
            selected_columns=[
                "eps()",
                "epm()",
                "tps()",
                "tpm()",
            ],
        )
        result = query.run_query("test_query")
        data = result["data"][0]
        # Check the aliases are correct
        assert data["epm"] == data["tpm"]
        assert data["eps"] == data["tps"]
        # Check the values are correct
        assert data["tpm"] == 5 / ((self.end - self.start).total_seconds() / 60)
        assert data["tpm"] / 60 == data["tps"]

    def test_failure_rate(self):
        for _ in range(3):
            self.store_metric(100, tags={"transaction.status": "internal_error"})
            self.store_metric(100, tags={"transaction.status": "ok"})
        query = MetricsQueryBuilder(
            self.params,
            "",
            selected_columns=[
                "failure_rate()",
                "failure_count()",
            ],
        )
        result = query.run_query("test_query")
        data = result["data"][0]
        assert data["failure_rate"] == 0.5
        assert data["failure_count"] == 3

    def test_run_query_with_multiple_groupby_orderby_null_values_in_second_entity(self):
        """Since the null value is on count_unique(user) we will still get baz_transaction since we query distributions
        first which will have it, and then just not find a unique count in the second"""
        self.setup_orderby_data()
        self.store_metric(200, tags={"transaction": "baz_transaction"})
        query = MetricsQueryBuilder(
            self.params,
            f"project:{self.project.slug}",
            selected_columns=[
                "transaction",
                "project",
                "p95(transaction.duration)",
                "count_unique(user)",
            ],
            orderby="p95(transaction.duration)",
        )
        result = query.run_query("test_query")
        assert len(result["data"]) == 3
        assert result["data"][0] == {
            "transaction": indexer.resolve("bar_transaction"),
            "project": self.project.slug,
            "p95_transaction_duration": 50,
            "count_unique_user": 2,
        }
        assert result["data"][1] == {
            "transaction": indexer.resolve("foo_transaction"),
            "project": self.project.slug,
            "p95_transaction_duration": 100,
            "count_unique_user": 1,
        }
        assert result["data"][2] == {
            "transaction": indexer.resolve("baz_transaction"),
            "project": self.project.slug,
            "p95_transaction_duration": 200,
        }
        self.assertCountEqual(
            result["meta"],
            [
                {"name": "transaction", "type": "UInt64"},
                {"name": "project", "type": "String"},
                {"name": "p95_transaction_duration", "type": "Float64"},
                {"name": "count_unique_user", "type": "UInt64"},
            ],
        )

    @pytest.mark.skip(
        reason="Currently cannot handle the case where null values are in the first entity"
    )
    def test_run_query_with_multiple_groupby_orderby_null_values_in_first_entity(self):
        """But if the null value is in the first entity, it won't show up in the groupby values, which means the
        transaction will be missing"""
        self.setup_orderby_data()
        self.store_metric(200, tags={"transaction": "baz_transaction"})
        query = MetricsQueryBuilder(
            self.params,
            f"project:{self.project.slug}",
            selected_columns=[
                "transaction",
                "project",
                "p95(transaction.duration)",
                "count_unique(user)",
            ],
            orderby="count_unique(user)",
        )
        result = query.run_query("test_query")
        assert len(result["data"]) == 3
        assert result["data"][0] == {
            "transaction": indexer.resolve("baz_transaction"),
            "project": self.project.slug,
            "p95_transaction_duration": 200,
        }
        assert result["data"][1] == {
            "transaction": indexer.resolve("foo_transaction"),
            "project": self.project.slug,
            "p95_transaction_duration": 100,
            "count_unique_user": 1,
        }
        assert result["data"][2] == {
            "transaction": indexer.resolve("bar_transaction"),
            "project": self.project.slug,
            "p95_transaction_duration": 50,
            "count_unique_user": 2,
        }

    def test_multiple_entity_orderby_fails(self):
        with self.assertRaises(IncompatibleMetricsQuery):
            query = MetricsQueryBuilder(
                self.params,
                f"project:{self.project.slug}",
                selected_columns=[
                    "transaction",
                    "project",
                    "p95(transaction.duration)",
                    "count_unique(user)",
                ],
                orderby=["-count_unique(user)", "p95(transaction.duration)"],
            )
            query.run_query("test_query")


class TimeseresMetricQueryBuilderTest(MetricBuilderBaseTest):
    def test_get_query(self):
        query = TimeseriesMetricQueryBuilder(
            self.params, granularity=900, query="", selected_columns=["p50(transaction.duration)"]
        )
        snql_query = query.get_snql_query()
        assert len(snql_query) == 1
        assert snql_query[0].select == [_metric_percentile_definition("50")]
        assert snql_query[0].match.name == "metrics_distributions"
        assert snql_query[0].granularity.granularity == 900

    def test_default_conditions(self):
        query = TimeseriesMetricQueryBuilder(
            self.params, granularity=900, query="", selected_columns=[]
        )
        self.assertCountEqual(query.where, self.default_conditions)

    def test_transaction_in_filter(self):
        query = TimeseriesMetricQueryBuilder(
            self.params,
            granularity=900,
            query="transaction:[foo_transaction, bar_transaction]",
            selected_columns=["p95(transaction.duration)"],
        )
        transaction_index = indexer.resolve("transaction")
        transaction_name1 = indexer.resolve("foo_transaction")
        transaction_name2 = indexer.resolve("bar_transaction")
        transaction = Column(f"tags[{transaction_index}]")
        self.assertCountEqual(
            query.where,
            [
                *self.default_conditions,
                Condition(transaction, Op.IN, [transaction_name1, transaction_name2]),
            ],
        )

    def test_missing_transaction_index(self):
        with self.assertRaisesRegex(
            InvalidSearchQuery,
            re.escape("Tag value was not found"),
        ):
            TimeseriesMetricQueryBuilder(
                self.params,
                granularity=900,
                query="transaction:something_else",
                selected_columns=["project", "p95(transaction.duration)"],
            )

    def test_missing_transaction_index_in_filter(self):
        with self.assertRaisesRegex(
            InvalidSearchQuery,
            re.escape("Tag value was not found"),
        ):
            TimeseriesMetricQueryBuilder(
                self.params,
                granularity=900,
                query="transaction:[something_else, something_else2]",
                selected_columns=["p95(transaction.duration)"],
            )

    def test_project_filter(self):
        query = TimeseriesMetricQueryBuilder(
            self.params,
            granularity=900,
            query=f"project:{self.project.slug}",
            selected_columns=["p95(transaction.duration)"],
        )
        self.assertCountEqual(
            query.where,
            [*self.default_conditions, Condition(Column("project_id"), Op.EQ, self.project.id)],
        )

    def test_meta(self):
        query = TimeseriesMetricQueryBuilder(
            self.params,
            granularity=900,
            selected_columns=["p50(transaction.duration)", "count_unique(user)"],
        )
        result = query.run_query("test_query")
        self.assertCountEqual(
            result["meta"],
            [
                {"name": "time", "type": "DateTime"},
                {"name": "p50_transaction_duration", "type": "Float64"},
                {"name": "count_unique_user", "type": "UInt64"},
            ],
        )

    def test_with_aggregate_filter(self):
        query = TimeseriesMetricQueryBuilder(
            self.params,
            granularity=900,
            query="p50(transaction.duration):>100",
            selected_columns=["p50(transaction.duration)", "count_unique(user)"],
        )
        # Aggregate conditions should be dropped
        assert query.having == []

    def test_run_query(self):
        for i in range(5):
            self.store_metric(100, timestamp=self.start + datetime.timedelta(minutes=i * 15))
            self.store_metric(
                1,
                metric=constants.METRICS_MAP["user"],
                entity="metrics_sets",
                timestamp=self.start + datetime.timedelta(minutes=i * 15),
            )
        query = TimeseriesMetricQueryBuilder(
            self.params,
            granularity=900,
            query="",
            selected_columns=["p50(transaction.duration)", "count_unique(user)"],
        )
        result = query.run_query("test_query")
        assert result["data"] == [
            {
                "time": "2015-01-01T10:15:00+00:00",
                "p50_transaction_duration": 100.0,
                "count_unique_user": 1,
            },
            {
                "time": "2015-01-01T10:30:00+00:00",
                "p50_transaction_duration": 100.0,
                "count_unique_user": 1,
            },
            {
                "time": "2015-01-01T10:45:00+00:00",
                "p50_transaction_duration": 100.0,
                "count_unique_user": 1,
            },
            {
                "time": "2015-01-01T11:00:00+00:00",
                "p50_transaction_duration": 100.0,
                "count_unique_user": 1,
            },
            {
                "time": "2015-01-01T11:15:00+00:00",
                "p50_transaction_duration": 100.0,
                "count_unique_user": 1,
            },
        ]
        self.assertCountEqual(
            result["meta"],
            [
                {"name": "time", "type": "DateTime"},
                {"name": "p50_transaction_duration", "type": "Float64"},
                {"name": "count_unique_user", "type": "UInt64"},
            ],
        )

    def test_run_query_with_filter(self):
        for i in range(5):
            self.store_metric(
                100,
                tags={"transaction": "foo_transaction"},
                timestamp=self.start + datetime.timedelta(minutes=i * 15),
            )
            self.store_metric(
                200,
                tags={"transaction": "bar_transaction"},
                timestamp=self.start + datetime.timedelta(minutes=i * 15),
            )
        query = TimeseriesMetricQueryBuilder(
            self.params,
            granularity=900,
            query="transaction:foo_transaction",
            selected_columns=["p50(transaction.duration)"],
        )
        result = query.run_query("test_query")
        assert result["data"] == [
            {"time": "2015-01-01T10:15:00+00:00", "p50_transaction_duration": 100.0},
            {"time": "2015-01-01T10:30:00+00:00", "p50_transaction_duration": 100.0},
            {"time": "2015-01-01T10:45:00+00:00", "p50_transaction_duration": 100.0},
            {"time": "2015-01-01T11:00:00+00:00", "p50_transaction_duration": 100.0},
            {"time": "2015-01-01T11:15:00+00:00", "p50_transaction_duration": 100.0},
        ]
        self.assertCountEqual(
            result["meta"],
            [
                {"name": "time", "type": "DateTime"},
                {"name": "p50_transaction_duration", "type": "Float64"},
            ],
        )
