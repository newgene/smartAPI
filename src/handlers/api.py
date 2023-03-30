import json
import logging

from biothings.web.auth.authn import BioThingsAuthnMixin
from biothings.web.handlers import BaseAPIHandler, QueryHandler
from biothings.web.handlers.query import BiothingHandler, capture_exceptions
import bmt
from tornado.httpclient import AsyncHTTPClient
from tornado.web import Finish, HTTPError

from controller import SmartAPI
from controller.exceptions import ControllerError, NotFoundError
from utils.downloader import DownloadError, download_async
from utils.notification import SlackNewAPIMessage, SlackNewTranslatorAPIMessage


def github_authenticated(func):
    """
    RegistryHandler Decorator
    """

    def _(self, *args, **kwargs):
        if not self.current_user:
            self.send_error(message="You must log in first.", status_code=401)
            return
        return func(self, *args, **kwargs)

    return _


class BaseHandler(BioThingsAuthnMixin, BaseAPIHandler):
    pass


class AuthHandler(BaseHandler):
    def set_cache_header(self, cache_value):
        # disabel cache for auth-related handlers
        self.set_header("Cache-Control", "private, max-age=0, no-cache")


class UserInfoHandler(AuthHandler):
    """ "Handler for /user_info endpoint."""

    def get(self):
        # Check for user cookie
        if self.current_user:
            self.write(self.current_user)
        else:
            # Check for WWW-authenticate header
            header = self.get_www_authenticate_header()
            if header:
                self.clear()
                self.set_header("WWW-Authenticate", header)
                self.set_status(401, "Unauthorized")
                # raising HTTPError will cause headers to be emptied
                self.finish()
            else:
                raise HTTPError(403)


class LoginHandler(AuthHandler):
    def get(self):
        self.redirect(self.get_argument("next", "/"))


class LogoutHandler(AuthHandler):
    def get(self):
        self.clear_cookie("user")
        self.redirect(self.get_argument("next", "/"))


class ValidateHandler(BaseHandler):
    """
    Validate a Swagger/OpenAPI document.
    Support three types of requests.

    GET /api/validate?url=<url>

    POST /api/validate
    url=<url>

    POST /api/validate
    {
        "openapi": "3.0.0",
        ...
    }
    """

    name = "validator"
    kwargs = {
        "GET": {"url": {"type": str, "location": "query", "required": True}},
        "POST": {"url": {"type": str, "location": "form"}},
    }

    # TODO
    # maybe this module should return 200 for all retrievable files?
    # when a document doesn't pass validation, maybe it's better to
    # indicate it by a field "passed": True/False instead of sharing
    # the same status code as missing a url parameter here.

    async def get(self):
        if self.request.body:
            raise HTTPError(400, reason="GET takes no request body.")

        raw = await self.download(self.args.url)
        self.validate(raw)

    async def post(self):
        if self.args.url:
            raw = await self.download(self.args.url)
        else:  # then treat the request body as raw
            raw = self.request.body

        self.validate(raw)

    async def download(self, url):
        try:
            file = await download_async(url)
        except DownloadError as err:
            raise HTTPError(400, reason=str(err))
        else:  # other file info irrelevant for validation
            return file.raw

    def validate(self, raw):
        try:
            smartapi = SmartAPI(SmartAPI.VALIDATION_ONLY)
            smartapi.raw = raw
            smartapi.validate()

        except (ControllerError, AssertionError) as err:
            raise HTTPError(400, reason=str(err))
        else:
            self.finish({"success": True, "details": f"valid SmartAPI ({smartapi.version}) metadata."})


