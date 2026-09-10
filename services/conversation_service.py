from repositories.repository import conversation_repository
from schemas.conversation import CreateConversationRequest, ConversationPage


class ConversationService:
    async def create(self, request: CreateConversationRequest, *, user_id: str):
        return await conversation_repository.create(request.title, user_id=user_id)

    async def get(self, conversation_id: str, *, user_id: str):
        return await conversation_repository.get(conversation_id, user_id=user_id)

    async def list(self, limit: int, *, user_id: str):
        return ConversationPage(
            items=await conversation_repository.list(limit, user_id=user_id)
        )

    async def get_run(
        self, conversation_id: str, run_id: str, *, user_id: str
    ):
        return await conversation_repository.get_run(
            conversation_id, run_id, user_id=user_id
        )


conversation_service = ConversationService()
