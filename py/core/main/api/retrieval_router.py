import asyncio
from pathlib import Path
from typing import Any, Optional, Union
from uuid import UUID

import yaml
from fastapi import Body, Depends
from fastapi.responses import StreamingResponse

from core.base import (
    GenerationConfig,
    KGSearchSettings,
    Message,
    R2RException,
    SearchSettings,
)
from core.base.api.models import (
    WrappedCompletionResponse,
    WrappedDocumentSearchResponse,
    WrappedRAGAgentResponse,
    WrappedRAGResponse,
    WrappedSearchResponse,
)
from core.base.logger.base import RunType
from core.providers import (
    HatchetOrchestrationProvider,
    SimpleOrchestrationProvider,
)

from ..services.retrieval_service import RetrievalService
from .base_router import BaseRouter


class RetrievalRouter(BaseRouter):
    def __init__(
        self,
        service: RetrievalService,
        orchestration_provider: Union[
            HatchetOrchestrationProvider, SimpleOrchestrationProvider
        ],
        run_type: RunType = RunType.RETRIEVAL,
    ):
        super().__init__(service, orchestration_provider, run_type)
        self.service: RetrievalService = service  # for type hinting

    def _load_openapi_extras(self):
        yaml_path = (
            Path(__file__).parent / "data" / "retrieval_router_openapi.yml"
        )
        with open(yaml_path, "r") as yaml_file:
            yaml_content = yaml.safe_load(yaml_file)
        return yaml_content

    def _register_workflows(self):
        pass

    def _select_filters(
        self,
        auth_user: Any,
        search_settings: Union[SearchSettings, KGSearchSettings],
    ) -> dict[str, Any]:
        selected_collections = {
            str(cid) for cid in set(search_settings.selected_collection_ids)
        }

        if auth_user.is_superuser:
            if selected_collections:
                # For superusers, we only filter by selected collections
                filters = {
                    "collection_ids": {"$overlap": list(selected_collections)}
                }
            else:
                filters = {}
        else:
            user_collections = set(auth_user.collection_ids)

            if selected_collections:
                allowed_collections = user_collections.intersection(
                    selected_collections
                )
            else:
                allowed_collections = user_collections
            # for non-superusers, we filter by user_id and selected & allowed collections
            filters = {
                "$or": [
                    {"user_id": {"$eq": auth_user.id}},
                    {
                        "collection_ids": {
                            "$overlap": list(allowed_collections)
                        }
                    },
                ]  # type: ignore
            }

        if search_settings.filters != {}:
            filters = {"$and": [filters, search_settings.filters]}  # type: ignore

        return filters

    def _setup_routes(self):
        search_extras = self.openapi_extras.get("search", {})
        search_descriptions = search_extras.get("input_descriptions", {})

        @self.router.post(
            "/search_documents",
            openapi_extra=search_extras.get("openapi_extra"),
        )
        @self.base_endpoint
        async def search_documents(
            query: str = Body(
                ..., description=search_descriptions.get("query")
            ),
            settings: SearchSettings = Body(
                default_factory=SearchSettings,
                description="Settings for document search",
            ),
            auth_user=Depends(self.service.providers.auth.auth_wrapper),
        ) -> WrappedDocumentSearchResponse:  # type: ignore
            """
            Perform a search query on the vector database and knowledge graph.

            This endpoint allows for complex filtering of search results using PostgreSQL-based queries.
            Filters can be applied to various fields such as document_id, and internal metadata values.


            Allowed operators include `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `like`, `ilike`, `in`, and `nin`.
            """

            query_embedding = (
                await self.service.providers.embedding.async_get_embedding(
                    query
                )
            )
            results = await self.service.search_documents(
                query=query,
                query_embedding=query_embedding,
                settings=settings,
            )
            return results

        @self.router.post(
            "/search",
            openapi_extra=search_extras.get("openapi_extra"),
        )
        @self.base_endpoint
        async def search_app(
            query: str = Body(
                ..., description=search_descriptions.get("query")
            ),
            vector_search_settings: SearchSettings = Body(
                default_factory=SearchSettings,
                description=search_descriptions.get("vector_search_settings"),
            ),
            kg_search_settings: KGSearchSettings = Body(
                default_factory=KGSearchSettings,
                description=search_descriptions.get("kg_search_settings"),
            ),
            auth_user=Depends(self.service.providers.auth.auth_wrapper),
        ) -> WrappedSearchResponse:  # type: ignore
            """
            Perform a search query on the vector database and knowledge graph.

            This endpoint allows for complex filtering of search results using PostgreSQL-based queries.
            Filters can be applied to various fields such as document_id, and internal metadata values.


            Allowed operators include `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `like`, `ilike`, `in`, and `nin`.
            """

            vector_search_settings.filters = self._select_filters(
                auth_user, vector_search_settings
            )

            kg_search_settings.filters = self._select_filters(
                auth_user, kg_search_settings
            )

            results = await self.service.search(
                query=query,
                vector_search_settings=vector_search_settings,
                kg_search_settings=kg_search_settings,
            )
            return results

        rag_extras = self.openapi_extras.get("rag", {})
        rag_descriptions = rag_extras.get("input_descriptions", {})

        @self.router.post(
            "/rag",
            openapi_extra=rag_extras.get("openapi_extra"),
        )
        @self.base_endpoint
        async def rag_app(
            query: str = Body(..., description=rag_descriptions.get("query")),
            vector_search_settings: SearchSettings = Body(
                default_factory=SearchSettings,
                description=rag_descriptions.get("vector_search_settings"),
            ),
            kg_search_settings: KGSearchSettings = Body(
                default_factory=KGSearchSettings,
                description=rag_descriptions.get("kg_search_settings"),
            ),
            rag_generation_config: GenerationConfig = Body(
                default_factory=GenerationConfig,
                description=rag_descriptions.get("rag_generation_config"),
            ),
            task_prompt_override: Optional[str] = Body(
                None, description=rag_descriptions.get("task_prompt_override")
            ),
            include_title_if_available: bool = Body(
                False,
                description=rag_descriptions.get("include_title_if_available"),
            ),
            auth_user=Depends(self.service.providers.auth.auth_wrapper),
        ) -> WrappedRAGResponse:  # type: ignore
            """
            Execute a RAG (Retrieval-Augmented Generation) query.

            This endpoint combines search results with language model generation.
            It supports the same filtering capabilities as the search endpoint,
            allowing for precise control over the retrieved context.

            The generation process can be customized using the rag_generation_config parameter.
            """

            vector_search_settings.filters = self._select_filters(
                auth_user, vector_search_settings
            )

            response = await self.service.rag(
                query=query,
                vector_search_settings=vector_search_settings,
                kg_search_settings=kg_search_settings,
                rag_generation_config=rag_generation_config,
                task_prompt_override=task_prompt_override,
                include_title_if_available=include_title_if_available,
            )

            if rag_generation_config.stream:

                async def stream_generator():
                    async for chunk in response:
                        yield chunk
                        await asyncio.sleep(0)

                return StreamingResponse(
                    stream_generator(), media_type="application/json"
                )  # type: ignore
            else:
                return response

        agent_extras = self.openapi_extras.get("agent", {})
        agent_descriptions = agent_extras.get("input_descriptions", {})

        @self.router.post(
            "/agent",
            openapi_extra=agent_extras.get("openapi_extra"),
        )
        @self.base_endpoint
        async def agent_app(
            message: Optional[Message] = Body(
                None, description=agent_descriptions.get("message")
            ),
            messages: Optional[list[Message]] = Body(
                None,
                description=agent_descriptions.get("messages"),
                deprecated=True,
            ),
            vector_search_settings: SearchSettings = Body(
                default_factory=SearchSettings,
                description=agent_descriptions.get("vector_search_settings"),
            ),
            kg_search_settings: KGSearchSettings = Body(
                default_factory=KGSearchSettings,
                description=agent_descriptions.get("kg_search_settings"),
            ),
            rag_generation_config: GenerationConfig = Body(
                default_factory=GenerationConfig,
                description=agent_descriptions.get("rag_generation_config"),
            ),
            task_prompt_override: Optional[str] = Body(
                None,
                description=agent_descriptions.get("task_prompt_override"),
            ),
            include_title_if_available: bool = Body(
                True,
                description=agent_descriptions.get(
                    "include_title_if_available"
                ),
            ),
            conversation_id: Optional[UUID] = Body(
                None, description=agent_descriptions.get("conversation_id")
            ),
            branch_id: Optional[UUID] = Body(
                None, description=agent_descriptions.get("branch_id")
            ),
            auth_user=Depends(self.service.providers.auth.auth_wrapper),
        ) -> WrappedRAGAgentResponse:  # type: ignore
            """
            Implement an agent-based interaction for complex query processing.

            This endpoint supports multi-turn conversations and can handle complex queries
            by breaking them down into sub-tasks. It uses the same filtering capabilities
            as the search and RAG endpoints for retrieving relevant information.

            The agent's behavior can be customized using the rag_generation_config and
            task_prompt_override parameters.
            """

            vector_search_settings.filters = self._select_filters(
                auth_user, vector_search_settings
            )

            kg_search_settings.filters = vector_search_settings.filters
            try:
                response = await self.service.agent(
                    message=message,
                    messages=messages,
                    vector_search_settings=vector_search_settings,
                    kg_search_settings=kg_search_settings,
                    rag_generation_config=rag_generation_config,
                    task_prompt_override=task_prompt_override,
                    include_title_if_available=include_title_if_available,
                    conversation_id=(
                        str(conversation_id) if conversation_id else None
                    ),
                    branch_id=str(branch_id) if branch_id else None,
                )

                if rag_generation_config.stream:

                    async def stream_generator():
                        content = ""
                        async for chunk in response:
                            yield chunk
                            content += chunk
                            await asyncio.sleep(0)

                    return StreamingResponse(
                        stream_generator(), media_type="application/json"
                    )  # type: ignore
                else:
                    return response
            except Exception as e:
                raise R2RException(str(e), 500)

        @self.router.post("/completion")
        @self.base_endpoint
        async def completion(
            messages: list[Message] = Body(
                ..., description="The messages to complete"
            ),
            generation_config: GenerationConfig = Body(
                default_factory=GenerationConfig,
                description="The generation config",
            ),
            auth_user=Depends(self.service.providers.auth.auth_wrapper),
            response_model=WrappedCompletionResponse,
        ):
            """
            Generate completions for a list of messages.

            This endpoint uses the language model to generate completions for the provided messages.
            The generation process can be customized using the generation_config parameter.
            """
            print("messages = ", messages)

            return await self.service.completion(
                messages=[message.to_dict() for message in messages],
                generation_config=generation_config,
            )

        @self.router.post("/embedding")
        @self.base_endpoint
        async def embedding(
            content: str = Body(..., description="The content to embed"),
            auth_user=Depends(self.service.providers.auth.auth_wrapper),
            response_model=WrappedCompletionResponse,
        ):
            """
            Generate completions for a list of messages.

            This endpoint uses the language model to generate completions for the provided messages.
            The generation process can be customized using the generation_config parameter.
            """

            return await self.service.providers.embedding.async_get_embedding(
                text=content
            )
