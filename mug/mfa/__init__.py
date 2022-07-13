import os
import json
import duo_client
from pathlib import Path
from redis import Redis
from babel.support import Translations
from functools import partial
from datetime import datetime, timezone
from werkzeug.urls import url_parse
from werkzeug.wrappers import Request, Response
from werkzeug.routing import Map, Rule
from werkzeug.exceptions import HTTPException, NotFound
from werkzeug.middleware.shared_data import SharedDataMiddleware
from werkzeug.utils import redirect
from jinja2 import Environment, FileSystemLoader


class MFA(object):
    def __init__(self, **config):
        self.redis = config.get("redis") and Redis(
            unix_socket_path=config.get("redis"), decode_responses=True
        )
        self.api = duo_client.Admin(
            ikey=config.get("ikey"), skey=config.get("skey"), host=config.get("host")
        )
        self.template_path = str(Path(__file__).parent.joinpath('templates'))
        self.locale_path = str(Path(__file__).parent.joinpath('locale'))
        self.lifetime = config.get('lifetime')

    def dispatch_request(self, request):
        username = request.remote_user
        if self.redis:
            cached = self.redis.get(username)
            if cached is None:
                users = list(self.fetch_users_data(username))
                self.redis.set(username, json.dumps(users), ex=self.lifetime)
            else:
                users = json.loads(cached)
        else:
            users = self.fetch_users_data(username)
        context = {
            "username": username,
            "data": dict(users) if users else None,
        }
        jinja = Environment(
            loader=FileSystemLoader(self.template_path),
            autoescape=True,
            extensions=(
                "jinja2.ext.i18n",
                "jinja2.ext.debug",
                "jinja2_time.TimeExtension",
            ),
        )
        languages = list(request.accept_languages.values())
        translations = Translations.load(self.locale_path, languages, "mfa")
        jinja.install_gettext_translations(translations)
        jinja.filters["fromtimestamp"] = partial(
            datetime.fromtimestamp, tz=timezone.utc
        )
        t = jinja.get_template("index.html")
        return Response(t.render(context), mimetype="text/html")

    def fetch_users_data(self, username):
        users = self.api.get_users_by_name(username)
        if not users:
            return False
        for user in users:
            user_id = user["user_id"]
            yield (
                user_id,
                {
                    "user": user,
                    #'auth_log': self.api.get_authentication_log(users=[user_id]),
                    "bypass": self.api.get_user_bypass_codes(user_id),
                    "phones": self.api.get_user_phones(user_id),
                    "tokens": self.api.get_user_tokens(user_id),
                    #'u2ftokens': self.api.get_user_u2ftokens(user_id),
                    #'webauthncredentials': self.api.get_user_webauthncredentials(user_id),
                },
            )

    def wsgi_app(self, environ, start_response):
        request = Request(environ)
        response = self.dispatch_request(request)
        return response(environ, start_response)

    def __call__(self, environ, start_response):
        return self.wsgi_app(environ, start_response)


def create_app(redis=None, ikey=None, skey=None, host=None, lifetime=300, with_static=True):
    app = MFA(redis=redis, ikey=ikey, skey=skey, lifetime=lifetime, host=host)
    if with_static:
        app.wsgi_app = SharedDataMiddleware(
            app.wsgi_app, {"/static": str(Path(__file__).parent.joinpath('static'))}
        )
    return app


if __name__ == "__main__":
    from werkzeug.serving import run_simple

    ikey, skey, host = os.environ.get("DUO_API_CREDENTIALS", "::").split(":")
    redis = os.environ.get("REDIS_SOCKET")
    app = create_app(ikey=ikey, skey=skey, host=host, redis=redis)
    run_simple("0.0.0.0", 8080, app, use_debugger=True, use_reloader=True)
