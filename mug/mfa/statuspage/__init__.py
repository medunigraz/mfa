import os
import json
import ldap
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
from tenacity import Retrying, RetryError, stop_after_attempt, wait_fixed, wait_random, retry_if_exception_type


class MFA(object):
    def __init__(self, **config):
        self.redis = config.get("redis") and Redis(
            unix_socket_path=config.get("redis"), decode_responses=True
        )
        self.api = duo_client.Admin(
            ikey=config.get("ikey"), skey=config.get("skey"), host=config.get("host")
        )
        self.template_path = str(Path(__file__).parent.joinpath("templates"))
        self.locale_path = str(Path(__file__).parent.joinpath("locale"))
        self.logos = [lang.lstrip(".") for lang, _ in [l.suffixes for l in Path(__file__).parent.joinpath("static").glob("logo.*.svg")]]
        self.lifetime = config.get("lifetime", 300)

        self.ldap_uri = config.get("ldap_uri", "ldap://localhost")
        self.ldap_dn = config.get("ldap_dn")
        self.ldap_password = config.get("ldap_password")
        self.ldap_base_dn = config.get("ldap_base_dn", "DC=example,DC=com")
        self.ldap_filter = config.get("ldap_filter", "(cn={username})")
        self.locked_group = config.get("locked_group")

        self.duo_user_keys = (
            "created",
            "is_enrolled",
            "last_directory_sync",
            "last_login",
            "phones",
            "status",
            "tokens",
            "webauthncredentials",
        )

    def dispatch_request(self, request):
        username = request.remote_user or "o_mikl"
        if self.redis:
            cached = self.redis.get(f"{__name__}:{username}")
            if cached is None:
                user = self.fetch_user_data(username)
                self.redis.set(username, json.dumps(user), ex=self.lifetime)
            else:
                user = json.loads(cached)
        else:
            user = self.fetch_user_data(username)
        if not user:
            return Response(f"No user data found for {username}", status=404)
        jinja = Environment(
            loader=FileSystemLoader(self.template_path),
            autoescape=True,
            extensions=(
                "jinja2.ext.i18n",
                "jinja2.ext.debug",
                "jinja2_time.TimeExtension",
            ),
        )
        logo = request.accept_languages.best_match(self.logos)
        languages = list(request.accept_languages.values())
        translations = Translations.load(self.locale_path, languages, "mfa")
        jinja.install_gettext_translations(translations)
        jinja.filters["fromtimestamp"] = partial(
            datetime.fromtimestamp, tz=timezone.utc
        )
        context = {
            "username": username,
            "user": user,
            "logo": logo,
        }
        t = jinja.get_template("index.html")
        return Response(t.render(context), mimetype="text/html")

    def fetch_user_data(self, username):
        retry = Retrying(
            retry=retry_if_exception_type(StopIteration),
            stop=stop_after_attempt(3),
            wait=wait_fixed(3) + wait_random(0, 2)
        )
        try:
            l = ldap.initialize(self.ldap_uri)
            if self.ldap_dn:
                l.simple_bind_s(self.ldap_dn, self.ldap_password)

        except ldap.LDAPError:
            return False
        try:
            for attempt in retry.copy():
                with attempt:
                    du = next(iter(self.api.get_users_by_name(username) or []))
        except RetryError:
            du = {}
            active = False
        else:
            active = True
        try:
            for attempt in retry.copy():
                with attempt:
                    _, lu = next(iter(
                        l.search_s(
                            self.ldap_base_dn,
                            ldap.SCOPE_SUBTREE,
                            self.ldap_filter.format(username=username),
                            ["mail", "sn", "title", "givenName", "memberOf"]
                        )
                    ) or [])
        except RetryError:
            return False

        def extract_value(values):
            return next((t.decode("utf-8") for t in values or []), None)
        locked = self.locked_group.encode("utf-8") in lu.get("memberOf", [])

        return {
            "active": active,
            "locked": locked,
            "firstname": du.get("firstname", extract_value(lu.get("givenName"))),
            "lastname": du.get("lastname", extract_value(lu.get("sn"))),
            "title": extract_value(lu.get("title")),
            "mail": du.get("email", extract_value(lu.get("mail"))),
            **{k: v for k, v in du.items() if k in self.duo_user_keys}
        }

    def wsgi_app(self, environ, start_response):
        request = Request(environ)
        response = self.dispatch_request(request)
        return response(environ, start_response)

    def __call__(self, environ, start_response):
        return self.wsgi_app(environ, start_response)


def create_app(with_static=True, **config):
    app = MFA(**config)
    if with_static:
        app.wsgi_app = SharedDataMiddleware(
            app.wsgi_app, {"/static": str(Path(__file__).parent.joinpath("static"))}
        )
    return app


if __name__ == "__main__":
    from werkzeug.serving import run_simple

    app = create_app(
        ikey=os.environ.get("DUO_API_IKEY"),
        skey=os.environ.get("DUO_API_SKEY"),
        host=os.environ.get("DUO_API_HOST"),
        redis=os.environ.get("REDIS"),
        ldap_uri=os.environ.get("LDAP_URI"),
        ldap_base_dn=os.environ.get("LDAP_BASE_DN"),
        ldap_dn=os.environ.get("LDAP_DN"),
        ldap_password=os.environ.get("LDAP_PASSWORD"),
        locked_group=os.environ.get("LOCKED_GROUP")
    )
    run_simple("0.0.0.0", 8080, app, use_debugger=True, use_reloader=True)
