import unittest

from services.rag_buffer_service import RagBufferService
from tests.fakes.memory_conversation_repository import MemoryConversationRepository


class RagBufferServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_persists_deduplicates_and_isolates_conversations(self):
        repository = MemoryConversationRepository()
        first = await repository.create("一", user_id="1")
        second = await repository.create("二", user_id="1")
        service = RagBufferService(repository)
        chunk = {
            "text": "证据正文",
            "source": "资料.pdf",
            "score": 0.9,
            "knowledge_id": "kb-1",
            "document_id": "doc-1",
            "chunk_id": "chunk-1",
            "page_index": 2,
        }

        initial = await service.persist_chunks(
            first.id, [chunk], user_id="1", query="问题", run_id="run-1"
        )
        repeated = await service.persist_chunks(
            first.id, [chunk], user_id="1", query="另一个问题", run_id="run-2"
        )
        isolated = await service.persist_chunks(
            second.id, [chunk], user_id="1", query="问题", run_id="run-3"
        )

        self.assertEqual(initial[0].buffer_id, repeated[0].buffer_id)
        self.assertNotEqual(initial[0].buffer_id, isolated[0].buffer_id)
        self.assertEqual(
            [item.buffer_id for item in await service.list_entries(first.id, user_id="1")],
            [initial[0].buffer_id],
        )

    async def test_changed_text_creates_a_new_stable_entry(self):
        repository = MemoryConversationRepository()
        conversation = await repository.create("测试", user_id="1")
        service = RagBufferService(repository)
        base = {"source": "资料.pdf", "chunk_id": "chunk-1", "text": "旧正文"}
        old = await service.persist_chunks(
            conversation.id, [base], user_id="1", query="问题", run_id="run-1"
        )
        new = await service.persist_chunks(
            conversation.id,
            [{**base, "text": "新正文"}],
            user_id="1",
            query="问题",
            run_id="run-2",
        )
        self.assertNotEqual(old[0].buffer_id, new[0].buffer_id)

    async def test_identity_normalizes_numeric_locations_and_identifier_types(self):
        repository = MemoryConversationRepository()
        conversation = await repository.create("测试", user_id="1")
        service = RagBufferService(repository)
        base = {
            "source": "资料.pdf",
            "document_id": 12,
            "chunk_id": 34,
            "page_index": "2",
            "line": "8",
            "text": "正文",
        }

        first = await service.persist_chunks(
            conversation.id, [base], user_id="1", query="问题", run_id="run-1"
        )
        repeated = await service.persist_chunks(
            conversation.id,
            [{**base, "document_id": "12", "chunk_id": "34", "page_index": 2, "line": 8}],
            user_id="1",
            query="问题",
            run_id="run-2",
        )

        self.assertEqual(first[0].buffer_id, repeated[0].buffer_id)
