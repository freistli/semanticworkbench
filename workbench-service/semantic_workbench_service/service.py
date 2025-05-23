import asyncio
import contextlib
import datetime
import json
import logging
import urllib.parse
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import (
    Annotated,
    Any,
    AsyncContextManager,
    AsyncIterator,
    Callable,
    NoReturn,
)

import asgi_correlation_id
import starlette.background
from asgi_correlation_id import CorrelationIdMiddleware
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from semantic_workbench_api_model.assistant_model import (
    ConfigPutRequestModel,
    ConfigResponseModel,
    ServiceInfoModel,
    StateDescriptionListResponseModel,
    StatePutRequestModel,
    StateResponseModel,
)
from semantic_workbench_api_model.workbench_model import (
    Assistant,
    AssistantList,
    AssistantServiceInfoList,
    AssistantServiceRegistration,
    AssistantServiceRegistrationList,
    AssistantStateEvent,
    Conversation,
    ConversationEvent,
    ConversationEventType,
    ConversationImportResult,
    ConversationList,
    ConversationMessage,
    ConversationMessageDebug,
    ConversationMessageList,
    ConversationParticipant,
    ConversationParticipantList,
    ConversationShare,
    ConversationShareList,
    ConversationShareRedemption,
    ConversationShareRedemptionList,
    FileList,
    FileVersions,
    MessageType,
    NewAssistant,
    NewAssistantServiceRegistration,
    NewConversation,
    NewConversationMessage,
    NewConversationShare,
    ParticipantRole,
    UpdateAssistant,
    UpdateAssistantServiceRegistration,
    UpdateAssistantServiceRegistrationUrl,
    UpdateConversation,
    UpdateFile,
    UpdateParticipant,
    UpdateUser,
    User,
    UserList,
)
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from sse_starlette import EventSourceResponse, ServerSentEvent

from semantic_workbench_service import azure_speech

from . import assistant_api_key, auth, controller, db, files, middleware, settings
from .event import ConversationEventQueueItem

logger = logging.getLogger(__name__)


