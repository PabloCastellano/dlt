from copy import deepcopy
from typing import (
    Callable,
    Sequence,
    Iterable,
    Optional,
    Any,
    Dict,
    Union,
    TYPE_CHECKING,
)

from dlt.common.jsonpath import TAnyJsonPath
from dlt.common.exceptions import TerminalException
from dlt.common.schema.typing import TSimpleRegex
from dlt.common.pipeline import pipeline_state as current_pipeline_state, TRefreshMode
from dlt.pipeline.exceptions import (
    PipelineNeverRan,
    PipelineStepFailed,
    PipelineHasPendingDataException,
)
from dlt.pipeline.state_sync import force_state_extract
from dlt.pipeline.typing import TPipelineStep
from dlt.pipeline.drop import drop_resources
from dlt.extract import DltSource

if TYPE_CHECKING:
    from dlt.pipeline import Pipeline


def retry_load(
    retry_on_pipeline_steps: Sequence[TPipelineStep] = ("load",)
) -> Callable[[BaseException], bool]:
    """A retry strategy for Tenacity that, with default setting, will repeat `load` step for all exceptions that are not terminal

    Use this condition with tenacity `retry_if_exception`. Terminal exceptions are exceptions that will not go away when operations is repeated.
    Examples: missing configuration values, Authentication Errors, terminally failed jobs exceptions etc.

    >>> data = source(...)
    >>> for attempt in Retrying(stop=stop_after_attempt(3), retry=retry_if_exception(retry_load(())), reraise=True):
    >>>     with attempt:
    >>>         p.run(data)

    Args:
        retry_on_pipeline_steps (Tuple[TPipelineStep, ...], optional): which pipeline steps are allowed to be repeated. Default: "load"

    """

    def _retry_load(ex: BaseException) -> bool:
        # do not retry in normalize or extract stages
        if isinstance(ex, PipelineStepFailed) and ex.step not in retry_on_pipeline_steps:
            return False
        # do not retry on terminal exceptions
        if isinstance(ex, TerminalException) or (
            ex.__context__ is not None and isinstance(ex.__context__, TerminalException)
        ):
            return False
        return True

    return _retry_load


class DropCommand:
    def __init__(
        self,
        pipeline: "Pipeline",
        resources: Union[Iterable[Union[str, TSimpleRegex]], Union[str, TSimpleRegex]] = (),
        schema_name: Optional[str] = None,
        state_paths: TAnyJsonPath = (),
        drop_all: bool = False,
        state_only: bool = False,
    ) -> None:
        """
        Args:
            pipeline: Pipeline to drop tables and state from
            resources: List of resources to drop. If empty, no resources are dropped unless `drop_all` is True
            schema_name: Name of the schema to drop tables from. If not specified, the default schema is used
            state_paths: JSON path(s) relative to the source state to drop
            drop_all: Drop all resources and tables in the schema (supersedes `resources` list)
            state_only: Drop only state, not tables
        """
        self.pipeline = pipeline

        if not pipeline.default_schema_name:
            raise PipelineNeverRan(pipeline.pipeline_name, pipeline.pipelines_dir)
        self.schema = pipeline.schemas[schema_name or pipeline.default_schema_name].clone()

        drop_result = drop_resources(
            # self._drop_schema, self._new_state, self.info = drop_resources(
            self.schema,
            pipeline.state,
            resources,
            state_paths,
            drop_all,
            state_only,
        )

        self._new_state = drop_result.state
        self.info = drop_result.info
        self._new_schema = drop_result.schema
        self._dropped_tables = drop_result.dropped_tables
        self.drop_tables = not state_only and bool(self._dropped_tables)

        self.drop_state = bool(drop_all or resources or state_paths)

    @property
    def is_empty(self) -> bool:
        return (
            len(self.info["tables"]) == 0
            and len(self.info["state_paths"]) == 0
            and len(self.info["resource_states"]) == 0
        )

    def __call__(self) -> None:
        if (
            self.pipeline.has_pending_data
        ):  # Raise when there are pending extracted/load files to prevent conflicts
            raise PipelineHasPendingDataException(
                self.pipeline.pipeline_name, self.pipeline.pipelines_dir
            )
        self.pipeline.sync_destination()

        if not self.drop_state and not self.drop_tables:
            return  # Nothing to drop

        self._new_schema._bump_version()
        new_state = deepcopy(self._new_state)
        force_state_extract(new_state)

        self.pipeline._save_and_extract_state_and_schema(
            new_state,
            schema=self._new_schema,
            load_package_state_update={"dropped_tables": self._dropped_tables},
        )

        self.pipeline.normalize()
        try:
            self.pipeline.load(raise_on_failed_jobs=True)
        except Exception:
            # Clear extracted state on failure so command can run again
            self.pipeline.drop_pending_packages()
            with self.pipeline.managed_state() as state:
                force_state_extract(state)
            # Restore original schema file so all tables are known on next run
            self.pipeline.schemas.save_schema(self.schema)
            raise


def drop(
    pipeline: "Pipeline",
    resources: Union[Iterable[str], str] = (),
    schema_name: str = None,
    state_paths: TAnyJsonPath = (),
    drop_all: bool = False,
    state_only: bool = False,
) -> None:
    return DropCommand(pipeline, resources, schema_name, state_paths, drop_all, state_only)()


def refresh_source(
    pipeline: "Pipeline", source: DltSource, refresh: TRefreshMode
) -> Dict[str, Any]:
    """Run the pipeline's refresh mode on the given source, updating the source's schema and state.

    Returns:
        The new load package state containing tables that need to be dropped/truncated.
    """
    if pipeline.first_run:
        return {}
    pipeline_state, _ = current_pipeline_state(pipeline._container)
    _resources_to_drop = list(source.resources.extracted) if refresh != "drop_sources" else []
    drop_result = drop_resources(
        source.schema,
        pipeline_state,
        resources=_resources_to_drop,
        drop_all=refresh == "drop_sources",
        state_paths="*" if refresh == "drop_sources" else [],
        sources=source.name,
    )
    load_package_state = {}
    if drop_result.dropped_tables:
        key = "dropped_tables" if refresh != "drop_data" else "truncated_tables"
        load_package_state[key] = drop_result.dropped_tables
    if refresh != "drop_data":  # drop_data is only data wipe, keep original schema
        source.schema = drop_result.schema
    if "sources" in drop_result.state:
        pipeline_state["sources"] = drop_result.state["sources"]
    return load_package_state
