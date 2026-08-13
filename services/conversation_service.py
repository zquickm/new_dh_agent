from repositories.memory_conversation_repository import conversation_repository
from schemas.conversation import CreateConversationRequest, ConversationPage


class ConversationService:
    async def create(self, request: CreateConversationRequest):
        return await conversation_repository.create(request.title)

    async def get(self, conversation_id: str):
        return await conversation_repository.get(conversation_id)

    async def list(self, limit: int):
        return ConversationPage(items=await conversation_repository.list(limit))


conversation_service = ConversationService()
