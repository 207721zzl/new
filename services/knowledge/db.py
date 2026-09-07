from packages.platform.database import Database
database = Database("knowledge")
def session_factory():
    return database.session_factory()
get_admin_session = database.session
def get_admin_session_factory():
    return session_factory
