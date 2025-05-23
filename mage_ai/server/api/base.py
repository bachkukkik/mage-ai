import asyncio
import json
import re
import traceback
import urllib.parse

import dateutil.parser
import simplejson
import tornado.web

from mage_ai.api.middleware import OAuthMiddleware
from mage_ai.server.api.constants import PATH_TRAVERSAL_PATTERN
from mage_ai.shared.parsers import encode_complex
from mage_ai.shared.strings import camel_to_snake_case
from mage_ai.usage_statistics.constants import EventNameType
from mage_ai.usage_statistics.logger import UsageStatisticLogger

META_KEY_LIMIT = '_limit'
META_KEY_OFFSET = '_offset'


class BaseHandler(tornado.web.RequestHandler):
    datetime_keys = []
    model_class = None

    def check_origin(self, origin):
        return True

    def get_bool_argument(self, name, default_value=None):
        value = self.get_argument(name, default_value)
        if type(value) is not str:
            return value
        return value.lower() in ('yes', 'true', 't', '1')

    def limit(self, results):
        limit = self.get_argument(META_KEY_LIMIT, None)
        offset = self.get_argument(META_KEY_OFFSET, None)
        if limit is not None:
            results = results.limit(limit)
        if offset is not None:
            results = results.offset(offset)
        return results

    def options(self, **kwargs):
        self.set_status(204)
        self.finish()

    def set_default_headers(self):
        methods = 'DELETE, GET, PATCH, POST, PUT, OPTIONS'
        self.set_header('Access-Control-Allow-Headers', '*')
        self.set_header('Access-Control-Allow-Methods', methods)
        self.set_header('Access-Control-Allow-Origin', '*')
        self.set_header('Content-Type', 'application/json')

    def write(self, chunk):
        if type(chunk) is dict:
            chunk = simplejson.dumps(
                chunk,
                default=encode_complex,
                ignore_nan=True,
            )
        super().write(chunk)

    def write_error(self, status_code, **kwargs):
        if status_code == 500:
            self.set_status(200)
            exception = kwargs['exc_info'][1]
            errors = traceback.format_stack()
            message = traceback.format_exc()

            child_pk = self.path_kwargs.get('child_pk')
            child = self.path_kwargs.get('child')
            pk = self.path_kwargs.get('pk')
            resource = self.path_kwargs.get('resource')

            asyncio.run(
                UsageStatisticLogger().error(
                    event_name=EventNameType.APPLICATION_ERROR,
                    code=status_code,
                    errors='\n'.join(errors or []),
                    message=str(exception),
                    operation=self.request.method,
                    resource=child or resource,
                    resource_id=child_pk if child else pk,
                    resource_parent=resource if child else None,
                    resource_parent_id=pk if child else None,
                    type=None,
                )
            )

            self.write(
                dict(
                    error=dict(
                        code=status_code,
                        errors=errors,
                        exception=str(exception),
                        message=message,
                    ),
                    url_parameters=self.path_kwargs,
                )
            )

    def write_model(self, model, **kwargs):
        key = camel_to_snake_case(self.model_class.__name__)
        self.write({key: model.to_dict(**kwargs)})

    def write_models(self, models):
        key = camel_to_snake_case(self.model_class.__name__) + 's'
        self.write({key: [m.to_dict() for m in models]})

    def get_payload(self):
        key = ''
        if self.model_class:
            key = camel_to_snake_case(self.model_class.__name__)

        payload = {}
        body = self.request.body
        if body:
            payload = json.loads(self.request.body)
            if key != '':
                payload = payload.get(key, {})
            for key in self.datetime_keys:
                if payload.get(key) is not None:
                    payload[key] = dateutil.parser.parse(payload[key])
        return payload


class BaseApiHandler(BaseHandler, OAuthMiddleware):
    def initialize(self, **kwargs) -> None:
        super().initialize(**kwargs)
        self.is_health_check = kwargs.get('is_health_check', False)

    def is_safe_path(self, user_input):
        """
        Check if the user input is safe and doesn't contain path traversal sequences.
        """
        return re.match(PATH_TRAVERSAL_PATTERN, user_input) is not None

    def prepare(self):
        from mage_ai.server.server import latest_user_activity

        if not self.is_health_check:
            latest_user_activity.update_latest_activity()

        # Validate the request path by decoding from bytes to string if necessary
        decoded_path = self.request.path
        if isinstance(decoded_path, bytes):
            decoded_path = decoded_path.decode('utf-8')  # Decode only if it's a bytes object

        decoded_path = urllib.parse.unquote(decoded_path)  # Decode URL-encoded characters
        if not self.is_safe_path(decoded_path):
            self.set_status(400)  # Bad Request
            self.write("Error: Invalid path (path traversal detected)")
            self.finish()
            return

        # Validate query parameters (if any)
        for key, value in self.request.arguments.items():
            # Decode each key and value from bytes to string if necessary
            decoded_key = key
            if isinstance(decoded_key, bytes):
                decoded_key = decoded_key.decode('utf-8')

            decoded_value = value[0]
            if isinstance(decoded_value, bytes):
                decoded_value = decoded_value.decode('utf-8')

            decoded_value = urllib.parse.unquote(decoded_value)  # Decode URL-encoded characters
            if not self.is_safe_path(decoded_value):
                self.set_status(400)
                self.write(
                    f"Error: Invalid parameter value for '{decoded_key}' (path traversal detected)"
                )
                self.finish()
                return
        super().prepare()


class BaseDetailHandler(BaseHandler):
    def get(self, model_id, **kwargs):
        model = self.model_class.query.get(int(model_id))
        self.write_model(model, **kwargs)

    def put(self, model_id, payload=None):
        model = self.model_class.query.get(int(model_id))
        payload = payload or self.get_payload()
        model.update(**payload)
        self.write_model(model)

    def delete(self, model_id):
        model = self.model_class.query.get(int(model_id))
        model.delete()
        self.write_model(model)
