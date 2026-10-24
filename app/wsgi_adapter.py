from threading import Lock

from a2wsgi import ASGIMiddleware

from .main import app

_application = None
_application_lock = Lock()


def application(environ, start_response):
    global _application
    if _application is None:
        with _application_lock:
            if _application is None:
                _application = ASGIMiddleware(app)
    return _application(environ, start_response)