def init(
    app: FastAPI,
    register_lifespan_handler: Callable[[Callable[[], AsyncContextManager[None]]], None],
) -> None:
    api_key_store = assistant_api_key.get_store()
    stop_signal: asyncio.Event = asyncio.Event()

    conversation_sse_queues_lock = asyncio.Lock()
    conversation_sse_queues: dict[uuid.UUID, set[asyncio.Queue[ConversationEvent]]] = defaultdict(set)

    user_sse_queues_lock = asyncio.Lock()
    user_sse_queues: dict[str, set[asyncio.Queue[uuid.UUID]]] = defaultdict(set)

    assistant_event_queues: dict[uuid.UUID, asyncio.Queue[ConversationEvent]] = {}

    background_tasks: set[asyncio.Task] = set()

    def _controller_get_session() -> AsyncContextManager[AsyncSession]:
        return db.create_session(app.state.db_engine)

    async def _forward_events_to_assistant(
        assistant_id: uuid.UUID, event_queue: asyncio.Queue[ConversationEvent]
    ) -> NoReturn:
        while True:
            try:
                event = await event_queue.get()
                event_queue.task_done()

                asgi_correlation_id.correlation_id.set(event.correlation_id)

                start_time = datetime.datetime.now(datetime.UTC)

                await assistant_controller.forward_event_to_assistant(assistant_id=assistant_id, event=event)

                end_time = datetime.datetime.now(datetime.UTC)
                logger.debug(
                    "forwarded event to assistant; assistant_id: %s, conversation_id: %s, event_id: %s,"
                    " duration: %s, time since event: %s",
                    assistant_id,
                    event.conversation_id,
                    event.id,
                    end_time - start_time,
                    end_time - event.timestamp,
                )

            except Exception:
                logger.exception("exception in _forward_events_to_assistant")

    async def _notify_event(queue_item: ConversationEventQueueItem) -> None:
        if stop_signal.is_set():
            logger.warning(
                "ignoring event due to stop signal; conversation_id: %s, event: %s, id: %s",
                queue_item.event.conversation_id,
                queue_item.event.event,
                queue_item.event.id,
            )
            return

        logger.debug(
            "received event to notify; conversation_id: %s, event: %s, event_id: %s, audience: %s",
            queue_item.event.conversation_id,
            queue_item.event.event,
            queue_item.event.id,
            queue_item.event_audience,
        )

        if "user" in queue_item.event_audience:
            async with conversation_sse_queues_lock:
                for queue in conversation_sse_queues.get(queue_item.event.conversation_id, {}):
                    await queue.put(queue_item.event)
            logger.debug(
                "enqueued event for SSE; conversation_id: %s, event: %s, event_id: %s",
                queue_item.event.conversation_id,
                queue_item.event.event,
                queue_item.event.id,
            )

            if queue_item.event.event in [
                ConversationEventType.message_created,
                ConversationEventType.message_deleted,
                ConversationEventType.conversation_updated,
                ConversationEventType.participant_created,
                ConversationEventType.participant_updated,
            ]:
                task = asyncio.create_task(
                    _notify_user_event(queue_item.event.conversation_id), name="notify_user_event"
                )
                background_tasks.add(task)
                task.add_done_callback(background_tasks.discard)

        if "assistant" in queue_item.event_audience:
            async with _controller_get_session() as session:
                assistant_ids = (
                    await session.exec(
                        select(db.Assistant.assistant_id)
                        .join(
                            db.AssistantParticipant,
                            col(db.Assistant.assistant_id) == col(db.AssistantParticipant.assistant_id),
                        )
                        .join(db.AssistantServiceRegistration)
                        .where(col(db.AssistantServiceRegistration.assistant_service_online).is_(True))
                        .where(col(db.AssistantParticipant.active_participant).is_(True))
                        .where(db.AssistantParticipant.conversation_id == queue_item.event.conversation_id)
                    )
                ).all()

            for assistant_id in assistant_ids:
                if assistant_id not in assistant_event_queues:
                    queue = asyncio.Queue()
                    assistant_event_queues[assistant_id] = queue
                    task = asyncio.create_task(
                        _forward_events_to_assistant(assistant_id, queue),
                        name=f"forward_events_to_{assistant_id}",
                    )
                    background_tasks.add(task)

                await assistant_event_queues[assistant_id].put(queue_item.event)
                logger.debug(
                    "enqueued event for assistant; conversation_id: %s, event: %s, event_id: %s, assistant_id: %s",
                    queue_item.event.conversation_id,
                    queue_item.event.event,
                    queue_item.event.id,
                    assistant_id,
                )

    async def _notify_user_event(conversation_id: uuid.UUID) -> None:
        listening_user_ids = set(user_sse_queues.keys())
        async with _controller_get_session() as session:
            active_user_participants = (
                await session.exec(
                    select(db.UserParticipant.user_id).where(
                        col(db.UserParticipant.active_participant).is_(True),
                        db.UserParticipant.conversation_id == conversation_id,
                        col(db.UserParticipant.user_id).in_(listening_user_ids),
                    )
                )
            ).all()

        if not active_user_participants:
            return

        async with user_sse_queues_lock:
            for user_id in active_user_participants:
                for queue in user_sse_queues.get(user_id, {}):
                    await queue.put(conversation_id)
                    logger.debug(
                        "enqueued event for user SSE; user_id: %s, conversation_id: %s", user_id, conversation_id
                    )

    assistant_client_pool = controller.AssistantServiceClientPool(api_key_store=api_key_store)

    assistant_service_registration_controller = controller.AssistantServiceRegistrationController(
        get_session=_controller_get_session,
        notify_event=_notify_event,
        api_key_store=api_key_store,
        client_pool=assistant_client_pool,
    )

    app.add_middleware(
        middleware.AuthMiddleware,
        exclude_methods={"OPTIONS"},
        exclude_paths=set(settings.service.anonymous_paths),
        api_key_source=assistant_service_registration_controller.api_key_source,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )
    app.add_middleware(CorrelationIdMiddleware)

    user_controller = controller.UserController(get_session=_controller_get_session)
    assistant_controller = controller.AssistantController(
        get_session=_controller_get_session,
        notify_event=_notify_event,
        client_pool=assistant_client_pool,
        file_storage=files.Storage(settings.storage),
    )
    conversation_controller = controller.ConversationController(
        get_session=_controller_get_session,
        notify_event=_notify_event,
        assistant_controller=assistant_controller,
    )
    conversation_share_controller = controller.ConversationShareController(
        get_session=_controller_get_session,
        notify_event=_notify_event,
    )

    file_controller = controller.FileController(
        get_session=_controller_get_session,
        notify_event=_notify_event,
        file_storage=files.Storage(settings.storage),
    )

    @asynccontextmanager
    async def _lifespan() -> AsyncIterator[None]:
        async with db.create_engine(settings.db) as engine:
            await db.bootstrap_db(engine, settings=settings.db)

            app.state.db_engine = engine

            background_tasks.add(
                asyncio.create_task(
                    _update_assistant_service_online_status(), name="update_assistant_service_online_status"
                ),
            )

            try:
                yield

            finally:
                stop_signal.set()

                for task in background_tasks:
                    task.cancel()

                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.gather(*background_tasks, return_exceptions=True)

    register_lifespan_handler(_lifespan)

    async def _update_assistant_service_online_status() -> NoReturn:
        while True:
            try:
                await asyncio.sleep(settings.service.assistant_service_online_check_interval_seconds)
                await assistant_service_registration_controller.check_assistant_service_online_expired()

            except Exception:
                logger.exception("exception in _update_assistant_service_online_status")

    @app.get("/")
    async def root() -> Response:
        return Response(status_code=status.HTTP_200_OK, content="")

    @app.get("/users")
    async def list_users(
        user_ids: list[str] = Query(alias="id"),
    ) -> UserList:
        return await user_controller.get_users(user_ids=user_ids)

    @app.get("/users/me")
    async def get_user_me(
        user_principal: auth.DependsUserPrincipal,
    ) -> User:
        return await user_controller.get_user_me(user_principal=user_principal)

    @app.put("/users/me")
    async def update_user_me(
        user_principal: auth.DependsUserPrincipal,
        update_user: UpdateUser,
    ) -> User:
        return await user_controller.update_user(
            user_principal=user_principal, user_id=user_principal.user_id, update_user=update_user
        )

    @app.post("/assistant-service-registrations")
    async def create_assistant_service_registration(
        user_principal: auth.DependsUserPrincipal,
        new_assistant_service: NewAssistantServiceRegistration,
    ) -> AssistantServiceRegistration:
        return await assistant_service_registration_controller.create_registration(
            user_principal=user_principal, new_assistant_service=new_assistant_service
        )

    @app.put("/assistant-service-registrations/{assistant_service_id:path}")
    async def update_assistant_service_registration_url(
        principal: auth.DependsAssistantServicePrincipal,
        assistant_service_id: str,
        update_assistant_service: UpdateAssistantServiceRegistrationUrl,
        background_tasks: BackgroundTasks,
    ) -> AssistantServiceRegistration:
        registration, task_args = await assistant_service_registration_controller.update_assistant_service_url(
            assistant_service_principal=principal,
            assistant_service_id=assistant_service_id,
            update_assistant_service_url=update_assistant_service,
        )
        if task_args:
            background_tasks.add_task(*task_args)
        return registration

    @app.patch("/assistant-service-registrations/{assistant_service_id:path}")
    async def update_assistant_service_registration(
        principal: auth.DependsUserPrincipal,
        assistant_service_id: str,
        update_assistant_service: UpdateAssistantServiceRegistration,
    ) -> AssistantServiceRegistration:
        return await assistant_service_registration_controller.update_registration(
            user_principal=principal,
            assistant_service_id=assistant_service_id,
            update_assistant_service=update_assistant_service,
        )

    @app.post("/assistant-service-registrations/{assistant_service_id:path}/api-key")
    async def reset_assistant_service_registration_api_key(
        user_principal: auth.DependsUserPrincipal,
        assistant_service_id: str,
    ) -> AssistantServiceRegistration:
        return await assistant_service_registration_controller.reset_api_key(
            user_principal=user_principal, assistant_service_id=assistant_service_id
        )

    @app.get("/assistant-service-registrations")
    async def list_assistant_service_registrations(
        user_principal: auth.DependsUserPrincipal,
        user_ids: Annotated[list[str], Query(alias="user_id")] = [],
        assistant_service_online: Annotated[bool | None, Query(alias="assistant_service_online")] = None,
    ) -> AssistantServiceRegistrationList:
        user_id_set = set([user_principal.user_id if user_id == "me" else user_id for user_id in user_ids])
        return await assistant_service_registration_controller.get_registrations(
            user_ids=user_id_set, assistant_service_online=assistant_service_online
        )

    @app.get("/assistant-service-registrations/{assistant_service_id:path}")
    async def get_assistant_service_registration(
        user_principal: auth.DependsUserPrincipal, assistant_service_id: str
    ) -> AssistantServiceRegistration:
        return await assistant_service_registration_controller.get_registration(
            assistant_service_id=assistant_service_id
        )

    @app.delete(
        "/assistant-service-registrations/{assistant_service_id:path}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_assistant_service(
        user_principal: auth.DependsUserPrincipal,
        assistant_service_id: str,
    ) -> None:
        return await assistant_service_registration_controller.delete_registration(
            user_principal=user_principal, assistant_service_id=assistant_service_id
        )

    @app.get("/assistant-services/{assistant_service_id:path}/info")
    @app.get("/assistant-services/{assistant_service_id:path}")
    async def get_assistant_service_info(
        user_principal: auth.DependsUserPrincipal, assistant_service_id: str
    ) -> ServiceInfoModel:
        return await assistant_service_registration_controller.get_service_info(
            assistant_service_id=assistant_service_id
        )

    @app.get("/assistant-services")
    async def list_assistant_service_infos(
        user_principal: auth.DependsUserPrincipal,
        user_ids: Annotated[list[str], Query(alias="user_id")] = [],
    ) -> AssistantServiceInfoList:
        user_id_set = set([user_principal.user_id if user_id == "me" else user_id for user_id in user_ids])
        return await assistant_service_registration_controller.get_service_infos(user_ids=user_id_set)

    @app.get("/assistants")
    async def list_assistants(
        user_principal: auth.DependsUserPrincipal, conversation_id: uuid.UUID | None = None
    ) -> AssistantList:
        return await assistant_controller.get_assistants(user_principal=user_principal, conversation_id=conversation_id)

    @app.get("/assistants/{assistant_id}")
    async def get_assistant(user_principal: auth.DependsUserPrincipal, assistant_id: uuid.UUID) -> Assistant:
        return await assistant_controller.get_assistant(user_principal=user_principal, assistant_id=assistant_id)

    @app.post("/assistants", status_code=status.HTTP_201_CREATED)
    async def create_assistant(
        new_assistant: NewAssistant,
        user_principal: auth.DependsUserPrincipal,
    ) -> Assistant:
        return await assistant_controller.create_assistant(user_principal=user_principal, new_assistant=new_assistant)

    @app.patch("/assistants/{assistant_id}")
    async def update_assistant(
        assistant_id: uuid.UUID,
        update_assistant: UpdateAssistant,
        user_principal: auth.DependsUserPrincipal,
    ) -> Assistant:
        return await assistant_controller.update_assistant(
            user_principal=user_principal, assistant_id=assistant_id, update_assistant=update_assistant
        )

    @app.get(
        "/assistants/{assistant_id}/export", description="Export an assistant's configuration and conversation data."
    )
    async def export_assistant(
        user_principal: auth.DependsUserPrincipal,
        assistant_id: uuid.UUID,
    ) -> FileResponse:
        result = await assistant_controller.export_assistant(user_principal=user_principal, assistant_id=assistant_id)

        return FileResponse(
            path=result.file_path,
            media_type=result.content_type,
            filename=result.filename,
            background=starlette.background.BackgroundTask(result.cleanup),
        )

    @app.get(
        "/conversations/export",
        description="Export  one or more conversations and the assistants that participate in them.",
    )
    async def export_conversations(
        user_principal: auth.DependsUserPrincipal,
        conversation_ids: list[uuid.UUID] = Query(alias="id"),
    ) -> FileResponse:
        result = await assistant_controller.export_conversations(
            user_principal=user_principal, conversation_ids=set(conversation_ids)
        )

        return FileResponse(
            path=result.file_path,
            media_type=result.content_type,
            filename=result.filename,
            background=starlette.background.BackgroundTask(result.cleanup),
        )

    @app.post("/conversations/import")
    async def import_conversations(
        from_export: Annotated[UploadFile, File(alias="from_export")],
        user_principal: auth.DependsUserPrincipal,
    ) -> ConversationImportResult:
        return await assistant_controller.import_conversations(
            user_principal=user_principal, from_export=from_export.file
        )

    @app.get("/assistants/{assistant_id}/config")
    async def get_assistant_config(
        user_principal: auth.DependsUserPrincipal,
        assistant_id: uuid.UUID,
    ) -> ConfigResponseModel:
        return await assistant_controller.get_assistant_config(user_principal=user_principal, assistant_id=assistant_id)

    @app.put("/assistants/{assistant_id}/config")
    async def update_assistant_config(
        user_principal: auth.DependsUserPrincipal,
        assistant_id: uuid.UUID,
        updated_config: ConfigPutRequestModel,
    ) -> ConfigResponseModel:
        return await assistant_controller.update_assistant_config(
            user_principal=user_principal,
            assistant_id=assistant_id,
            updated_config=updated_config,
        )

    @app.get("/assistants/{assistant_id}/conversations/{conversation_id}/states")
    async def get_assistant_conversation_state_descriptions(
        user_principal: auth.DependsUserPrincipal,
        assistant_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> StateDescriptionListResponseModel:
        return await assistant_controller.get_assistant_conversation_state_descriptions(
            user_principal=user_principal,
            assistant_id=assistant_id,
            conversation_id=conversation_id,
        )

    @app.get("/assistants/{assistant_id}/conversations/{conversation_id}/states/{state_id}")
    async def get_assistant_conversation_state(
        user_principal: auth.DependsUserPrincipal,
        assistant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        state_id: str,
    ) -> StateResponseModel:
        return await assistant_controller.get_assistant_conversation_state(
            user_principal=user_principal,
            assistant_id=assistant_id,
            conversation_id=conversation_id,
            state_id=state_id,
        )

    @app.put("/assistants/{assistant_id}/conversations/{conversation_id}/states/{state_id}")
    async def update_assistant_conversation_state(
        user_principal: auth.DependsUserPrincipal,
        assistant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        state_id: str,
        updated_state: StatePutRequestModel,
    ) -> StateResponseModel:
        return await assistant_controller.update_assistant_conversation_state(
            user_principal=user_principal,
            assistant_id=assistant_id,
            conversation_id=conversation_id,
            state_id=state_id,
            updated_state=updated_state,
        )

    @app.post("/assistants/{assistant_id}/states/events", status_code=status.HTTP_204_NO_CONTENT)
    async def post_assistant_state_event(
        assistant_id: uuid.UUID,
        state_event: AssistantStateEvent,
        assistant_principal: auth.DependsAssistantPrincipal,
        conversation_id: Annotated[uuid.UUID | None, Query()] = None,
    ) -> None:
        await assistant_controller.post_assistant_state_event(
            assistant_id=assistant_id,
            state_event=state_event,
            assistant_principal=assistant_principal,
            conversation_ids=[conversation_id] if conversation_id else [],
        )

    @app.delete(
        "/assistants/{assistant_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_assistant(
        user_principal: auth.DependsUserPrincipal,
        assistant_id: uuid.UUID,
    ) -> None:
        await assistant_controller.delete_assistant(
            user_principal=user_principal,
            assistant_id=assistant_id,
        )

    @app.get("/assistants/{assistant_id}/conversations")
    async def get_assistant_conversations(
        assistant_id: uuid.UUID,
        user_principal: auth.DependsUserPrincipal,
        latest_message_types: Annotated[list[MessageType], Query(alias="latest_message_type")] = [MessageType.chat],
    ) -> ConversationList:
        return await conversation_controller.get_assistant_conversations(
            user_principal=user_principal,
            assistant_id=assistant_id,
            latest_message_types=set(latest_message_types),
        )

    @app.get("/conversations/{conversation_id}/events")
    async def conversation_server_sent_events(
        conversation_id: uuid.UUID, request: Request, principal: auth.DependsActorPrincipal
    ) -> EventSourceResponse:
        # ensure the principal has access to the conversation
        await conversation_controller.get_conversation(
            conversation_id=conversation_id,
            principal=principal,
            latest_message_types=set(),
        )

        principal_id_type = "assistant_id" if isinstance(principal, auth.AssistantPrincipal) else "user_id"
        principal_id = principal.assistant_id if isinstance(principal, auth.AssistantPrincipal) else principal.user_id

        logger.debug(
            "client connected to sse; %s: %s, conversation_id: %s",
            principal_id_type,
            principal_id,
            conversation_id,
        )
        event_queue = asyncio.Queue[ConversationEvent]()

        async with conversation_sse_queues_lock:
            queues = conversation_sse_queues[conversation_id]
            queues.add(event_queue)

        async def event_generator() -> AsyncIterator[ServerSentEvent]:
            try:
                while True:
                    if stop_signal.is_set():
                        logger.debug("sse stopping due to signal; conversation_id: %s", conversation_id)
                        break

                    try:
                        if await request.is_disconnected():
                            logger.debug("client disconnected from sse; conversation_id: %s", conversation_id)
                            break
                    except Exception:
                        logger.exception(
                            "error checking if client disconnected from sse; conversation_id: %s", conversation_id
                        )
                        break

                    try:
                        try:
                            async with asyncio.timeout(1):
                                conversation_event = await event_queue.get()
                        except asyncio.TimeoutError:
                            continue

                        server_sent_event = ServerSentEvent(
                            id=conversation_event.id,
                            event=conversation_event.event.value,
                            data=conversation_event.model_dump_json(include={"timestamp", "data"}),
                            retry=1000,
                        )
                        yield server_sent_event
                        logger.debug(
                            "sent event to sse client; %s: %s, conversation_id: %s, event: %s, id: %s, time since"
                            " event: %s",
                            principal_id_type,
                            principal_id,
                            conversation_id,
                            conversation_event.event,
                            conversation_event.id,
                            datetime.datetime.now(datetime.UTC) - conversation_event.timestamp,
                        )

                    except Exception:
                        logger.exception("error sending event to sse client; conversation_id: %s", conversation_id)

            finally:
                queues.discard(event_queue)
                if len(queues) == 0:
                    async with conversation_sse_queues_lock:
                        if len(queues) == 0:
                            conversation_sse_queues.pop(conversation_id, None)

        return EventSourceResponse(event_generator(), sep="\n")

    @app.get("/events")
    async def user_server_sent_events(
        request: Request, user_principal: auth.DependsUserPrincipal
    ) -> EventSourceResponse:
        logger.debug("client connected to user events sse; user_id: %s", user_principal.user_id)

        event_queue = asyncio.Queue[uuid.UUID]()

        async with user_sse_queues_lock:
            queues = user_sse_queues[user_principal.user_id]
            queues.add(event_queue)

        async def event_generator() -> AsyncIterator[ServerSentEvent]:
            try:
                while True:
                    if stop_signal.is_set():
                        logger.debug("sse stopping due to signal; user_id: %s", user_principal.user_id)
                        break

                    try:
                        if await request.is_disconnected():
                            logger.debug("client disconnected from sse; user_id: %s", user_principal.user_id)
                            break
                    except Exception:
                        logger.exception(
                            "error checking if client disconnected from sse; user_id: %s", user_principal.user_id
                        )
                        break

                    try:
                        try:
                            async with asyncio.timeout(1):
                                conversation_id = await event_queue.get()
                        except asyncio.TimeoutError:
                            continue

                        server_sent_event = ServerSentEvent(
                            id=uuid.uuid4().hex,
                            event="message.created",
                            data=json.dumps({"conversation_id": str(conversation_id)}),
                            retry=1000,
                        )
                        yield server_sent_event
                        logger.debug(
                            "sent event to user sse client; user_id: %s, event: %s",
                            user_principal.user_id,
                            server_sent_event.event,
                        )

                    except Exception:
                        logger.exception("error sending event to sse client; user_id: %s", user_principal.user_id)

            finally:
                queues.discard(event_queue)
                if len(queues) == 0:
                    async with conversation_sse_queues_lock:
                        if len(queues) == 0:
                            user_sse_queues.pop(user_principal.user_id, None)

        return EventSourceResponse(event_generator(), sep="\n")

    @app.post("/conversations")
    async def create_conversation(
        new_conversation: NewConversation,
        user_principal: auth.DependsUserPrincipal,
    ) -> Conversation:
        return await conversation_controller.create_conversation(
            user_principal=user_principal,
            new_conversation=new_conversation,
        )

    @app.post("/conversations/{owner_id}")
    async def create_conversation_with_owner(
        assistant_principal: auth.DependsAssistantPrincipal,
        new_conversation: NewConversation,
        owner_id: str,
    ) -> Conversation:
        return await conversation_controller.create_conversation_with_owner(
            new_conversation=new_conversation,
            principal=assistant_principal,
            owner_id=owner_id,
        )

    @app.post("/conversations/{conversation_id}")
    async def duplicate_conversation(
        conversation_id: uuid.UUID,
        principal: auth.DependsActorPrincipal,
        new_conversation: NewConversation,
    ) -> ConversationImportResult:
        return await assistant_controller.duplicate_conversation(
            principal=principal, conversation_id=conversation_id, new_conversation=new_conversation
        )

    @app.get("/conversations")
    async def list_conversations(
        principal: auth.DependsActorPrincipal,
        include_inactive: bool = False,
        latest_message_types: Annotated[list[MessageType], Query(alias="latest_message_type")] = [MessageType.chat],
    ) -> ConversationList:
        return await conversation_controller.get_conversations(
            principal=principal,
            include_all_owned=include_inactive,
            latest_message_types=set(latest_message_types),
        )

    @app.get("/conversations/{conversation_id}")
    async def get_conversation(
        conversation_id: uuid.UUID,
        principal: auth.DependsActorPrincipal,
        latest_message_types: Annotated[list[MessageType], Query(alias="latest_message_type")] = [MessageType.chat],
    ) -> Conversation:
        return await conversation_controller.get_conversation(
            principal=principal,
            conversation_id=conversation_id,
            latest_message_types=set(latest_message_types),
        )

    @app.patch("/conversations/{conversation_id}")
    async def update_conversation(
        conversation_id: uuid.UUID,
        update_conversation: UpdateConversation,
        user_principal: auth.DependsActorPrincipal,
    ) -> Conversation:
        return await conversation_controller.update_conversation(
            user_principal=user_principal,
            conversation_id=conversation_id,
            update_conversation=update_conversation,
        )

    @app.get("/conversations/{conversation_id}/participants")
    async def list_conversation_participants(
        conversation_id: uuid.UUID,
        principal: auth.DependsActorPrincipal,
        include_inactive: bool = False,
    ) -> ConversationParticipantList:
        return await conversation_controller.get_conversation_participants(
            principal=principal,
            conversation_id=conversation_id,
            include_inactive=include_inactive,
        )

    def _translate_participant_id_me(principal: auth.ActorPrincipal, participant_id: str) -> str:
        if participant_id != "me":
            return participant_id

        match principal:
            case auth.UserPrincipal():
                return principal.user_id
            case auth.AssistantPrincipal():
                return str(principal.assistant_id)

    @app.get("/conversations/{conversation_id}/participants/{participant_id}")
    async def get_conversation_participant(
        conversation_id: uuid.UUID,
        participant_id: str,
        principal: auth.DependsActorPrincipal,
    ) -> ConversationParticipant:
        participant_id = _translate_participant_id_me(principal, participant_id)

        return await conversation_controller.get_conversation_participant(
            principal=principal,
            conversation_id=conversation_id,
            participant_id=participant_id,
        )

    @app.patch("/conversations/{conversation_id}/participants/{participant_id}")
    @app.put("/conversations/{conversation_id}/participants/{participant_id}")
    async def add_or_update_conversation_participant(
        conversation_id: uuid.UUID,
        participant_id: str,
        update_participant: UpdateParticipant,
        principal: auth.DependsActorPrincipal,
    ) -> ConversationParticipant:
        participant_id = _translate_participant_id_me(principal, participant_id)

        return await conversation_controller.add_or_update_conversation_participant(
            participant_id=participant_id,
            update_participant=update_participant,
            conversation_id=conversation_id,
            principal=principal,
        )

    @app.get("/conversations/{conversation_id}/messages")
    async def list_conversation_messages(
        conversation_id: uuid.UUID,
        principal: auth.DependsActorPrincipal,
        participant_roles: Annotated[list[ParticipantRole] | None, Query(alias="participant_role")] = None,
        participant_ids: Annotated[list[str] | None, Query(alias="participant_id")] = None,
        message_types: Annotated[list[MessageType] | None, Query(alias="message_type")] = None,
        before: Annotated[uuid.UUID | None, Query()] = None,
        after: Annotated[uuid.UUID | None, Query()] = None,
        limit: Annotated[int, Query(lte=500)] = 100,
    ) -> ConversationMessageList:
        return await conversation_controller.get_messages(
            conversation_id=conversation_id,
            principal=principal,
            participant_ids=participant_ids,
            participant_roles=participant_roles,
            message_types=message_types,
            before=before,
            after=after,
            limit=limit,
        )

    @app.post("/conversations/{conversation_id}/messages")
    async def create_conversation_message(
        conversation_id: uuid.UUID,
        new_message: NewConversationMessage,
        principal: auth.DependsActorPrincipal,
        background_tasks: BackgroundTasks,
    ) -> ConversationMessage:
        response, task_args = await conversation_controller.create_conversation_message(
            conversation_id=conversation_id,
            new_message=new_message,
            principal=principal,
        )
        if task_args:
            background_tasks.add_task(*task_args)
        return response

    @app.get(
        "/conversations/{conversation_id}/messages/{message_id}",
    )
    async def get_message(
        conversation_id: uuid.UUID,
        message_id: uuid.UUID,
        principal: auth.DependsActorPrincipal,
    ) -> ConversationMessage:
        return await conversation_controller.get_message(
            conversation_id=conversation_id,
            message_id=message_id,
            principal=principal,
        )

    @app.get(
        "/conversations/{conversation_id}/messages/{message_id}/debug_data",
    )
    async def get_message_debug_data(
        conversation_id: uuid.UUID,
        message_id: uuid.UUID,
        principal: auth.DependsActorPrincipal,
    ) -> ConversationMessageDebug:
        return await conversation_controller.get_message_debug(
            conversation_id=conversation_id,
            message_id=message_id,
            principal=principal,
        )

    @app.delete(
        "/conversations/{conversation_id}/messages/{message_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_message(
        conversation_id: uuid.UUID,
        message_id: uuid.UUID,
        user_principal: auth.DependsUserPrincipal,
    ) -> None:
        await conversation_controller.delete_message(
            conversation_id=conversation_id,
            message_id=message_id,
            user_principal=user_principal,
        )

    @app.put("/conversations/{conversation_id}/files")
    async def upload_files(
        conversation_id: uuid.UUID,
        upload_files: Annotated[list[UploadFile], File(alias="files")],
        principal: auth.DependsActorPrincipal,
        file_metadata_raw: str = Form(alias="metadata", default="{}"),
    ) -> FileList:
        try:
            file_metadata: dict[str, dict[str, Any]] = json.loads(file_metadata_raw)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

        if not isinstance(file_metadata, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="metadata must be a JSON object as a string"
            )

        return await file_controller.upload_files(
            conversation_id=conversation_id,
            upload_files=upload_files,
            principal=principal,
            file_metadata=file_metadata,
        )

    @app.get("/conversations/{conversation_id}/files")
    async def list_files(
        conversation_id: uuid.UUID,
        principal: auth.DependsActorPrincipal,
        prefix: str | None = None,
    ) -> FileList:
        return await file_controller.list_files(conversation_id=conversation_id, principal=principal, prefix=prefix)

    @app.get("/conversations/{conversation_id}/files/{filename:path}/versions")
    async def file_versions(
        conversation_id: uuid.UUID,
        filename: str,
        principal: auth.DependsActorPrincipal,
        version: int | None = None,
    ) -> FileVersions:
        return await file_controller.file_versions(
            conversation_id=conversation_id, filename=filename, principal=principal, version=version
        )

    @app.get("/conversations/{conversation_id}/files/{filename:path}")
    async def download_file(
        conversation_id: uuid.UUID,
        filename: str,
        principal: auth.DependsActorPrincipal,
        version: int | None = None,
    ) -> StreamingResponse:
        result = await file_controller.download_file(
            conversation_id=conversation_id,
            filename=filename,
            principal=principal,
            version=version,
        )

        return StreamingResponse(
            result.stream,
            media_type=result.content_type,
            headers={"Content-Disposition": f'attachment; filename="{urllib.parse.quote(result.filename)}"'},
        )

    @app.patch("/conversations/{conversation_id}/files/{filename:path}")
    async def update_file(
        conversation_id: uuid.UUID,
        filename: str,
        principal: auth.DependsActorPrincipal,
        update_file: UpdateFile,
    ) -> FileVersions:
        return await file_controller.update_file_metadata(
            conversation_id=conversation_id,
            filename=filename,
            principal=principal,
            metadata=update_file.metadata,
        )

    @app.delete("/conversations/{conversation_id}/files/{filename:path}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_file(
        conversation_id: uuid.UUID,
        filename: str,
        principal: auth.DependsActorPrincipal,
    ) -> None:
        await file_controller.delete_file(
            conversation_id=conversation_id,
            filename=filename,
            principal=principal,
        )

    @app.post("/conversation-shares")
    async def create_conversation_share(
        user_principal: auth.DependsUserPrincipal,
        new_conversation_share: NewConversationShare,
    ) -> ConversationShare:
        return await conversation_share_controller.create_conversation_share(
            user_principal=user_principal,
            new_conversation_share=new_conversation_share,
        )

    # create_conversation_share_with_owner
    @app.post("/conversation-shares/{owner_id}")
    async def create_conversation_share_with_owner(
        new_conversation_share: NewConversationShare,
        owner_id: str,
    ) -> ConversationShare:
        return await conversation_share_controller.create_conversation_share_with_owner(
            new_conversation_share=new_conversation_share,
            owner_id=owner_id,
        )

    @app.get("/conversation-shares")
    async def list_conversation_shares(
        user_principal: auth.DependsUserPrincipal,
        include_unredeemable: bool = False,
        conversation_id: uuid.UUID | None = None,
    ) -> ConversationShareList:
        return await conversation_share_controller.get_conversation_shares(
            user_principal=user_principal,
            conversation_id=conversation_id,
            include_unredeemable=include_unredeemable,
        )

    @app.get("/conversation-shares/{conversation_share_id}")
    async def get_conversation_share(
        user_principal: auth.DependsUserPrincipal,
        conversation_share_id: uuid.UUID,
    ) -> ConversationShare:
        return await conversation_share_controller.get_conversation_share(
            user_principal=user_principal,
            conversation_share_id=conversation_share_id,
        )

    @app.delete("/conversation-shares/{conversation_share_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_conversation_share(
        user_principal: auth.DependsUserPrincipal,
        conversation_share_id: uuid.UUID,
    ) -> None:
        await conversation_share_controller.delete_conversation_share(
            user_principal=user_principal,
            conversation_share_id=conversation_share_id,
        )

    @app.post("/conversation-shares/{conversation_share_id}/redemptions")
    async def redeem_conversation_share(
        user_principal: auth.DependsUserPrincipal,
        conversation_share_id: uuid.UUID,
    ) -> ConversationShareRedemption:
        return await conversation_share_controller.redeem_conversation_share(
            user_principal=user_principal,
            conversation_share_id=conversation_share_id,
        )

    @app.get("/conversation-shares/{conversation_share_id}/redemptions")
    async def list_conversation_share_redemptions(
        user_principal: auth.DependsUserPrincipal,
        conversation_share_id: uuid.UUID,
    ) -> ConversationShareRedemptionList:
        return await conversation_share_controller.get_redemptions_for_share(
            user_principal=user_principal,
            conversation_share_id=conversation_share_id,
        )

    @app.get("/conversation-share-redemptions")
    async def list_conversation_share_redemptions_for_user(
        user_principal: auth.DependsUserPrincipal,
    ) -> ConversationShareRedemptionList:
        return await conversation_share_controller.get_redemptions_for_user(
            user_principal=user_principal,
        )

    @app.get("/azure-speech/token")
    async def get_azure_speech_token() -> dict[str, str]:
        return azure_speech.get_token()
