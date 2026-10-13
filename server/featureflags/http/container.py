from dependency_injector import containers, providers
from hiku.engine import Engine
from hiku.executors.asyncio import AsyncIOExecutor

from featureflags.config import config
from featureflags.http.repositories.flags import FlagsRepository
from featureflags.services.db import init_db_engine
from featureflags.services.project_versions import init_project_version_hub


class Container(containers.DeclarativeContainer):
    """
    Container with http dependencies.
    """

    wiring_config = containers.WiringConfiguration(
        packages=[
            "featureflags.services",
            "featureflags.http",
        ]
    )

    graph_engine: Engine = providers.Factory(
        Engine,
        providers.Callable(AsyncIOExecutor),
    )
    db_engine = providers.Resource(init_db_engine)
    version_hub = providers.Resource(
        init_project_version_hub,
        db_engine=db_engine,
    )

    # Repos
    # a singleton, so all requests share its cache of project states
    flags_repo = providers.Singleton(
        FlagsRepository,
        db_engine=db_engine,
        graph_engine=graph_engine,
        readonly=config.readonly,
    )
