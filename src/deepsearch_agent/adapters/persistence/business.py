"""Assembly-time repository selection; PostgreSQL imports stay optional."""
def build_database(settings):
    if settings.backend=='sqlite':
        return None
    from deepsearch_agent.adapters.persistence.postgres import PostgresDatabase
    return PostgresDatabase(settings)


def access_store(database, path):
    if database is None:
        from deepsearch_agent.adapters.persistence.access import AccessStore
        return AccessStore(path)
    from deepsearch_agent.adapters.persistence.postgres_access import PostgresAccessStore
    return PostgresAccessStore(database)


def conversation_store(database, workspace, path):
    if database is None:
        from deepsearch_agent.workspace.conversations import ConversationStore
        return ConversationStore(path)
    from deepsearch_agent.adapters.persistence.postgres_conversations import PostgresConversationStore
    return PostgresConversationStore(database,workspace,path)


def research_run_store(database, workspace, path):
    if database is None:
        from deepsearch_agent.adapters.persistence.research_runs import ResearchRunStore
        return ResearchRunStore(path)
    from deepsearch_agent.adapters.persistence.postgres_runs import PostgresResearchRunStore
    return PostgresResearchRunStore(database,workspace,path)
