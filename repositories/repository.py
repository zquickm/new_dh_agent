from config.settings import get_settings
from database.runtime import database_runtime
from repositories.memory_conversation_repository import (
    conversation_repository as memory_conversation_repository,
)
from repositories.mysql_conversation_repository import MySQLConversationRepository


conversation_repository = (
    MySQLConversationRepository(database_runtime)
    if get_settings().persistence.enabled
    else memory_conversation_repository
)
