"""Import has no side effects. Gunicorn invokes the explicit factory."""
def create_from_env():
    import os
    from cloud.identity.config import WebSettings
    from cloud.web import create_web_app
    return create_web_app(WebSettings.from_mapping(os.environ))