class SmartAPIHandler(BaseHandler, BiothingHandler):
    kwargs = {
        "*": BiothingHandler.kwargs["*"],
        "PUT": {
            "slug": {"type": str, "default": None},
        },
        "POST": {
            "url": {"type": str, "required": True},
            "dryrun": {"type": bool, "default": False},
        },
    }

    @github_authenticated
    async def post(self):
        """
        Add an API document
        """

        if SmartAPI.find(self.args.url, "url"):
            raise HTTPError(409)

        try:
            file = await download_async(self.args.url)
        except DownloadError as err:
            raise HTTPError(400, reason=str(err)) from err

        try:
            smartapi = SmartAPI(self.args.url)
            smartapi.raw = file.raw
            smartapi.validate()
        except (ControllerError, AssertionError) as err:
            raise HTTPError(400, reason=str(err)) from err

        if self.args.dryrun:
            raise Finish({"success": True, "details": f"[Dryrun] Valid {smartapi.version} Metadata"})

        try:
            smartapi.username = self.current_user["login"]
            smartapi.refresh(file)  # populate webdoc meta
            _id = smartapi.save()
        except ControllerError as err:
            raise HTTPError(400, reason=str(err)) from err
        else:
            self.finish({"success": True, "_id": _id})
            await self._notify(smartapi)

    async def _notify(self, smartapi):
        if self.settings.get("debug"):
            return

        client = AsyncHTTPClient()
        kwargs = {
            "_id": smartapi._id,
            "name": dict(smartapi).get("info", {}).get("title", "<Notitle>"),
            "description": dict(smartapi).get("info", {}).get("description", "")[:120] + "...",
            "username": smartapi.username,
        }
        try:
            # NOTE
            # SLACK_WEBHOOKS = [
            #     {"webhook": <url>}
            #     {"webhook": <url>, "tags": "translator"} # project specific
            # ]
            for slack in getattr(self.web_settings, "SLACK_WEBHOOKS", []):
                if "tags" in slack:
                    if slack["tags"] == "translator":
                        if "x-translator" in smartapi["info"]:
                            res = await client.fetch(
                                slack["webhook"],
                                method="POST",
                                headers={"content-type": "application/json"},
                                body=json.dumps(SlackNewTranslatorAPIMessage(**kwargs).compose()),
                            )
                            logging.info(res.code)
                            logging.info(res.body)

                    # elif slack["tags"] == <other>:
                    #   pass

                else:  # typical case
                    res = await client.fetch(
                        slack["webhook"],
                        method="POST",
                        headers={"content-type": "application/json"},
                        body=json.dumps(SlackNewAPIMessage(**kwargs).compose()),
                    )
                    logging.info(res.code)
                    logging.info(res.body)

        except Exception as exc:
            logging.error(str(exc))

    @github_authenticated
    async def put(self, _id):
        """
        Add/Update the URL slug:
            PUT {"slug": "new_slug"}
        Remove a URL slug:
            PUT {"slug": "" }
        Refresh a document:
            PUT {}
        """

        try:
            smartapi = SmartAPI.get(_id)
        except NotFoundError:
            raise HTTPError(404)

        if smartapi.username != self.current_user["login"]:
            raise HTTPError(403)

        if self.args.slug is not None:
            if self.args.slug in {"api"}:  # reserved
                raise HTTPError(400, reason="slug is reserved")

            try:  # update slug
                smartapi.slug = self.args.slug or None
                smartapi.save()

            except (ControllerError, ValueError) as err:
                raise HTTPError(400, reason=str(err)) from err

            self.finish({"success": True})

        else:  # refresh
            file = await download_async(smartapi.url, raise_error=False)
            code = smartapi.refresh(file)
            smartapi.save()

            try:
                status = smartapi.webdoc.STATUS(code)
                status = status.name.lower()
            except ValueError:
                status = "nofile"  # keep the original copy

            self.finish({"success": code in (200, 299), "status": status, "code": code})

    @github_authenticated
    def delete(self, _id):
        """
        Delete API
        """

        try:
            smartapi = SmartAPI.get(_id)
        except NotFoundError:
            raise HTTPError(404)

        if smartapi.username != self.current_user["login"]:
            raise HTTPError(403)

        try:
            _id = smartapi.delete()
        except ControllerError as err:
            raise HTTPError(400, reason=str(err)) from err

        self.finish({"success": True, "_id": _id})


class ValueSuggestionHandler(BaseHandler):
    """
    Handle field aggregation for UI suggestions
    """

    kwargs = {
        "GET": {"field": {"type": str, "required": True}},
    }

    name = "value_suggestion"

    def get(self):
        """
        /api/suggestion?field=
        Returns aggregations for any field provided
        Used for tag:count on registry
        """
        res = SmartAPI.get_tags(self.args.field)
        self.finish(res)


class UptimeHandler(BaseHandler):
    """
    Check uptime status for a registered API

    GET /api/uptime?id=<id>

    POST /api/uptime
    id=<id>
    """

    kwargs = {
        "GET": {"id": {"type": str, "location": "query", "required": True}},
        "POST": {"id": {"type": str, "location": "form", "required": True}},
    }

    name = "uptime_checker"

    @github_authenticated
    def get(self):
        if self.request.body:
            raise HTTPError(400, reason="GET takes no request body.")

        if self.args.id:
            try:
                smartapi = SmartAPI.get(self.args.id)
                if smartapi.username != self.current_user["login"]:
                    raise HTTPError(403)
                status = smartapi.check()
                smartapi.save()
            except NotFoundError:
                raise HTTPError(404)
            except (ControllerError, AssertionError) as err:
                raise HTTPError(400, reason=str(err))
            else:
                self.finish({"success": True, "details": status})
        else:
            raise HTTPError(400, reason="Missing required parameter: id")

    @github_authenticated
    def post(self):
        if self.args.id:
            try:
                smartapi = SmartAPI.get(self.args.id)
                if smartapi.username != self.current_user["login"]:
                    raise HTTPError(403)
                status = smartapi.check()
                smartapi.save()
            except NotFoundError:
                raise HTTPError(404)
            except (ControllerError, AssertionError) as err:
                raise HTTPError(400, reason=str(err))
            else:
                self.finish({"success": True, "details": status})
        else:
            raise HTTPError(400, reason="Missing required form field: id")


class MetaKGQueryHandler(QueryHandler):
    """
    Support metakg queries with biolink model's semantic descendants

    Allowed query by fields: subject, object, predicate
    If expand is passed, will use Biolink Model Toolkit to get these terms' descendants, and queries by them instead.
    """

    kwargs = {
        "*": QueryHandler.kwargs["*"],
        "GET": {
            "subject": {"type": str, "location": "query", "required": False},
            "object": {"type": str, "required": False},
            "predicate": {"type": str, "required": False},
            "expand": {"type": bool, "default": False},
        },
    }

    @capture_exceptions
    async def get(self, *args, **kwargs):
        biolink_model_toolkit = None
        if self.args.expand:
            biolink_model_toolkit = bmt.Toolkit()

        for field in ["subject", "object", "predicate"]:
            value = getattr(self.args, field)
            if not value:
                continue
            if biolink_model_toolkit:
                try:
                    value = biolink_model_toolkit.get_descendants(value)
                except ValueError as ex:
                    raise HTTPError(
                        400, reason=f"Cannot get descendants for field: `{field}` with value: `{value}`. Error: {ex}"
                    )
            else:
                value = [value]
            setattr(self.args, field, value)

        await super().get(*args, **kwargs)
